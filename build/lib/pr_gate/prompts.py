from pathlib import Path


class PromptMissing(Exception):
    """A prompt markdown file is missing or empty."""


_DIRECTORY = Path(__file__).resolve().parent / "prompts"
_REBUILD_FILE = "rebuild.md"
_COMPARE_FILE = "compare.md"


def _prompt_path(filename: str) -> Path:
    path = _DIRECTORY / filename
    if not path.is_file():
        raise PromptMissing(f"Falta {filename}")
    return path


def _require_text(filename: str, text: str) -> None:
    if not text:
        raise PromptMissing(f"{filename} está vacío")


def _load(filename: str) -> str:
    text = _prompt_path(filename).read_text(encoding="utf-8").strip()
    _require_text(filename, text)
    return text


REBUILD_PROMPT = _load(_REBUILD_FILE)
COMPARE_PROMPT = _load(_COMPARE_FILE)
