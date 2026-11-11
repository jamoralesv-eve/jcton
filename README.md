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
| `pr_gate/structdiff.py` | Structural diff. Invoked only as `jcton structdiff` |
| `pr_gate/recap.py` | Facts and risk score for `jcton recap`. No model, no network |
| `pr_gate/recap_notes.py` | Loads and validates the agent's notes against the diff |
| `pr_gate/recap_html.py` | Renders the single offline HTML page |

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

To review a GitHub pull request, run `jcton` from a clone of that repository and pass the link. `jcton` reads the head branch with `gh`, takes the Linear issue id from that name, checks out the branch, and then runs the same comparison. The id is three letters, a hyphen or a space, and four digits, for example `rnd-1644` or `rnd 1644`, and it is normalized to `RND-1644`:

```bash
jcton https://github.com/org/repo/pull/123
```

`--ticket` still overrides the id from the branch name. `--print-inputs` checks out the branch and prints the diff and the ticket without calling the model. This path needs the `gh` command, authenticated for that repository.

`ANTHROPIC_API_KEY` is required for the default model. An `ollama` or `ollama_chat` model does not need a provider key. `openai/...` needs `OPENAI_API_KEY`.

The diff is the change on the current branch against the remote default branch (`origin/HEAD`). It writes `reports/<fecha>-<rama>-<ticket>.md` and prints the same report. `--report` sets a different path. The comparison JSON and a copy of the report also go to `$TMPDIR/pr_gate/<fecha>-<rama>-<ticket>/`.

## Structural diff

`jcton structdiff` compares two git revisions, or two files, with tree-sitter. It does not call the model or Linear. Python, JavaScript, TypeScript, Go, and Java are recognized. The markdown report is printed in the terminal.

```bash
jcton structdiff main HEAD
jcton structdiff main HEAD --json
jcton structdiff main HEAD --llm-context
jcton structdiff --files old.py new.py
```

`HEAD` is the default second revision. `--repo` sets the git checkout. `--json` prints the same facts as JSON. `--llm-context` prints only the files whose changes are large enough to need an explanation. `--lang` forces the language when `--files` is used and the extension is not recognized.

## Visual recap

`jcton recap` writes one self-contained HTML file for reviewing a change. It makes no network calls and loads no external fonts, scripts, or styles; the page carries a `default-src 'none'` Content Security Policy.

```bash
jcton recap origin/main HEAD --facts       # facts as JSON, no HTML
jcton recap origin/main HEAD --template    # notes skeleton with the diff's paths
jcton recap origin/main HEAD --notes notes.json --open
```

Python computes the facts: the diff with line numbers and word-level highlights, the `structdiff` changes, the commits, and a risk score per file. Each point of the score comes with the signal that produced it: sensitive paths (auth, secrets, migrations, CI, dependencies), size, removed symbols, changed signatures, heavily rewritten bodies, and patterns in added lines (hardcoded credentials, disabled TLS, `eval`/`shell=True`, destructive SQL, silenced linters, skipped tests, fewer assertions). The score orders the reading. It does not approve anything.

`--notes` adds the agent's narrative: summary, intent, review order, risks with `path` and `line`, per-file notes, what was verified, what was not, and questions. Python validates it against the diff. A path that is not in the diff is shown in red and printed as `aviso:` on stderr, never silently dropped. The page labels the narrative as written by a model and keeps it apart from the computed sections.

The page has a unified or split view, a "reviewed" checkbox per file with progress saved in the browser, and a filter for low-risk files. Without `--out`, the file goes to `$TMPDIR/pr_gate/recaps/`.

The `pr-visual-recap` skill in `.claude/skills/` tells a coding agent how to run the whole review: check out the PR, read the facts, investigate on its own before trusting the PR description, write the notes, and render the page. To use it from other repositories, copy that folder to `~/.claude/skills/`.

## What gets collected

The diff is `git diff origin/<default>...HEAD`, excluding lockfiles and `*.snap`, and cut at 60,000 characters. `<default>` is the branch `origin/HEAD` points at.

When a link is passed, the Linear issue id comes from the pull request branch name: three letters, a hyphen or a space, and four digits, such as `emanuel/rnd-1644-block-agent` or `emanuel/rnd 1644 block agent`. Otherwise it comes from `--ticket`, or from the current branch name, for example `alejandro/eng-123-title`. A branch without an id needs `--ticket`. The ticket text is the Linear title plus description, loaded with the GraphQL `issue(id:)` query. Issue comments are not included.

## Tests

```bash
python -m unittest tests.test_pr_gate tests.test_recap
```

The tests cover ticket resolution, a real git diff that drops lockfiles, Linear response parsing, the threshold, the CLI exit codes, and `jcton structdiff` on two files. `tests.test_recap` covers diff parsing, the risk signals, note validation, HTML escaping, and the `recap` command on a real git repository. They do not call the model or Linear.
