# CLAUDE.md

## Researching this codebase

When asked to research, investigate, look into, or dig into anything in this
codebase, run `entire search "<topic>" --json --compact --limit 5` first and
summarize the prior work before reading source files or grepping.

Entire records the prompts and transcripts behind each change as Checkpoints, so
the recorded intent answers "what was tried and why" in a way the code cannot.
Read that first, then confirm against the source. Label anything you conclude
from code alone as inferred, not recorded.

Notes on driving the CLI from an agent:

- Pass `--json` to anything that might otherwise open the interactive TUI —
  plain `entire search` is a TUI and will hang.
- Pass `--no-pager` so a pager never activates in a subprocess.
- Give `entire checkpoint explain` an explicit `--checkpoint <id>` or
  `--commit <sha>`; with no locator it falls back to an interactive picker.

## Branches

`main` is the demo and requires a pull request with one approval — the rule is
enforced on GitHub, not just documented here. `dev` is the default branch and
where work lands. Release with a merge commit, never a squash, so the two
branches do not diverge.
