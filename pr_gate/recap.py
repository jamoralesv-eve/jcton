"""
recap.py: hechos deterministas para el recap visual de un cambio.

Reúne, sin modelo y sin red, lo que el HTML necesita: el diff por archivo,
los cambios estructurales de structdiff, los commits y un puntaje de riesgo
explicable por archivo. El agente escribe la narrativa aparte (recap_notes);
este módulo nunca la inventa ni la modifica.
"""
from __future__ import annotations

import fnmatch
import re
import subprocess
from dataclasses import dataclass, field

from pr_gate.errors import GateError
from pr_gate.structdiff import FileReport
from pr_gate.structdiff import analyze

# Un archivo con más líneas de diff que esto se recorta en el HTML.
MAX_FILE_DIFF_LINES = 1500
HIGH_RISK = 5.0
MEDIUM_RISK = 2.0

GENERATED = ("*.lock", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
             "uv.lock", "poetry.lock", "go.sum", "*.snap", "*.min.js", "*.map")
TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|spec)(/|$)|(_test|\.test|\.spec|_spec)\.\w+$|(^|/)test_[^/]+$")
DOC_PATH = re.compile(r"\.(md|mdx|rst|txt)$|(^|/)docs?/", re.IGNORECASE)

# (patrón, peso, señal). El patrón se busca en la ruta completa, en minúsculas.
PATH_SIGNALS = [
    (re.compile(r"auth|login|password|passwd|secret|token|crypt|permission|rbac|acl|session|oauth|jwt|saml|sso"),
     3.0, "toca autenticación, permisos o secretos"),
    (re.compile(r"migrat|schema|\.sql$|alembic|prisma"),
     3.0, "toca esquema o migraciones de datos"),
    (re.compile(r"(^|/)\.github/|dockerfile|docker-compose|\.tf$|(^|/)(k8s|helm|terraform|deploy|infra)/|\.gitlab-ci|jenkinsfile"),
     2.0, "toca CI, despliegue o infraestructura"),
    (re.compile(r"(^|/)(package\.json|pyproject\.toml|requirements[^/]*\.txt|setup\.py|go\.mod|pom\.xml|build\.gradle|cargo\.toml|gemfile)$"),
     2.0, "cambia dependencias o el build"),
    (re.compile(r"(^|/)\.env|config|settings"),
     1.0, "cambia configuración"),
]


# Patrones en líneas AÑADIDAS. (patrón, peso, señal)
ADDED_SIGNALS = [
    (re.compile(r"""(?i)(secret|passw(or)?d|api[_-]?key|token|private[_-]?key)\w*\s*[:=]\s*["'][^"']{4,}["']"""),
     3.0, "posible credencial escrita en el código"),
    (re.compile(r"(?i)-----BEGIN [A-Z ]*PRIVATE KEY-----"), 4.0, "clave privada en el diff"),
    (re.compile(r"\b(eval|exec)\s*\(|shell\s*=\s*True|pickle\.loads|yaml\.load\((?![^)]*Loader)|os\.system\("),
     2.0, "ejecución dinámica o deserialización insegura"),
    (re.compile(r"(?i)verify\s*=\s*False|InsecureSkipVerify|rejectUnauthorized\s*:\s*false|ssl\._create_unverified"),
     3.0, "desactiva verificación TLS"),
    (re.compile(r"(?i)\b(drop\s+(table|index|column)|truncate\s+table|delete\s+from\s+\w+\s*;)"),
     2.0, "operación destructiva de datos"),
    (re.compile(r"#\s*(noqa|type:\s*ignore|pragma:\s*no cover)|eslint-disable|@ts-ignore|@ts-expect-error|//\s*nolint"),
     1.0, "silencia linters o el chequeo de tipos"),
]
# Señales que solo cuentan dentro de archivos de test.
TEST_ADDED_SIGNALS = [
    (re.compile(r"@(pytest\.mark\.)?skip|unittest\.skip|\b(it|describe|test)\.(skip|only)\(|\bx(it|describe)\(|t\.Skip\("),
     2.0, "salta o aísla tests"),
]
ASSERTION = re.compile(r"\b(assert\w*|expect\s*\(|should\.|require\.\w+\()")


@dataclass
class DiffLine:
    kind: str          # " ", "+", "-", "\\"
    text: str
    old_no: int | None
    new_no: int | None


@dataclass
class Hunk:
    header: str
    lines: list = field(default_factory=list)


@dataclass
class FileDiff:
    path: str
    old_path: str | None = None
    binary: bool = False
    hunks: list = field(default_factory=list)
    truncated: int = 0     # líneas omitidas por MAX_FILE_DIFF_LINES


@dataclass
class Risk:
    score: float
    level: str             # alto, medio, bajo
    signals: list


@dataclass
class FileFacts:
    report: FileReport
    diff: FileDiff
    risk: Risk
    generated: bool
    is_test: bool
    is_doc: bool

    @property
    def path(self) -> str:
        return self.report.path


@dataclass
class Commit:
    sha: str
    subject: str
    author: str


@dataclass
class RecapFacts:
    base: str
    head: str
    merge_base: str
    head_sha: str
    files: list
    commits: list
    warnings: list

    @property
    def added(self) -> int:
        return sum(f.report.added for f in self.files)

    @property
    def deleted(self) -> int:
        return sum(f.report.deleted for f in self.files)

    def paths(self) -> set:
        return {f.path for f in self.files} | {f.report.old_path for f in self.files if f.report.old_path}


# ------------------------------------------------------------------ git

def git(args: list[str], repo: str) -> str:
    completed = subprocess.run(["git", *args], cwd=repo, check=False, capture_output=True)
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip()
        raise GateError(detail or f"git {args[0]} falló")
    return completed.stdout.decode("utf-8", "replace")


def resolve(rev: str, repo: str) -> str:
    return git(["rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"], repo).strip()


def collect_commits(merge_base: str, head: str, repo: str) -> list[Commit]:
    raw = git(["log", "--no-merges", "--format=%h%x1f%s%x1f%an", f"{merge_base}..{head}"], repo)
    commits = []
    for line in raw.splitlines():
        sha, subject, author = (line.split("\x1f") + ["", ""])[:3]
        commits.append(Commit(sha, subject, author))
    return commits


# --------------------------------------------------------- diff unificado

_FILE_START = re.compile(r'^diff --git "?a/(.+?)"? "?b/(.+?)"?$')
_HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def parse_unified(text: str) -> dict[str, FileDiff]:
    """Separa `git diff` en archivos y hunks con números de línea."""
    files: dict[str, FileDiff] = {}
    current: FileDiff | None = None
    hunk: Hunk | None = None
    old_no = new_no = 0
    count = 0
    for line in text.splitlines():
        start = _FILE_START.match(line)
        if start:
            current = FileDiff(path=start.group(2))
            files[current.path] = current
            hunk, count = None, 0
            continue
        if current is None:
            continue
        if line.startswith("rename from "):
            current.old_path = line[len("rename from "):]
            continue
        if line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            current.binary = True
            continue
        match = _HUNK.match(line)
        if match:
            old_no, new_no = int(match.group(1)), int(match.group(2))
            hunk = Hunk(header=line)
            current.hunks.append(hunk)
            continue
        if hunk is None or not line or line[0] not in " +-\\":
            continue
        if count >= MAX_FILE_DIFF_LINES:
            current.truncated += 1
            continue
        count += 1
        kind = line[0]
        if kind == "+":
            hunk.lines.append(DiffLine("+", line[1:], None, new_no))
            new_no += 1
        elif kind == "-":
            hunk.lines.append(DiffLine("-", line[1:], old_no, None))
            old_no += 1
        elif kind == " ":
            hunk.lines.append(DiffLine(" ", line[1:], old_no, new_no))
            old_no += 1
            new_no += 1
        else:
            hunk.lines.append(DiffLine("\\", line[1:].strip(), None, None))
    return files


# ----------------------------------------------------------------- riesgo

def is_generated(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(name, pattern) for pattern in GENERATED)


def is_test(path: str) -> bool:
    return bool(TEST_PATH.search(path.lower()))


def is_doc(path: str) -> bool:
    return bool(DOC_PATH.search(path))


def score_file(report: FileReport, diff: FileDiff | None = None) -> Risk:
    """Puntaje explicable: cada punto viene con la señal que lo produjo."""
    path = report.path.lower()
    if is_generated(path):
        return Risk(0.0, "bajo", ["archivo generado o lockfile"])
    if report.cosmetic_only:
        return Risk(0.0, "bajo", ["solo formato o comentarios"])

    signals: list[tuple[float, str]] = []
    if not is_test(path) and not is_doc(path):
        for pattern, weight, label in PATH_SIGNALS:
            if pattern.search(path):
                signals.append((weight, label))
    churn = report.added + report.deleted
    if churn > 200:
        signals.append((2.0, f"diff grande ({churn} líneas)"))
    elif churn > 50:
        signals.append((1.0, f"diff mediano ({churn} líneas)"))
    if report.status == "D":
        signals.append((1.0, "archivo eliminado"))
    removed = [c for c in report.changes if c.status == "removed"]
    if removed:
        signals.append((min(3.0, 1.0 * len(removed)), f"{len(removed)} símbolo(s) eliminado(s)"))
    signatures = [c for c in report.changes if any(d.startswith("firma:") for d in c.details)]
    if signatures:
        signals.append((min(3.0, 1.0 * len(signatures)), f"{len(signatures)} firma(s) cambiada(s)"))
    large = [c for c in report.changes if c.needs_llm]
    if large:
        signals.append((min(3.0, 1.5 * len(large)), f"{len(large)} cuerpo(s) reescrito(s) a fondo"))
    if report.error:
        signals.append((1.0, "no se pudo analizar la estructura"))
    if diff is not None and not is_doc(path):
        signals += content_signals(diff, is_test(path))

    score = round(sum(weight for weight, _ in signals), 1)
    return Risk(score, _level(score), [label for _, label in signals])


def content_signals(diff: FileDiff, test_file: bool) -> list[tuple[float, str]]:
    """Busca patrones de riesgo en lo que el cambio añade o quita."""
    added = [l.text for h in diff.hunks for l in h.lines if l.kind == "+"]
    removed = [l.text for h in diff.hunks for l in h.lines if l.kind == "-"]
    rules = ADDED_SIGNALS + (TEST_ADDED_SIGNALS if test_file else [])
    found = []
    for pattern, weight, label in rules:
        hits = sum(1 for line in added if pattern.search(line))
        if hits:
            found.append((weight, label if hits == 1 else f"{label} ({hits}×)"))
    if test_file:
        lost = sum(1 for l in removed if ASSERTION.search(l)) - sum(1 for l in added if ASSERTION.search(l))
        if lost > 0:
            found.append((min(3.0, 1.0 * lost), f"{lost} aserción(es) menos en tests"))
    return found


def _level(score: float) -> str:
    if score >= HIGH_RISK:
        return "alto"
    if score >= MEDIUM_RISK:
        return "medio"
    return "bajo"


def global_warnings(files: list[FileFacts]) -> list[str]:
    code = [f for f in files if not (f.is_test or f.is_doc or f.generated)]
    tests = [f for f in files if f.is_test]
    warnings = []
    if code and not tests:
        warnings.append("Cambia código pero no toca ningún test.")
    deleted_tests = [f.path for f in tests if f.report.status == "D"]
    if deleted_tests:
        warnings.append("Elimina tests: " + ", ".join(deleted_tests))
    truncated = [f.path for f in files if f.diff.truncated]
    if truncated:
        warnings.append("Diff recortado en el HTML para: " + ", ".join(truncated))
    return warnings


# ---------------------------------------------------------------- entrada

def gather_recap(base: str, head: str = "HEAD", repo: str = ".") -> RecapFacts:
    resolve(base, repo)
    head_sha = resolve(head, repo)
    merge_base = git(["merge-base", base, head], repo).strip()
    reports = analyze(base, head, repo)
    diffs = parse_unified(git(["-c", "core.quotepath=off", "diff", "-M", "--no-color", "--no-ext-diff",
                               merge_base, head], repo))
    if not reports:
        raise GateError(f"No hay cambios entre {base} y {head}")
    files = [_facts(report, diffs) for report in reports]
    files.sort(key=lambda f: (-f.risk.score, -(f.report.added + f.report.deleted), f.path))
    return RecapFacts(
        base=base,
        head=head,
        merge_base=merge_base,
        head_sha=head_sha,
        files=files,
        commits=collect_commits(merge_base, head, repo),
        warnings=global_warnings(files),
    )


def _facts(report: FileReport, diffs: dict[str, FileDiff]) -> FileFacts:
    diff = diffs.get(report.path) or FileDiff(path=report.path, old_path=report.old_path)
    return FileFacts(
        report=report,
        diff=diff,
        risk=score_file(report, diff),
        generated=is_generated(report.path),
        is_test=is_test(report.path),
        is_doc=is_doc(report.path),
    )


def facts_json(facts: RecapFacts) -> dict:
    """Lo mismo que ve el HTML, sin el diff, para que el agente lo lea."""
    return {
        "base": facts.base,
        "head": facts.head,
        "merge_base": facts.merge_base,
        "head_sha": facts.head_sha,
        "added": facts.added,
        "deleted": facts.deleted,
        "warnings": facts.warnings,
        "commits": [{"sha": c.sha, "subject": c.subject} for c in facts.commits],
        "files": [_file_json(f) for f in facts.files],
    }


def _file_json(f: FileFacts) -> dict:
    return {
        "path": f.path,
        "old_path": f.report.old_path,
        "status": f.report.status,
        "added": f.report.added,
        "deleted": f.report.deleted,
        "risk": {"score": f.risk.score, "level": f.risk.level, "signals": f.risk.signals},
        "generated": f.generated,
        "is_test": f.is_test,
        "cosmetic_only": f.report.cosmetic_only,
        "structural": [_change_text(c) for c in f.report.changes if c.status != "cosmetic"],
    }


def _change_text(change) -> str:
    detail = "; ".join(change.details)
    name = change.qualname
    if change.status == "renamed":
        name = f"{change.old_qualname} -> {change.qualname}"
    if change.status == "moved":
        name = f"{change.qualname} (desde {change.from_path})"
    return f"{change.status} {change.kind} {name}" + (f": {detail}" if detail else "")
