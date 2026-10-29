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
ISSUE_URL = re.compile(
    r"linear\.app/[^/\s]+/issue/([A-Za-z][A-Za-z0-9]+-\d+)",
    re.IGNORECASE,
)
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
PR_FIELDS = "number,body,baseRefName,headRefName,url"


@dataclass(frozen=True)
class PullRequest:
    number: int
    body: str
    base: str
    head: str
    url: str


@dataclass(frozen=True)
class GateInputs:
    diff: str
    ticket: str
    ticket_id: str
    pr_url: str
    pr_number: int


@dataclass(frozen=True)
class CollectRequest:
    ticket_id: str | None
    pr_number: str | None
    repo: str | None
    api_key: str


def gather(request: CollectRequest) -> GateInputs:
    pull = pull_request(request.pr_number, request.repo)
    ticket_id = resolve_ticket_id(request.ticket_id, pull.head, pull.body)
    return GateInputs(
        diff=collect_diff(pull.base),
        ticket=fetch_ticket(ticket_id, request.api_key),
        ticket_id=ticket_id,
        pr_url=pull.url,
        pr_number=pull.number,
    )


def resolve_ticket_id(explicit: str | None, branch: str, body: str) -> str:
    if explicit and explicit.strip():
        return explicit.strip().upper()
    found = _dedupe(_detected(branch, body))
    if len(found) == 1:
        return found[0]
    if not found:
        raise GateError("El PR no referencia un ticket de Linear")
    joined = ", ".join(found)
    raise GateError(f"Hay varios tickets: {joined}. Pasa --ticket.")


def branch_ticket_id(branch: str) -> str:
    match = ISSUE_IN_BRANCH.search(branch)
    if match is None:
        raise TicketNotInBranch(branch)
    return match.group(1).upper()


def tickets_from_body(body: str) -> list[str]:
    return [match.group(1).upper() for match in ISSUE_URL.finditer(body)]


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


def pull_request(number: str | None, repo: str | None) -> PullRequest:
    payload = json.loads(_command_output(_pr_command(number, repo)))
    return PullRequest(
        number=payload["number"],
        body=payload.get("body") or "",
        base=payload["baseRefName"],
        head=payload["headRefName"],
        url=payload["url"],
    )


def _detected(branch: str, body: str) -> list[str]:
    found = tickets_from_body(body)
    try:
        return [*found, branch_ticket_id(branch)]
    except TicketNotInBranch:
        return found


def _dedupe(identifiers: list[str]) -> list[str]:
    unique: list[str] = []
    for identifier in identifiers:
        if identifier not in unique:
            unique.append(identifier)
    return unique


def _diff_command(base: str) -> list[str]:
    return ["git", "diff", f"origin/{base}...HEAD", "--", ".", *DIFF_EXCLUDES]


def _pr_command(number: str | None, repo: str | None) -> list[str]:
    command = ["gh", "pr", "view", "--json", PR_FIELDS]
    if number:
        command.append(number)
    if repo:
        command.extend(["--repo", repo])
    return command


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
