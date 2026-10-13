import json

from pydantic import BaseModel

from pr_gate.errors import GateError

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_ERROR = 2


def as_verdict(raw: object) -> dict:
    if isinstance(raw, BaseModel):
        return raw.model_dump()
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        return _object_from_json(raw)
    raise GateError(f"Veredicto inesperado: {type(raw).__name__}")


def distance_of(verdict: dict) -> float:
    try:
        return float(verdict["overall_distance"])
    except (KeyError, TypeError, ValueError) as error:
        raise GateError("El veredicto no trae overall_distance") from error


def exit_code(distance: float, threshold: float) -> int:
    if distance > threshold:
        return EXIT_FAIL
    return EXIT_PASS


def render_report(verdict: dict, threshold: float) -> str:
    distance = distance_of(verdict)
    missing = _bullets(list(verdict.get("missing_in_pr") or []))
    extra = _bullets(list(verdict.get("unrequested_changes") or []))
    confidence = verdict.get("confidence") or ""
    justification = str(verdict.get("justification") or "")
    return "\n".join([
        "## Alineación PR vs ticket",
        "",
        f"**Distancia:** {distance:.2f} (umbral {threshold})",
        f"**Confianza:** {confidence}",
        "",
        "**Falta en el PR:**",
        missing,
        "",
        "**Cambios no solicitados:**",
        extra,
        "",
        justification,
        "",
    ])


def _object_from_json(raw: str) -> dict:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        raise GateError("El veredicto no es JSON") from error
    if isinstance(parsed, dict):
        return parsed
    raise GateError("El veredicto no es un objeto")


def _bullets(items: list[str]) -> str:
    if not items:
        return "- ninguno"
    return "\n".join(f"- {item}" for item in items)
