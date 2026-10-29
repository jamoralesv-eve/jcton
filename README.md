# pr_gate

Command-line check that compares the current branch with its Linear ticket.

Python collects the diff and the real ticket. A two-step agent then scores how far the change is from that ticket. Python, not the model, applies the threshold and exits 0 or 1.

```
git + Linear API
        │  branch diff and real ticket, no model
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

The comparator returns the JSON from `compare.md`: aligned claims, dimension scores from 0 to 10, an overall distance, a justification, and a confidence of `low`, `medium`, or `high`. `decide.py` prints that JSON as tables. It fails the run when `overall_distance` is greater than the threshold. A distance equal to the threshold passes.

Both agents use LiteLLM at temperature 0, with `include_contents="none"`, so each call is driven by its instruction and the session values that instruction names. The default model is `anthropic/claude-sonnet-4-5`. `PR_GATE_MODEL` selects another one, for example `ollama_chat/qwen2.5-coder` or `openai/qwen-coder` with `PR_GATE_API_BASE`. The agents have no tools. Comments inside the diff are treated as untrusted data.

## Layout

| Module | Role |
|---|---|
| `pr_gate/cli.py` | Arguments, keys, exit codes |
| `pr_gate/collect.py` | Current branch diff and Linear ticket |
| `pr_gate/agent.py` | `LlmAgent`s and the `SequentialAgent` |
| `pr_gate/prompts.py` | Loads `pr_gate/prompts/*.md`. `{identifier}` is session state; other braces stay literal |
| `pr_gate/run.py` | `Runner` and an in-memory session |
| `pr_gate/decide.py` | Threshold and the report |

`pr_gate/__init__.py` imports the agent module so `adk web` can load `root_agent` later.

## Install

Anyone with access to this private repository can install the `jcton` command once:

```bash
uv tool install "git+ssh://git@github.com/jamoralesv-eve/jcton.git"
```

Without `uv`:

```bash
pipx install "git+ssh://git@github.com/jamoralesv-eve/jcton.git"
```

To work on this repository itself:

```bash
pip install -e .
```

## Run

From the checkout of the branch to review, `jcton -h` prints the flags and examples:

```bash
export LINEAR_API_KEY=...
export ANTHROPIC_API_KEY=...
jcton -h
```

`ANTHROPIC_API_KEY` is required for the default model. An `ollama` or `ollama_chat` model does not need a provider key. `openai/...` needs `OPENAI_API_KEY`.

The diff is the change on the current branch against the remote default branch (`origin/HEAD`). It writes `reports/<fecha>-<rama>-<ticket>.md` and prints the same report. `--report` sets a different path. The comparison JSON and a copy of the report also go to `$TMPDIR/pr_gate/<fecha>-<rama>-<ticket>/`.

## What gets collected

The diff is `git diff origin/<default>...HEAD`, excluding lockfiles and `*.snap`, and cut at 60,000 characters. `<default>` is the branch `origin/HEAD` points at.

The Linear issue id comes from `--ticket`, or from the current branch name, for example `alejandro/eng-123-title`. A branch without an id needs `--ticket`. The ticket text is the Linear title plus description, loaded with the GraphQL `issue(id:)` query. Issue comments are not included.

## Tests

```bash
python -m unittest tests.test_pr_gate
```

The tests cover ticket resolution, a real git diff that drops lockfiles, Linear response parsing, the threshold, and the CLI exit codes. They do not call the model or Linear.
