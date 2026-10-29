import json
import re
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass

from pr_gate.errors import GateError
from pr_gate.errors import TicketNotInBranch

LINEAR_URL = "https://api.linear.app/graphql"
MAX_DIFF_CHARS = 60_000
TRUNCATION_NOTE = "\n\n[diff truncated]\n"
ISSUE_IN_BRANCH = re.compile(r"(?:^|/)([A-Za-z][A-Za-z0-9]+-\d+)(?:-|/|$)")
ORIGIN_HEAD = "refs/remotes/origin/HEAD"
DIFF_EXCLUDES = [
    ":(exclude)*.lock",
    ":(exclude)package-lock.json",
    ":(exclude)pnpm-lock.yaml",
    ":(exclude)yarn.lock",
    ":(exclude)*.snap",
]
ISSUE_QUERY = """
query Issue($id: String!) {
  issue(id: $id) {
    identifier
    title
    description
    url
  }
}
"""


@dataclass(frozen=True)
class GateInputs:
    diff: str
    ticket: str
    ticket_id: str
    branch: str


@dataclass(frozen=True)
class CollectRequest:
    ticket_id: str | None
    api_key: str


def gather(request: CollectRequest) -> GateInputs:
    branch = current_branch()
    ticket_id = resolve_ticket_id(request.ticket_id, branch)
    return GateInputs(
        diff=collect_diff(default_base()),
        ticket=fetch_ticket(ticket_id, request.api_key),
        ticket_id=ticket_id,
        branch=branch,
    )


def resolve_ticket_id(explicit: str | None, branch: str) -> str:
    if explicit and explicit.strip():
        return explicit.strip().upper()
    return _ticket_from_branch(branch)


def branch_ticket_id(branch: str) -> str:
    match = ISSUE_IN_BRANCH.search(branch)
    if match is None:
        raise TicketNotInBranch(branch)
    return match.group(1).upper()


def current_branch() -> str:
    name = _command_output(["git", "branch", "--show-current"]).strip()
    if name:
        return name
    raise GateError("HEAD no está en una rama")


def default_base() -> str:
    ref = _remote_head()
    return _short_base(ref)


def collect_diff(base: str) -> str:
    diff = _command_output(_diff_command(base))
    if not diff.strip():
        raise GateError("El diff está vacío")
    return limit_diff(diff)


def limit_diff(diff: str) -> str:
    if len(diff) <= MAX_DIFF_CHARS:
        return diff
    return diff[:MAX_DIFF_CHARS] + TRUNCATION_NOTE


def fetch_ticket(identifier: str, api_key: str) -> str:
    issue = _linear_issue(identifier, api_key)
    description = issue.get("description") or ""
    return f"# {issue['identifier']} {issue['title']}\n\n{description}"


def _ticket_from_branch(branch: str) -> str:
    try:
        return branch_ticket_id(branch)
    except TicketNotInBranch as error:
        raise GateError(f"La rama {branch} no tiene un ticket. Pasa --ticket.") from error


def _remote_head() -> str:
    command = ["git", "symbolic-ref", "--short", ORIGIN_HEAD]
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        raise GateError("No está origin/HEAD. El remoto no tiene rama por defecto.")
    return completed.stdout.strip()


def _short_base(ref: str) -> str:
    remote, _separator, name = ref.partition("/")
    if remote == "origin" and name:
        return name
    raise GateError(f"La base remota no es de origin: {ref}")


def _diff_command(base: str) -> list[str]:
    return ["git", "diff", f"origin/{base}...HEAD", "--", ".", *DIFF_EXCLUDES]


def _command_output(command: list[str]) -> str:
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise GateError(detail or f"{command[0]} falló")
    return completed.stdout


def _linear_issue(identifier: str, api_key: str) -> dict:
    try:
        payload = _post_linear(identifier, api_key)
    except urllib.error.URLError as error:
        raise GateError(f"Linear no respondió: {error.reason}") from error
    return _issue_from_payload(payload, identifier)


def _post_linear(identifier: str, api_key: str) -> dict:
    request = urllib.request.Request(
        LINEAR_URL,
        data=_query_body(identifier),
        headers={"Authorization": api_key, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request) as response:
        return json.load(response)


def _query_body(identifier: str) -> bytes:
    body = {"query": ISSUE_QUERY, "variables": {"id": identifier}}
    return json.dumps(body).encode()


def _issue_from_payload(payload: dict, identifier: str) -> dict:
    errors = payload.get("errors") or []
    if errors:
        message = errors[0].get("message") or "Linear rechazó la consulta"
        raise GateError(message)
    issue = (payload.get("data") or {}).get("issue")
    if not issue:
        raise GateError(f"Linear no tiene el issue {identifier}")
    return issue
