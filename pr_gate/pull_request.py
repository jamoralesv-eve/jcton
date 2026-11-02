import json
import os
import re
import subprocess
from dataclasses import dataclass

from pr_gate.errors import GateError

_LINK = re.compile(
    r"^https?://github\.com/"
    r"(?P<owner>[A-Za-z0-9_.-]+)/"
    r"(?P<repo>[A-Za-z0-9_.-]+)/"
    r"pull/(?P<number>\d+)"
)
_TICKET = re.compile(r"(?<![A-Za-z])([A-Za-z]{3})[- ](\d{4})(?!\d)")


@dataclass(frozen=True)
class PullRequestLink:
    owner: str
    repo: str
    number: int
    url: str


@dataclass(frozen=True)
class PullRequest:
    link: PullRequestLink
    branch: str


@dataclass(frozen=True)
class PreparedReview:
    ticket_id: str
    branch: str


def prepare_pull_request(url: str) -> PreparedReview:
    request = fetch_pull_request(parse_link(url))
    ticket_id = ticket_from_branch(request.branch)
    checkout_pull_request(request)
    return PreparedReview(ticket_id, request.branch)


def parse_link(url: str) -> PullRequestLink:
    match = _LINK.match(url.strip())
    if match is None:
        raise GateError(f"No es un link de pull request: {url}")
    return _link_from(match, url.strip())


def ticket_from_branch(branch: str) -> str:
    match = _TICKET.search(branch)
    if match is None:
        raise GateError(f"La rama no tiene un ticket de Linear: {branch}")
    return f"{match.group(1).upper()}-{match.group(2)}"


def fetch_pull_request(link: PullRequestLink) -> PullRequest:
    payload = _gh_json(["pr", "view", link.url, "--json", "headRefName"])
    return _loaded(link, payload)


def checkout_pull_request(request: PullRequest) -> None:
    _gh(["pr", "checkout", request.link.url])


def _link_from(match: re.Match[str], url: str) -> PullRequestLink:
    return PullRequestLink(
        match.group("owner"),
        match.group("repo").removesuffix(".git"),
        int(match.group("number")),
        url,
    )


def _loaded(link: PullRequestLink, payload: dict) -> PullRequest:
    branch = _required(payload, "headRefName", "El pull request no tiene rama")
    return PullRequest(link, branch)


def _required(payload: dict, key: str, message: str) -> str:
    value = str(payload.get(key) or "").strip()
    if value:
        return value
    raise GateError(message)


def _gh_json(args: list[str]) -> dict:
    payload = _parsed_json(_gh(args))
    if isinstance(payload, dict):
        return payload
    raise GateError("gh no devolvió JSON")


def _parsed_json(raw: str) -> object:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as error:
        raise GateError("gh no devolvió JSON") from error


def _gh(args: list[str]) -> str:
    completed = _run_gh(args)
    if completed.returncode == 0:
        return completed.stdout
    raise GateError(_gh_detail(completed))


def _run_gh(args: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["gh", *args],
            check=False,
            capture_output=True,
            text=True,
            env=_gh_env(),
        )
    except FileNotFoundError as error:
        raise GateError("Falta el comando gh") from error


def _gh_env() -> dict[str, str]:
    env = dict(os.environ)
    env["GH_PROMPT_DISABLED"] = "1"
    return env


def _gh_detail(completed: subprocess.CompletedProcess[str]) -> str:
    detail = completed.stderr.strip() or completed.stdout.strip()
    if detail:
        return detail
    return "gh falló"
