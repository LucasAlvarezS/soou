# Contributing to soou

[English](CONTRIBUTING.md) · [Español](CONTRIBUTING.es.md)

soou is vendor neutral on purpose. It does not belong to any AI provider, and it
should work with whatever agent you actually use. That makes external help
especially valuable: the person who uses an agent we have never touched is the
one who knows how to wire it properly.

Fork it, break it, send it back better.

## The easiest contribution that matters

**Add support for an agent.** No core changes needed, and it helps everyone who
uses that tool.

Every known CLI lives in `RECIPE_REGISTRY` in `soou_core/agentes.py`. A recipe
says which binaries to look for and how to invoke the agent headless, once with
autonomous writing and once read-only, and which restriction its read mode
actually enforces:

```python
"your-agent":  {"bins": ["your-agent"],
                "model_flag": "-m",
                "write": ["run", "--auto", "{prompt}"],
                "read":  ["run", "--read-only", "{prompt}"],
                "lectura": "aprobacion_cli"},
```

`{prompt}` is replaced with the task; with `"prompt_via": "stdin"` it goes
through standard input instead, which is preferred. `lectura` is what stops a
read-only run from writing: `"sandbox_os"` if the operating system enforces it,
`"aprobacion_cli"` if the agent's own permissions do, `"no_verificada"` if you
are not sure (soou then rejects read-only runs for it). If the agent has no read
mode, set `"read": None`: never reuse the write command. Validate it with
`python soou.py agents --probe`, which launches the binary for real instead of
just checking `PATH`.

If the agent has no CLI but exposes an OpenAI-compatible API, nothing needs to be
added at all: it connects through the `http` type in `soou.config.json`. If it
speaks ACP, use the `acp` type: soou then decides every permission itself.

Before opening a pull request for an agent, run
`python soou.py agents --conformance NAME`. It goes through the same executor as
`run`, in a clean temporary repo and without brain notes, and checks that the
agent starts, reads (or is rejected when its read mode is not verified), leaves
the workspace untouched when asked to write in read mode, writes in write mode,
stops on a short timeout and returns a result that follows the contract. It
launches the agent up to 4 times, so it costs real usage. It exits with code 1
if a case fails. The same checks run in CI against the fake agents in
`tests/fixtures/`.

Other welcome contributions: a harvester for another client's transcripts, a
translation, a bug with a failing test attached, or a rough edge you hit during
install.

## Principles a change should respect

These are not style preferences. They are why the project works the way it does.

- **The core has zero dependencies.** Standard library only. If something needs a
  package, it goes behind an optional import and degrades gracefully, the way the
  interface falls back from `pywebview` to the browser.
- **Markdown is the source of truth.** `soou.db` is a derived index. Anything
  that cannot be rebuilt by deleting the database and running `reindex` is a bug.
- **Additive by default.** Distillation never deletes. Only `compact` removes
  material, it archives instead of deleting, and it simulates unless you pass
  `--apply`.
- **`pinned: true` is sacred.** It marks a human correction. Nothing overwrites
  it. When new information contradicts it, the answer is a separate note, linked.
- **The user's knowledge is theirs.** `wiki/`, `sources/` and `sessions/` never
  go to the repository. Notes reach an agent only through the paths listed in
  the README's Privacy section, and every new path must respect `private` notes
  and `brain_context`.
- **Never promise what the code does not enforce.** If a guarantee depends on
  an agent's own permissions, say so. A read mode that cannot be enforced is
  rejected, not assumed.

## Code layout

`soou.py` is the command line: argument parsing and the small commands. The work
lives in `soou_core/`, in layers. A module only imports from the layers below it,
which is what keeps the imports free of cycles.

| Layer | Module | What lives there |
|---|---|---|
| base | `rutas.py` | Every data path (`ROOT`, `WIKI`, `SESSIONS`, `DB_PATH`...), in one place |
| | `texto.py` | Tokenizer, accent folding, the Spanish/English stemmer, BM25, TF-IDF, TextRank |
| | `saneado.py` | Scrubbing of injected client blocks and secrets before a session hits disk |
| | `contrato.py` | The internal contract: `TaskRequest`, `TaskResult`, states, error codes, verification policies |
| | `agentes.py` | Adapters for the agent CLIs, the recipe registry, safe process launch |
| | `acp.py` | The minimal ACP v1 client (JSON-RPC over stdio) and its adapter |
| | `vigencia.py` | Provenance and validity fields: replacements, the validity lint, run verification |
| | `plantillas.py`, `consola.py` | The graph HTML; terminal brand and spinner |
| core | `config.py` | `soou.config.json`, its defaults, models and the daily run budget |
| | `indice.py` | Notes, the SQLite index, the graph and hybrid retrieval |
| | `router.py` | Task classification and the router, plus `soou eval` |
| | `contexto.py` | The one context package: project, isolation, budget, `context --explain` |
| | `registro.py` | Building the available agents from the config |
| memory | `sesiones.py` | Capture and harvesting of sessions (L0) |
| | `destilado.py` | Level A and Level B distillation, sync |
| | `compactar.py` | `compact`: archiving, log rotation, link cleanup |
| top | `ejecutor.py` | The single executor: candidates, one attempt (budget, checkpoint, verification, trace), fallback, `soou trace` |
| | `harness.py` | Orchestration on top of the executor: worktrees, validators, plans, `run` |
| | `conformidad.py` | `agents --conformance`: checks an agent through the executor |
| | `evaluacion.py` | `eval --tareas`: the value of memory over the fictional corpus, with and without it |
| | `instalacion.py` | `init`, `status`, `uninstall`: hooks, MCP, scheduled sync |
| | `ui.py` | The interface API and its local server |
| | `soou_mcp.py` | The MCP server (JSON-RPC over stdio) |

Every call to an agent goes through `ejecutor.intentar`: it reserves budget,
takes a checkpoint of the worktree, runs the agent, applies the minimum
verification (which holds at every level), writes a trace and, if the attempt
failed, returns the worktree exactly to its checkpoint. Do not add another loop
that calls `adapter.run` directly.

The memory layer never imports the top layer. When distillation needs an agent
(`sync --deep`, `persona`), the CLI passes it an `ejecutor.InvocadorDestilado`:
the executor makes the calls and the distill module only builds the batch and
reads the result. Distillation edits the real wiki and its work is additive, so
those attempts keep partial work instead of returning to a checkpoint.
Distillation runs at the `trivial` level with the model from
`config.modelo_destilado` (`models.<agent>.distill`, else `trivial`); `persona`
stays at `complex`. A test
(`tests/test_destilado_ejecutor.py`) checks the imports.

Two rules keep the tests honest:

- **Data paths are read from `rutas` at call time**, never copied at import. To
  point soou somewhere else, assign `soou.rutas.WIKI = ...` (or call
  `rutas.configurar(root)`); `soou.WIKI` still reads, it just does not write.
- **Across modules, call `module.name`, not `from module import name`.** Then
  `patch.object(soou.config, "load_config", ...)` reaches every caller. Patch
  the module that owns the function. `soou.py` re-exports the names for reading.
- **A test never launches a real agent or writes to your data.** Use fake
  adapters (`tests/fixtures/agente_falso.py`) and patch `sesiones._runs_dir` and
  `config._budget_reserve`.

## Running the tests

```bash
python -m unittest discover -s tests
```

A few seconds, no network and no external services. CI runs them on Linux,
macOS and Windows on every push and pull request. If you touch retrieval, also
run `soou eval`: `tests/test_eval_retrieval.py` holds a quality floor.

New behaviour needs a test. The bug that cost this project the most was a
collision that silently collapsed several sessions into one note, and the test
suite did not catch it because nothing covered that path. Tests earn their keep
when they cover what can fail quietly.

Destructive paths deserve tests for what must **not** happen: that `pinned` is
respected, that a simulation writes nothing, that archived material still exists.

## Pull requests

- One topic per pull request.
- Explain the **why** in the description. The what is already in the diff.
- Make sure the suite is green.
- Spanish or English are both fine, in commits and in the description.

If you are unsure whether an idea fits, open an issue first and ask. That is
cheaper for everyone than a rejected pull request.

## Reporting a bug

Include what you expected, what happened, and the output of:

```bash
python soou.py status
python soou.py agents
```

Do not paste notes from your brain. If a note is involved, describe its shape
rather than its content.

## License

Contributions are released under the [MIT license](LICENSE), the same as the
project.
