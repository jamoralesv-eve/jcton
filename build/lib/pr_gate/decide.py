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


_DIMENSIONS = ("topic", "factual", "conclusions", "detail")


def render_report(verdict: dict, threshold: float) -> str:
    blocks = [
        _heading(verdict, threshold),
        _alignment_table(verdict),
        _dimensions_table(verdict),
        _justification(verdict),
    ]
    return "\n\n".join(blocks) + "\n"


def _heading(verdict: dict, threshold: float) -> str:
    distance = distance_of(verdict)
    confidence = verdict.get("confidence") or ""
    return "\n".join([
        "## Comparación",
        "",
        f"**Distancia:** {distance:.2f} (umbral {threshold})",
        f"**Confianza:** {confidence}",
    ])


def _alignment_table(verdict: dict) -> str:
    rows = [_alignment_cells(item) for item in _alignment(verdict)]
    table = _table(["Documento A", "Documento B", "Relación"], rows)
    return "## Alineación\n\n" + table


def _dimensions_table(verdict: dict) -> str:
    scores = _dimensions(verdict)
    rows = [[name, _score(scores, name)] for name in _DIMENSIONS]
    return "## Dimensiones\n\n" + _table(["Dimensión", "Puntaje"], rows)


def _justification(verdict: dict) -> str:
    return str(verdict.get("justification") or "")


def _object_from_json(raw: str) -> dict:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        raise GateError("El veredicto no es JSON") from error
    if isinstance(parsed, dict):
        return parsed
    raise GateError("El veredicto no es un objeto")


def _alignment(verdict: dict) -> list:
    rows = verdict.get("alignment")
    if not isinstance(rows, list):
        raise GateError("El veredicto no trae alignment")
    return rows


def _alignment_cells(item: object) -> list[str]:
    if not isinstance(item, dict):
        raise GateError("alignment no es una lista de objetos")
    return [_text(item, key) for key in ("a", "b", "relation")]


def _text(item: dict, key: str) -> str:
    if key not in item:
        raise GateError(f"alignment no trae {key}")
    return str(item[key])


def _dimensions(verdict: dict) -> dict:
    scores = verdict.get("dimensions")
    if not isinstance(scores, dict):
        raise GateError("El veredicto no trae dimensions")
    return scores


def _score(scores: dict, name: str) -> str:
    if name not in scores:
        raise GateError(f"dimensions no trae {name}")
    return str(scores[name])


def _table(headers: list[str], rows: list[list[str]]) -> str:
    lines = [_header(headers), _rule(headers)]
    lines.extend(_row(cells) for cells in rows)
    return "\n".join(lines)


def _header(headers: list[str]) -> str:
    return "| " + " | ".join(headers) + " |"


def _rule(headers: list[str]) -> str:
    return "| " + " | ".join("---" for _ in headers) + " |"


def _row(cells: list[str]) -> str:
    return "| " + " | ".join(_cell(value) for value in cells) + " |"


def _cell(value: str) -> str:
    return value.replace("\n", " ").replace("|", "\\|")
