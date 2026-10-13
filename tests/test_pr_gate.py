import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from pr_gate.agent import compare_agent
from pr_gate.agent import rebuild_agent
from pr_gate.cli import main
from pr_gate.collect import GateInputs
from pr_gate.collect import MAX_DIFF_CHARS
from pr_gate.collect import branch_ticket_id
from pr_gate.collect import collect_diff
from pr_gate.collect import fetch_ticket
from pr_gate.collect import limit_diff
from pr_gate.collect import resolve_ticket_id
from pr_gate.decide import EXIT_ERROR
from pr_gate.decide import EXIT_FAIL
from pr_gate.decide import EXIT_PASS
from pr_gate.decide import as_verdict
from pr_gate.decide import exit_code
from pr_gate.decide import render_report
from pr_gate.errors import GateError
from pr_gate.errors import TicketNotInBranch
from pr_gate.prompts import COMPARE_PROMPT
from pr_gate.prompts import REBUILD_PROMPT

REPO = Path(__file__).resolve().parents[1]
GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}
BODY = "See https://linear.app/acme/issue/ENG-9/login"
VERDICT = {
    "overall_distance": 0.8,
    "missing_in_pr": ["criterio de login"],
    "unrequested_changes": [],
    "justification": "falta un criterio",
    "confidence": "high",
}


class TicketResolutionTests(unittest.TestCase):
    def test_branch_with_username_prefix(self) -> None:
        self.assertEqual(branch_ticket_id("alejandro/eng-123-fix-login"), "ENG-123")

    def test_branch_without_username(self) -> None:
        self.assertEqual(branch_ticket_id("eng-45-title"), "ENG-45")

    def test_branch_without_ticket_raises(self) -> None:
        with self.assertRaises(TicketNotInBranch):
            branch_ticket_id("main")

    def test_url_in_body(self) -> None:
        ticket_id = resolve_ticket_id(None, "main", BODY)
        self.assertEqual(ticket_id, "ENG-9")

    def test_explicit_ticket_wins(self) -> None:
        ticket_id = resolve_ticket_id("lin-2", "alejandro/eng-123-x", BODY)
        self.assertEqual(ticket_id, "LIN-2")

    def test_missing_ticket(self) -> None:
        with self.assertRaises(GateError):
            resolve_ticket_id(None, "main", "no link here")

    def test_several_tickets(self) -> None:
        body = BODY + "\nhttps://linear.app/acme/issue/ENG-10/other"
        with self.assertRaises(GateError) as caught:
            resolve_ticket_id(None, "main", body)
        self.assertIn("ENG-9", str(caught.exception))
        self.assertIn("ENG-10", str(caught.exception))

    def test_same_id_in_url_and_branch_is_one_ticket(self) -> None:
        ticket_id = resolve_ticket_id(None, "alejandro/eng-9-login", BODY)
        self.assertEqual(ticket_id, "ENG-9")


class DiffTests(unittest.TestCase):
    def test_limit_diff_keeps_short_diffs(self) -> None:
        self.assertEqual(limit_diff("abc"), "abc")

    def test_limit_diff_marks_truncation(self) -> None:
        limited = limit_diff("a" * (MAX_DIFF_CHARS + 10))
        self.assertTrue(limited.endswith("[diff truncated]\n"))
        self.assertLess(len(limited), MAX_DIFF_CHARS + 30)

    def test_collect_diff_drops_lockfiles(self) -> None:
        with tempfile.TemporaryDirectory(dir=REPO) as raw:
            directory = Path(raw)
            _commit_base(directory)
            _commit_change(directory)
            diff = _in_directory(directory, lambda: collect_diff("main"))
        self.assertIn("app.py", diff)
        self.assertNotIn("package-lock.json", diff)
        self.assertNotIn("yarn.lock", diff)

    def test_empty_diff_raises(self) -> None:
        with tempfile.TemporaryDirectory(dir=REPO) as raw:
            directory = Path(raw)
            _commit_base(directory)
            with self.assertRaises(GateError):
                _in_directory(directory, lambda: collect_diff("main"))


class LinearTests(unittest.TestCase):
    def test_fetch_ticket_formats_title_and_description(self) -> None:
        payload = {
            "data": {
                "issue": {
                    "identifier": "ENG-1",
                    "title": "Login",
                    "description": "Allow login",
                    "url": "https://linear.app/acme/issue/ENG-1",
                }
            }
        }
        text = _fetch(payload)
        self.assertEqual(text, "# ENG-1 Login\n\nAllow login")

    def test_missing_issue_raises(self) -> None:
        with self.assertRaises(GateError):
            _fetch({"data": {"issue": None}})

    def test_graphql_error_raises(self) -> None:
        with self.assertRaises(GateError) as caught:
            _fetch({"errors": [{"message": "boom"}]})
        self.assertIn("boom", str(caught.exception))

    def test_network_error_raises(self) -> None:
        with patch("pr_gate.collect.urllib.request.urlopen") as urlopen:
            urlopen.side_effect = urllib.error.URLError("down")
            with self.assertRaises(GateError):
                fetch_ticket("ENG-1", "key")


class DecisionTests(unittest.TestCase):
    def test_distance_above_threshold_fails(self) -> None:
        self.assertEqual(exit_code(0.51, 0.5), EXIT_FAIL)

    def test_distance_equal_to_threshold_passes(self) -> None:
        self.assertEqual(exit_code(0.5, 0.5), EXIT_PASS)

    def test_json_string_verdict(self) -> None:
        parsed = as_verdict(json.dumps(VERDICT))
        self.assertEqual(parsed["overall_distance"], 0.8)

    def test_report_lists_missing_requirement(self) -> None:
        report = render_report(VERDICT, 0.5)
        self.assertIn("**Distancia:** 0.80", report)
        self.assertIn("criterio de login", report)
        self.assertIn("- ninguno", report)


class AgentFirewallTests(unittest.TestCase):
    def test_prompts_have_no_template_braces(self) -> None:
        self.assertNotIn("{", REBUILD_PROMPT)
        self.assertNotIn("{", COMPARE_PROMPT)

    def test_rebuilder_sees_only_the_diff(self) -> None:
        self.assertIn("{pr_diff}", rebuild_agent.instruction)
        self.assertNotIn("original_ticket", rebuild_agent.instruction)

    def test_comparator_sees_both_documents_and_not_the_diff(self) -> None:
        self.assertIn("{original_ticket}", compare_agent.instruction)
        self.assertIn("{reconstructed_ticket}", compare_agent.instruction)
        self.assertNotIn("{pr_diff}", compare_agent.instruction)
        self.assertIs(compare_agent.output_key, "verdict")


class CliTests(unittest.TestCase):
    def test_missing_linear_key(self) -> None:
        with patch.dict(os.environ, {"LINEAR_API_KEY": ""}):
            code, stderr = _quiet(lambda: main(["--print-inputs"]))
        self.assertEqual(code, EXIT_ERROR)
        self.assertIn("LINEAR_API_KEY", stderr)

    def test_print_inputs_skips_the_model(self) -> None:
        inputs = GateInputs("diff-body", "# ENG-1\n\nbody", "ENG-1", "http://pr")
        with patch.dict(os.environ, {"LINEAR_API_KEY": "lin_test"}):
            with patch("pr_gate.cli.gather", return_value=inputs):
                with patch("pr_gate.cli.evaluate") as evaluate:
                    code, stdout = _quiet(lambda: main(["--print-inputs"]))
        self.assertEqual(code, EXIT_PASS)
        self.assertIn("ENG-1", stdout)
        self.assertIn("diff-body", stdout)
        evaluate.assert_not_called()

    def test_bad_threshold_stops_before_the_model(self) -> None:
        with patch("pr_gate.cli.evaluate") as evaluate:
            code, stderr = _quiet(lambda: main(["--threshold", "2"]))
        self.assertEqual(code, EXIT_ERROR)
        self.assertIn("umbral", stderr)
        evaluate.assert_not_called()

    def test_distance_above_threshold_writes_report_and_fails(self) -> None:
        inputs = GateInputs("diff", "ticket", "ENG-1", "http://pr")
        with tempfile.TemporaryDirectory(dir=REPO) as raw:
            report = Path(raw) / "report.md"
            code = _run_gate(inputs, VERDICT, report, "0.5")
            text = report.read_text()
        self.assertEqual(code, EXIT_FAIL)
        self.assertIn("0.80", text)

    def test_distance_within_threshold_passes(self) -> None:
        inputs = GateInputs("diff", "ticket", "ENG-1", "http://pr")
        passing = {**VERDICT, "overall_distance": 0.2}
        with tempfile.TemporaryDirectory(dir=REPO) as raw:
            report = Path(raw) / "report.md"
            code = _run_gate(inputs, passing, report, "0.5")
        self.assertEqual(code, EXIT_PASS)


def _fetch(payload: dict) -> str:
    raw = io.BytesIO(json.dumps(payload).encode())
    with patch("pr_gate.collect.urllib.request.urlopen") as urlopen:
        urlopen.return_value.__enter__.return_value = raw
        return fetch_ticket("ENG-1", "key")


def _run_gate(inputs: GateInputs, verdict: dict, report: Path, threshold: str) -> int:
    env = {"LINEAR_API_KEY": "lin_test", "GOOGLE_API_KEY": "google_test"}

    async def fake_evaluate(_diff: str, _ticket: str) -> dict:
        return verdict

    def run() -> int:
        with patch.dict(os.environ, env):
            with patch("pr_gate.cli.gather", return_value=inputs):
                with patch("pr_gate.cli.evaluate", fake_evaluate):
                    return main(["--threshold", threshold, "--report", str(report)])

    code, _stdout = _quiet(run)
    return code


def _quiet(action):
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        result = action()
    return result, stdout.getvalue() + stderr.getvalue()


def _commit_base(directory: Path) -> None:
    _git(directory, "init", "-b", "main")
    (directory / "app.py").write_text("print(1)\n")
    (directory / "package-lock.json").write_text("{}\n")
    _git(directory, "add", ".")
    _git(directory, "commit", "-m", "base")
    _git(directory, "update-ref", "refs/remotes/origin/main", "HEAD")


def _commit_change(directory: Path) -> None:
    (directory / "app.py").write_text("print(2)\n")
    (directory / "package-lock.json").write_text('{"lock": true}\n')
    (directory / "yarn.lock").write_text("lock\n")
    _git(directory, "add", ".")
    _git(directory, "commit", "-m", "change")


def _git(directory: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(directory), *args],
        check=True,
        env=GIT_ENV,
        capture_output=True,
        text=True,
    )


def _in_directory(directory: Path, action):
    previous = os.getcwd()
    os.chdir(directory)
    try:
        return action()
    finally:
        os.chdir(previous)


if __name__ == "__main__":
    unittest.main()
