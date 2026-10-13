import argparse
import asyncio
import os
import sys
from pathlib import Path

from pr_gate.collect import CollectRequest
from pr_gate.collect import GateInputs
from pr_gate.collect import gather
from pr_gate.decide import EXIT_ERROR
from pr_gate.decide import EXIT_PASS
from pr_gate.decide import distance_of
from pr_gate.decide import exit_code
from pr_gate.decide import render_report
from pr_gate.errors import GateError
from pr_gate.run import evaluate


def main(argv: list[str] | None = None) -> int:
    try:
        return _execute(_parser().parse_args(argv))
    except GateError as error:
        return _fail(str(error))
    except Exception as error:
        return _fail(f"{type(error).__name__}: {error}")


def _execute(args: argparse.Namespace) -> int:
    if args.print_inputs:
        return _show_inputs(args)
    return _evaluate_pr(args)


def _show_inputs(args: argparse.Namespace) -> int:
    _print_inputs(_gather(args))
    return EXIT_PASS


def _evaluate_pr(args: argparse.Namespace) -> int:
    threshold = _checked_threshold(args.threshold)
    _require_model_key()
    inputs = _gather(args)
    verdict = asyncio.run(evaluate(inputs.diff, inputs.ticket))
    return _write_and_exit(verdict, threshold, Path(args.report))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pr_gate",
        description="Compara un PR con su ticket de Linear.",
    )
    parser.add_argument("--pr", help="Número del pull request.")
    parser.add_argument("--repo", help="Repositorio owner/name.")
    parser.add_argument("--ticket", help="Issue de Linear, por ejemplo ENG-123.")
    parser.add_argument("--threshold", type=float, default=_threshold_from_env())
    parser.add_argument("--report", default="report.md")
    parser.add_argument(
        "--print-inputs",
        action="store_true",
        help="Muestra el diff y el ticket, sin llamar al modelo.",
    )
    return parser


def _gather(args: argparse.Namespace) -> GateInputs:
    return gather(
        CollectRequest(
            ticket_id=args.ticket,
            pr_number=args.pr,
            repo=args.repo,
            api_key=_linear_key(),
        )
    )


def _print_inputs(inputs: GateInputs) -> None:
    print(f"ticket: {inputs.ticket_id}")
    print(f"pr: {inputs.pr_url}")
    print("--- ticket ---")
    print(inputs.ticket)
    print("--- diff ---")
    print(inputs.diff)


def _write_and_exit(verdict: dict, threshold: float, report_path: Path) -> int:
    report = render_report(verdict, threshold)
    report_path.write_text(report)
    print(report, end="")
    return exit_code(distance_of(verdict), threshold)


def _linear_key() -> str:
    key = os.environ.get("LINEAR_API_KEY", "").strip()
    if not key:
        raise GateError("Falta LINEAR_API_KEY")
    return key


def _require_model_key() -> None:
    if os.environ.get("GOOGLE_API_KEY", "").strip():
        return
    raise GateError("Falta GOOGLE_API_KEY")


def _threshold_from_env() -> float:
    raw = os.getenv("DISTANCE_THRESHOLD", "0.5")
    try:
        return float(raw)
    except ValueError as error:
        raise GateError("DISTANCE_THRESHOLD no es un número") from error


def _checked_threshold(value: float) -> float:
    if 0 <= value <= 1:
        return value
    raise GateError("El umbral tiene que estar entre 0 y 1")


def _fail(message: str) -> int:
    print(message, file=sys.stderr)
    return EXIT_ERROR
