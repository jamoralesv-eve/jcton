# pr_gate

Command-line check that compares a GitHub pull request with its Linear ticket.

Python collects the diff and the real ticket. A two-step agent then scores how far the change is from that ticket. Python, not the model, applies the threshold and exits 0 or 1.

```
gh + git + Linear API
        │  diff and real ticket, no model
        ▼
SequentialAgent
        ├─ ticket_rebuilder      writes a ticket from the diff only
        └─ semantic_comparator   scores the real ticket against that reconstruction
        ▼
Python
        distance > threshold → exit 1
        otherwise            → exit 0
```

## Why the split

The rebuilder never sees the real ticket. Its instruction interpolates only `{pr_diff}`. The real ticket sits in session state under `original_ticket` and is interpolated only into the comparator instruction. If the rebuilder could read the ticket, it would copy it and the comparison would be meaningless.

The comparator returns a `Verdict`: a distance from 0 to 1, missing requirements, unrequested behavior changes, a justification, and a confidence of `low`, `medium`, or `high`. `decide.py` fails the run when `overall_distance` is greater than the threshold. A distance equal to the threshold passes. Confidence is printed in the report and does not affect the exit code.

Both agents use `gemini-2.5-flash` at temperature 0, with `include_contents="none"`, so each call is driven by its instruction and the session values that instruction names. The agents have no tools. Comments inside the diff are treated as untrusted data.

## Layout

| Module | Role |
|---|---|
| `pr_gate/cli.py` | Arguments, keys, exit codes |
| `pr_gate/collect.py` | Pull request, filtered diff, Linear ticket |
| `pr_gate/agent.py` | `LlmAgent`s and the `SequentialAgent` |
| `pr_gate/prompts.py` | Instructions. No literal braces, because ADK treats `{name}` as session state |
| `pr_gate/run.py` | `Runner` and an in-memory session |
| `pr_gate/decide.py` | Threshold and `report.md` |

`pr_gate/__init__.py` imports the agent module so `adk web` can load `root_agent` later.

## Run

From the repository that contains the pull request, with `gh` authenticated:

```bash
pip install -r requirements.txt
export LINEAR_API_KEY=...
export GOOGLE_API_KEY=...
python -m pr_gate
```

The command uses the open pull request for the current branch. It writes `report.md` and prints the same report.

```bash
python -m pr_gate --pr 123 --repo owner/name
python -m pr_gate --ticket ENG-123
python -m pr_gate --threshold 0.5 --report report.md
python -m pr_gate --print-inputs
```

`--print-inputs` prints the ticket and the diff, then exits. It does not call the model.

`DISTANCE_THRESHOLD` overrides the default threshold of `0.5` when `--threshold` is omitted.

## What gets collected

`gh pr view` supplies the pull request body, base branch, and head branch. The diff is `git diff origin/<base>...HEAD`, excluding lockfiles and `*.snap`, and cut at 60,000 characters.

The Linear issue id is resolved in this order:

1. `--ticket`
2. A `linear.app/<workspace>/issue/ENG-123` URL in the pull request body
3. The head branch name, for example `alejandro/eng-123-title`

One id is required. Several ids stop the run until `--ticket` picks one. The ticket text is the Linear title plus description, loaded with the GraphQL `issue(id:)` query. Issue comments are not included.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Distance is within the threshold |
| 1 | Distance is above the threshold |
| 2 | The check could not run: missing key, no ticket, empty diff, or an API failure |

## Tests

```bash
python -m unittest tests.test_pr_gate
```

The tests cover ticket resolution, a real git diff that drops lockfiles, Linear response parsing, the threshold, and the CLI exit codes. They do not call Gemini or Linear.
