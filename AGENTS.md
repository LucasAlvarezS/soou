# AGENTS.md

Guide for coding agents (Codex, Claude Code, Gemini, Cursor...) working on
soou's own code. It assumes a clean clone: no personal brain, no paid agents, no
network. Spanish and English are both fine in code comments and commits.

The instructions for agents that *use* a soou brain are a different file,
`agentes-cerebro.md`. Do not follow them here unless the user asks.

## What this repository is

soou is a local second brain for AI agents (Markdown notes, a SQLite index,
hybrid BM25 + TF-IDF retrieval, an MCP server) plus a harness that classifies a
task and runs it with an agent on a separate git worktree.

- `soou.py`: the command line. `soou_mcp.py`: the MCP server.
- `soou_core/`: the work, in layers. The table is in CONTRIBUTING.md, "Code
  layout". A module only imports from the layers below it.
- `tests/`: standard library `unittest`, no network, a few seconds.

## Run the tests

```bash
python -m unittest discover -s tests
```

They must pass before and after your change. If you touch retrieval, also run
`python soou.py eval`: `tests/test_eval_retrieval.py` holds a quality floor.
Tests never need real agents: fake them with `unittest.mock` or a small script,
as `tests/test_lectura_segura.py` does.

## Rules for a change

- **Standard library only.** No new dependencies.
- **Data paths are read from `soou_core.rutas` at call time.** Never copy them
  at import.
- **Across modules, call `module.name`**, not `from module import name`, so
  `patch.object(soou.<module>, ...)` reaches every caller.
- **Never promise what the code does not enforce.** A read-only mode that an
  agent cannot guarantee is rejected, not assumed. Privacy and isolation claims
  in the docs must match the code and have a test.
- **Destructive paths get tests for what must not happen**: a simulation writes
  nothing, `pinned` notes are not overwritten, private notes never reach an
  agent, archived material still exists.
- **Never commit user data**: `wiki/`, `sources/`, `sessions/`, `soou.db`,
  `soou.config.json`, `runs/`, `.env`. They are in `.gitignore`; keep it that way.
- New behaviour needs a test. Keep the change to one topic.

## Adding an agent

Add a recipe to `RECIPE_REGISTRY` in `soou_core/agentes.py`, declaring
`"lectura"` honestly (see CONTRIBUTING.md). Prefer `"prompt_via": "stdin"`.
Never reuse the write command as the read command.

## If a soou brain is available

When the user has soou installed and its MCP tools (`brain_recall`,
`brain_query`) are available in your session, you may use them for context
about past decisions on this project. They are optional: everything needed to
work on the code is in this repository.
