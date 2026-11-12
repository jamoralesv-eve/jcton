import contextlib
import io
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from pr_gate.cli import main
from pr_gate.decide import EXIT_ERROR
from pr_gate.decide import EXIT_PASS
from pr_gate.errors import GateError
from pr_gate.recap import FileDiff
from pr_gate.recap import gather_recap
from pr_gate.recap import parse_unified
from pr_gate.recap import score_file
from pr_gate.recap_html import render_recap
from pr_gate.recap_notes import parse_notes
from pr_gate.recap_notes import template
from pr_gate.structdiff import FileReport

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}
UNIFIED = """diff --git a/app/auth.py b/app/auth.py
index 1..2 100644
--- a/app/auth.py
+++ b/app/auth.py
@@ -3,3 +3,4 @@ def login():
 a = 1
-b = 2
+b = 3
+c = 4
 d = 5
diff --git a/old.py b/new.py
similarity index 90%
rename from old.py
rename to new.py
diff --git a/logo.png b/logo.png
Binary files a/logo.png and b/logo.png differ
"""


def _git(directory: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=directory, check=True, capture_output=True, env=GIT_ENV)


def _repo(directory: Path) -> None:
    _git(directory, "init", "-q", "-b", "main")
    (directory / "app").mkdir()
    (directory / "tests").mkdir()
    (directory / "app" / "session.py").write_text(
        "def issue(user):\n    return {'sub': user}\n\n\ndef revoke(token):\n    return True\n"
    )
    (directory / "tests" / "test_session.py").write_text(
        "def test_issue():\n    assert issue('a')\n    assert True\n"
    )
    _git(directory, "add", "-A")
    _git(directory, "commit", "-qm", "base")
    _git(directory, "checkout", "-qb", "feature")
    (directory / "app" / "session.py").write_text(
        "API_KEY = 'sk-live-123456'\n\n\ndef issue(user, ttl=900):\n"
        "    return {'sub': user, 'ttl': ttl, 'x': '<script>alert(1)</script>'}\n"
    )
    (directory / "tests" / "test_session.py").write_text(
        "import pytest\n\n\n@pytest.mark.skip\ndef test_issue():\n    pass\n"
    )
    (directory / "uv.lock").write_text("lock\n")
    _git(directory, "add", "-A")
    _git(directory, "commit", "-qm", "rotate tokens")


def _quiet(call):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = call()
    return code, out.getvalue() + err.getvalue()


class ParseUnifiedTests(unittest.TestCase):
    def test_line_numbers_follow_the_hunk_header(self) -> None:
        files = parse_unified(UNIFIED)
        lines = files["app/auth.py"].hunks[0].lines
        self.assertEqual([(l.kind, l.old_no, l.new_no) for l in lines], [
            (" ", 3, 3), ("-", 4, None), ("+", None, 4), ("+", None, 5), (" ", 5, 6),
        ])

    def test_rename_and_binary(self) -> None:
        files = parse_unified(UNIFIED)
        self.assertEqual(files["new.py"].old_path, "old.py")
        self.assertTrue(files["logo.png"].binary)


class RiskTests(unittest.TestCase):
    def test_auth_path_scores_higher_than_plain_code(self) -> None:
        auth = score_file(FileReport("app/auth/login.py", "M", None, 10, 2))
        plain = score_file(FileReport("app/billing.py", "M", None, 10, 2))
        self.assertGreater(auth.score, plain.score)
        self.assertIn("toca autenticación, permisos o secretos", auth.signals)

    def test_lockfile_is_low_risk(self) -> None:
        risk = score_file(FileReport("uv.lock", "M", None, 900, 900))
        self.assertEqual((risk.score, risk.level), (0.0, "bajo"))

    def test_added_credential_and_disabled_tls(self) -> None:
        diff = parse_unified(
            "diff --git a/x.py b/x.py\n@@ -1,0 +1,2 @@\n"
            "+API_KEY = 'abcd1234efgh'\n+requests.get(url, verify=False)\n"
        )["x.py"]
        risk = score_file(FileReport("x.py", "M", None, 2, 0), diff)
        self.assertIn("posible credencial escrita en el código", risk.signals)
        self.assertIn("desactiva verificación TLS", risk.signals)

    def test_docs_do_not_trigger_code_patterns(self) -> None:
        diff = parse_unified(
            "diff --git a/README.md b/README.md\n@@ -1,0 +1,1 @@\n+Flags `eval(` and `verify=False`.\n"
        )["README.md"]
        risk = score_file(FileReport("README.md", "M", None, 1, 0), diff)
        self.assertEqual(risk.signals, [])

    def test_lost_assertions_in_tests(self) -> None:
        diff = parse_unified(
            "diff --git a/tests/test_a.py b/tests/test_a.py\n@@ -1,2 +1,1 @@\n"
            "-    assert a == 1\n-    assert b == 2\n+    pass\n"
        )["tests/test_a.py"]
        risk = score_file(FileReport("tests/test_a.py", "M", None, 1, 2), diff)
        self.assertIn("2 aserción(es) menos en tests", risk.signals)


class NotesTests(unittest.TestCase):
    def test_paths_outside_the_diff_are_flagged_not_hidden(self) -> None:
        notes = parse_notes({
            "review_order": [{"path": "a.py", "why": "x"}, {"path": "ghost.py"}],
            "risks": [{"level": "alto", "title": "t", "path": "ghost.py"}],
            "file_notes": {"ghost.py": "x", "a.py": "ok"},
        }, {"a.py"})
        self.assertFalse(notes.review_order[1].known)
        self.assertFalse(notes.risks[0].known)
        self.assertEqual(notes.file_notes, {"a.py": "ok"})
        self.assertEqual(len(notes.problems), 3)

    def test_bad_level_stops(self) -> None:
        with self.assertRaises(GateError):
            parse_notes({"risks": [{"level": "crítico", "title": "x"}]}, set())

    def test_template_uses_the_diff_paths(self) -> None:
        skeleton = template(["a.py", "b.py"])
        self.assertEqual(list(skeleton["file_notes"]), ["a.py", "b.py"])
        parse_notes(skeleton, {"a.py", "b.py"})


class RecapEndToEndTests(unittest.TestCase):
    def test_html_is_offline_and_escaped(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            _repo(directory)
            facts = gather_recap("main", "HEAD", str(directory))
            notes = parse_notes({"summary": "<img src=x onerror=alert(1)> `ok`"}, facts.paths())
            page = render_recap(facts, notes)
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.assertNotIn("<img", page)
        self.assertIn("<code>ok</code>", page)
        self.assertIsNone(re.search(r"(src|href)=\"(https?:)?//", page))
        self.assertIn("default-src 'none'", page)

    def test_facts_rank_session_first_and_flag_tests(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            _repo(directory)
            facts = gather_recap("main", "HEAD", str(directory))
        self.assertEqual(facts.files[0].path, "app/session.py")
        session = facts.files[0].risk.signals
        self.assertIn("posible credencial escrita en el código", session)
        self.assertIn("1 símbolo(s) eliminado(s)", session)
        tests = next(f for f in facts.files if f.is_test)
        self.assertIn("salta o aísla tests", tests.risk.signals)
        self.assertIn("2 aserción(es) menos en tests", tests.risk.signals)

    def test_cli_writes_html_and_prints_facts(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            _repo(directory)
            out = directory / "recap.html"
            notes = directory / "notes.json"
            notes.write_text(json.dumps({"summary": "hola", "review_order": [{"path": "nope.py"}]}))
            code, printed = _quiet(lambda: main([
                "recap", "main", "HEAD", "--repo", str(directory),
                "--notes", str(notes), "--out", str(out),
            ]))
            self.assertEqual(code, EXIT_PASS)
            self.assertTrue(out.read_text().startswith("<!doctype html>"))
            self.assertIn("nope.py", printed)
            code, printed = _quiet(lambda: main(["recap", "main", "--repo", str(directory), "--facts"]))
            self.assertEqual(code, EXIT_PASS)
            self.assertEqual(json.loads(printed)["files"][0]["path"], "app/session.py")

    def test_cli_rejects_unknown_revision(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            _repo(directory)
            code, _ = _quiet(lambda: main(["recap", "no-such-branch", "--repo", str(directory)]))
        self.assertEqual(code, EXIT_ERROR)


if __name__ == "__main__":
    unittest.main()
