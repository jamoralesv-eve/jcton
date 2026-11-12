"""
recap_notes.py: la narrativa que escribe el agente para el recap.

El agente entrega un JSON. Aquí se valida contra los hechos: una ruta que no
está en el diff se marca, nunca se oculta, porque suele ser una alucinación.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from pr_gate.errors import GateError

LEVELS = ("alto", "medio", "bajo")
KNOWN_KEYS = {"title", "summary", "intent", "review_order", "risks", "file_notes",
              "verified", "not_verified", "questions", "author"}


@dataclass
class OrderItem:
    path: str
    why: str
    known: bool = True


@dataclass
class RiskNote:
    level: str
    title: str
    detail: str = ""
    path: str | None = None
    line: int | None = None
    known: bool = True


@dataclass
class RecapNotes:
    title: str = ""
    summary: str = ""
    intent: str = ""
    author: str = ""
    review_order: list = field(default_factory=list)
    risks: list = field(default_factory=list)
    file_notes: dict = field(default_factory=dict)
    verified: list = field(default_factory=list)
    not_verified: list = field(default_factory=list)
    questions: list = field(default_factory=list)
    problems: list = field(default_factory=list)   # avisos de validación, se muestran en el HTML

    @property
    def empty(self) -> bool:
        return not (self.summary or self.intent or self.review_order or self.risks
                    or self.file_notes or self.verified or self.not_verified or self.questions)


def load_notes(path: str | None, known_paths: set[str]) -> RecapNotes:
    if not path:
        return RecapNotes()
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise GateError(f"No existe el archivo de notas: {path}") from error
    except json.JSONDecodeError as error:
        raise GateError(f"Las notas no son JSON válido: {error}") from error
    return parse_notes(raw, known_paths)


def parse_notes(raw: object, known_paths: set[str]) -> RecapNotes:
    if not isinstance(raw, dict):
        raise GateError("Las notas tienen que ser un objeto JSON")
    notes = RecapNotes(
        title=_text(raw, "title"),
        summary=_text(raw, "summary"),
        intent=_text(raw, "intent"),
        author=_text(raw, "author"),
        verified=_strings(raw, "verified"),
        not_verified=_strings(raw, "not_verified"),
        questions=_strings(raw, "questions"),
    )
    unknown = sorted(set(raw) - KNOWN_KEYS)
    if unknown:
        notes.problems.append("Claves desconocidas ignoradas: " + ", ".join(unknown))
    notes.review_order = [_order(item, known_paths, notes) for item in _list(raw, "review_order")]
    notes.risks = [_risk(item, known_paths, notes) for item in _list(raw, "risks")]
    notes.file_notes = _file_notes(raw, known_paths, notes)
    return notes


def template(paths: list[str]) -> dict:
    """Esqueleto que el agente rellena. Las rutas ya vienen del diff."""
    return {
        "title": "",
        "author": "",
        "summary": "",
        "intent": "",
        "review_order": [{"path": p, "why": ""} for p in paths[:5]],
        "risks": [],
        "file_notes": {p: "" for p in paths},
        "verified": [],
        "not_verified": [],
        "questions": [],
    }


def _text(raw: dict, key: str) -> str:
    value = raw.get(key) or ""
    if not isinstance(value, str):
        raise GateError(f"'{key}' tiene que ser texto")
    return value.strip()


def _list(raw: dict, key: str) -> list:
    value = raw.get(key) or []
    if not isinstance(value, list):
        raise GateError(f"'{key}' tiene que ser una lista")
    return value


def _strings(raw: dict, key: str) -> list[str]:
    items = _list(raw, key)
    if not all(isinstance(item, str) for item in items):
        raise GateError(f"'{key}' tiene que ser una lista de textos")
    return [item.strip() for item in items if item.strip()]


def _order(item: object, known: set[str], notes: RecapNotes) -> OrderItem:
    if not isinstance(item, dict) or not isinstance(item.get("path"), str):
        raise GateError("Cada elemento de 'review_order' necesita 'path'")
    path = item["path"].strip()
    order = OrderItem(path=path, why=str(item.get("why") or "").strip(), known=path in known)
    if not order.known:
        notes.problems.append(f"review_order menciona una ruta que no está en el diff: {path}")
    return order


def _risk(item: object, known: set[str], notes: RecapNotes) -> RiskNote:
    if not isinstance(item, dict):
        raise GateError("Cada riesgo tiene que ser un objeto")
    level = str(item.get("level") or "").strip().lower()
    if level not in LEVELS:
        raise GateError(f"Nivel de riesgo inválido: {level or '(vacío)'}. Usa alto, medio o bajo")
    title = str(item.get("title") or "").strip()
    if not title:
        raise GateError("Cada riesgo necesita 'title'")
    path = item.get("path")
    path = path.strip() if isinstance(path, str) and path.strip() else None
    line = item.get("line")
    line = line if isinstance(line, int) and line > 0 else None
    risk = RiskNote(level, title, str(item.get("detail") or "").strip(), path, line,
                    known=path is None or path in known)
    if not risk.known:
        notes.problems.append(f"Un riesgo menciona una ruta que no está en el diff: {path}")
    return risk


def _file_notes(raw: dict, known: set[str], notes: RecapNotes) -> dict[str, str]:
    value = raw.get("file_notes") or {}
    if not isinstance(value, dict):
        raise GateError("'file_notes' tiene que ser un objeto ruta -> texto")
    result = {}
    for path, text in value.items():
        if not isinstance(text, str) or not text.strip():
            continue
        if path not in known:
            notes.problems.append(f"file_notes menciona una ruta que no está en el diff: {path}")
            continue
        result[path] = text.strip()
    return result
