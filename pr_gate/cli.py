import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown

from pr_gate.agent import model_name
from pr_gate.agent import required_api_key
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

_USAGE = """
ejemplos:
  jcton
  jcton --ticket ENG-123
  jcton --threshold 0.5 --report reports/custom.md
  jcton --print-inputs

Corre jcton desde el checkout de la rama a revisar.
LINEAR_API_KEY es obligatorio. El modelo por defecto también pide ANTHROPIC_API_KEY.
DISTANCE_THRESHOLD sustituye el umbral de 0.5 cuando se omite --threshold.
"""


class _UsageFormat(
    argparse.RawDescriptionHelpFormatter,
    argparse.ArgumentDefaultsHelpFormatter,
):
    def _get_help_string(self, action: argparse.Action) -> str:
        if action.default in (None, False):
            return action.help or ""
        return super()._get_help_string(action)


def print_header() -> None:
    print(r"""   
                 ___                         ___           ___
    ___         /\__\                       /\  \         /\  \
   /\__\       /:/  /          ___         /::\  \        \:\  \
  /:/__/      /:/  /          /\__\       /:/\:\  \        \:\  \
 /::\  \     /:/  /  ___     /:/  /      /:/  \:\  \   _____\:\  \
 \/\:\  \   /:/__/  /\__\   /:/__/      /:/__/ \:\__\ /::::::::\__\
  ~~\:\  \  \:\  \ /:/  /  /::\  \      \:\  \ /:/  / \:\~~\~~\/__/
     \:\__\  \:\  /:/  /  /:/\:\  \      \:\  /:/  /   \:\  \
     /:/  /   \:\/:/  /   \/__\:\  \      \:\/:/  /     \:\  \
    /:/  /     \::/  /         \:\__\      \::/  /       \:\__\
    \/__/       \/__/           \/__/       \/__/         \/__/
""")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    print_header()
    try:
        return _execute(args)
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
    report_path = _destination(args, inputs)
    return _write_and_exit(verdict, threshold, report_path, inputs)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jcton",
        description="Compara la rama actual con su ticket de Linear.",
        epilog=_USAGE,
        formatter_class=_UsageFormat,
    )
    parser.add_argument("--ticket", help="Issue de Linear, por ejemplo ENG-123.")
    parser.add_argument(
        "--threshold",
        type=float,
        default=_threshold_from_env(),
        help="Distancia máxima para aprobar, entre 0 y 1.",
    )
    parser.add_argument(
        "--report",
        help="Ruta del reporte. Si se omite, se crea en reports/ con fecha, rama y ticket.",
    )
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
            api_key=_linear_key(),
        )
    )


def _print_inputs(inputs: GateInputs) -> None:
    print(f"ticket: {inputs.ticket_id}")
    print(f"branch: {inputs.branch}")
    print("--- ticket ---")
    print(inputs.ticket)
    print("--- diff ---")
    print(inputs.diff)


def _destination(args: argparse.Namespace, inputs: GateInputs) -> Path:
    if args.report:
        return Path(args.report)
    return _generated_report(inputs)


def _generated_report(inputs: GateInputs) -> Path:
    name = report_filename(inputs.branch, inputs.ticket_id, datetime.now())
    return _reports_directory() / name


def _reports_directory() -> Path:
    directory = Path(__file__).resolve().parents[1] / "reports"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def report_filename(branch: str, ticket_id: str, moment: datetime) -> str:
    stamp = moment.strftime("%Y-%m-%d-%H%M%S")
    return f"{stamp}-{_file_token(branch)}-{_file_token(ticket_id)}.md"


def _file_token(value: str) -> str:
    token = re.sub(r"[^A-Za-z0-9-]", "", value.replace("/", "-"))
    if token:
        return token
        raise GateError("No sirve como nombre de archivo")


def _write_and_exit(
    verdict: dict,
    threshold: float,
    report_path: Path,
    inputs: GateInputs,
) -> int:
    report = render_report(verdict, threshold)
    report_path.write_text(report)
    folder = _save_temp(verdict, report, inputs)
    print(report_path)
    print(folder)
    _show_report(report)
    return exit_code(distance_of(verdict), threshold)


def _save_temp(verdict: dict, report: str, inputs: GateInputs) -> Path:
    folder = _temp_folder(inputs)
    _write_json(folder / "comparison.json", verdict)
    (folder / "report.md").write_text(report)
    return folder


def _temp_folder(inputs: GateInputs) -> Path:
    name = report_filename(inputs.branch, inputs.ticket_id, datetime.now())
    folder = _temp_root() / name.removesuffix(".md")
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _temp_root() -> Path:
    return Path(tempfile.gettempdir()) / "pr_gate"


def _write_json(path: Path, verdict: dict) -> None:
    text = json.dumps(verdict, ensure_ascii=False, indent=2) + "\n"
    path.write_text(text)


def _show_report(report: str) -> None:
    Console().print(Markdown(report))


def _linear_key() -> str:
    key = os.environ.get("LINEAR_API_KEY", "").strip()
    if not key:
        raise GateError("Falta LINEAR_API_KEY")
    return key


def _require_model_key() -> None:
    key = required_api_key(model_name())
    if _key_present(key):
        return
    raise GateError(f"Falta {key}")


def _key_present(key: str) -> bool:
    if not key:
        return True
    return bool(os.environ.get(key, "").strip())


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
