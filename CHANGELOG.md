# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added: measuring the value of memory

- `soou eval --tareas` runs a fixed, fictional corpus (`tests/fixtures/corpus/`:
  32 notes of three made-up projects, 13 tasks, toy repos with hidden tests)
  with and without memory. It reports tasks solved, answers that repeat a
  replaced fact, answers citing a note they received (and citations of notes
  that appear nowhere in their context), leaked secrets, context size, notes
  that should not have entered, and a simulated plan interruption and resume.
- A read task is solved when every expected fact appears; a write task when the
  hidden test passes. The test is copied into the repo only after the agent
  finishes.
- The default `eco` agent returns its prompt. It is a deterministic CI check of
  context assembly and of the evaluator; it solves no task and measures no agent. `--agente NAME` (repeatable) uses real agents,
  asks for confirmation and `--guardar` writes the result to
  `benchmarks/tareas/`.
- Both conditions launch the same agent with the same flags, permissions and
  tools; only the context in the prompt changes. With `SOOU_EVAL=1` (also set by
  `agents --conformance`), soou's hooks do nothing, Claude Code runs with
  `--strict-mcp-config`, hooks disabled and no saved session, and Codex runs
  without the soou MCP server and with `--ephemeral`.
- The report lists the instruction files an agent would load by itself and any
  file in the real brain that holds corpus data. Write rows keep the agent's
  diff and the test output; results are saved after every call, and temp paths
  appear as `<tmp>`.
- First results and their conditions are in `benchmarks/tareas/`.

### Added: provenance and validity

- Optional frontmatter fields (`soou_core/vigencia.py`): `source_session`,
  `source_run`, `recorded_at`, `status` (`propuesta`, `vigente`, `reemplazada`),
  `supersedes` / `superseded_by` and `verification` (`tests_passed`, `revisado`,
  `sin_verificar`, `afirmado_por_agente`). Notes without them work as before.
- Level A writes provenance and verification from the session header; the
  Level B prompt asks for them and forbids claiming `tests_passed` or `revisado`
  without the session saying so.
- The `run` capture writes a machine-readable `Verificacion:` line:
  `tests_passed` only when a functional test passed. Notes written over MCP are
  recorded as `afirmado_por_agente`. Unverified claims reach an agent tagged
  `[sin comprobar]`, inside the context budget.
- A replacement declared only on the new note (`supersedes`) now also keeps the
  old one out of the context.
- `lint` (and `brain_health`) report broken `supersedes` chains, notes still
  current although replaced, two current notes replacing the same one, cycles
  and unknown values.
- `soou provenance [--apply]`: optional migration for Level A notes generated
  before these fields; preview by default, never touches a `pinned` note.

### Added: one context package

- `soou_core/contexto.py` builds what every path hands to an agent. The recall
  hook, `run`, `run --plan` and the MCP tools used to have three versions with
  different rules. It puts the project summary first when the working folder
  belongs to a known project, then the relevant notes, within `context_budget`
  (1600 characters by default, about what `run` sent before).
- Private notes and replaced notes (`status: reemplazada` or `superseded_by`)
  never get in. Notes of another project stay out unless they are the most
  relevant one or the task names that project. A note belongs to a project when
  it links to that project's note; the persona and notes linking to three or
  more projects are general. Only seeds that pass these filters bring their
  graph neighbours.
- `soou context "task" --explain` shows what an agent would get, why each note
  got in, its provenance, and why the rest stayed out. `soou context` without a
  task is still the SessionStart hook, which now detects the project the same
  way, by path component (`Api` no longer matches inside
  `Api-Movil`).
- The `run --plan` planner now goes through the executor in read mode, like
  every other agent call. It used to call the first agent directly, skipping
  the budget and the traces.

### Added: agents over ACP

- `soou_core/acp.py`, a minimal Agent Client Protocol v1 client in the standard
  library: `initialize`, `session/new`, `session/prompt`, progress through
  `session/update`, `session/cancel`, permission requests and the text file
  methods. Configure an agent with `"type": "acp"` and its `command`.
- soou answers every permission request by mode: read mode allows only read
  tools inside the workspace, write mode allows tools inside the workspace and
  never outside, and "always allow" is never chosen. The new read restriction
  `cliente_acp` counts as an enforced read mode.
- A timeout sends `session/cancel`, answers pending permission requests as
  cancelled, then stops the process tree. `refusal` maps to `E_NO_PUDO`, output
  that is not ACP and an unsupported protocol version to `E_SALIDA_INVALIDA`.
- `agents --probe` on an ACP agent does the `initialize` handshake only.
- If the agent offers session modes, soou sets one with `session/set_mode`: a
  read-only mode for read runs, a workspace mode for writes, never full access.
  The first real run showed why: `codex-acp` starts in a mode that edits
  without asking, so a read run wrote a file until the mode was set.
- Verified with `@agentclientprotocol/codex-acp` 2.5.0, which reuses
  `codex login`: all six conformance cases pass, and a timeout leaves no
  process behind. Also tested against a fake ACP agent in CI.

### Added: agent manifests and conformance

- An agent can be declared in its own file, `agents/NAME.json` in the data
  folder, with the same fields as an entry in `soou.config.json`. Every
  declaration is validated on load. An invalid one is not built and
  `soou agents` says why. An unknown field only produces a warning, and fields
  starting with `_` are comments. A file never replaces an agent of the same
  name in the config.
- `soou agents --conformance NAME` checks an agent through the executor, in a
  clean temporary repo without brain notes: start, read (or rejection when its
  read mode is not verified), no writes in read mode, writes in write mode,
  timeout and a result that follows the contract. It exits with code 1 when a
  case fails. Its attempts are traced at their own level so they do not affect
  the learned agent order.

### Changed: one executor for every agent call

- `run`, every plan step and the dual mode used to have their own attempt loop,
  each with its copy of budget, model, trace, success check and cleanup. They
  now go through one executor (`soou_core/ejecutor.py`) and one internal
  contract (`soou_core/contrato.py`): a `TaskRequest` in, a `TaskResult` out,
  with stable states (`completado`, `sin_cambios`, `fallo`, `timeout`,
  `rechazado`, `cancelado`) and error codes.
- Distillation (`sync --deep`, `distill`, `persona`) now asks the executor for
  its agents instead of calling them directly. It shares the daily budget, the
  traces (`soou trace` shows `distill` and `persona` attempts), the minimum
  verification and the learned agent order with `run`. `persona` gained a
  fallback to the next agent. A failed attempt keeps the notes it already wrote,
  since distillation is additive. Without budget nothing is launched or
  announced, and only Level A runs.
- A minimum verification holds at every level: the process ended well, there is
  an answer or a change, the agent does not say it could not do the work, a
  read-only run did not touch the workspace, and a write produced changes.
  "Verified" is derived from the checks a named policy requires. A validator
  that did not run is recorded as not run, never as passed.
- A failed attempt returns the worktree exactly to the checkpoint taken before
  it, including commits the agent made, files it deleted and files it created.
  Earlier steps and your own uncommitted changes are kept; there is no blanket
  `git clean` anymore.
- A timeout kills the agent's whole process tree (children and grandchildren)
  before cleaning up or trying the next agent.
- The daily budget is reserved under a file lock before an agent is launched,
  so concurrent runs cannot both pass on the same balance. A lock left behind
  by a dead process is recovered. It is documented as a cap on invocations, not
  on money.
- `soou trace [RUN_ID]` shows every attempt of a run: agent, model, state,
  error code, checks, duration, and why the next agent was tried.
- Run results are captured with their run id (a resumed plan does not capture
  twice) and with the validators' real verdict, after validation.
- One detached launcher for the interface, MCP, the harness and the distill hook.
- The validation verdict is graded. Only a passing test suite counts as
  functional verification; the diff, compiling and lint are recorded as passed
  checks, but the result reads "without functional verification". A validator
  that could not run (for example `npm test` without `node_modules`) used to be
  recorded as passed; it is now recorded as not run.
- `sessions --importar ID --proyecto FOLDER` imports one session that automatic
  capture dropped, attributed to a project that must pass the same capture
  policy. The header records the original folder and that the import was
  explicit. The allowlist is never widened to make up for a client started in
  the wrong folder.

### Security

- Read-only runs can no longer reach an agent's write command. A recipe with no
  read mode used to fall back to its write command; goose, amp and openhands
  had the same command for both. Each adapter now declares which restriction
  its read mode actually enforces (OS sandbox, the agent's own approvals, text
  only, or not verified), and read-only runs reject the rest without launching
  them. Aider reads with `--dry-run`.
- Claude Code in read mode no longer runs with `acceptEdits`, and its write
  tools are denied explicitly. The prompt goes through standard input.
- On Windows, task text never goes through a `cmd.exe` command line. npm shims
  are launched with `node` directly; any other `.cmd`/`.bat` only accepts
  arguments from a strict allowlist, and free text is rejected.
- Live capture (the `SessionEnd` and `PreCompact` hooks) did not apply the
  capture policy: `capture_roots` and `exclude_projects` only filtered
  harvesting. Both now follow the same rule, and a session with no known folder
  is dropped unless `capture_unknown_cwd` is on.

### Added

- Private notes: `private: true` in the frontmatter, or `private_paths`, keeps a
  note out of recall, MCP (including `brain_orchestrate`), `query`, the `run`
  context and the persona.
  `query --privadas` shows them to you.
- `brain_context: false` stops soou from injecting context on its own;
  `run --no-brain` does it for one run.
- `init --preview` lists every change without making it, `init --minimal`
  prepares only the workspace and the index, and in a terminal `init` asks
  before touching hooks, MCP or the scheduler (`--yes` skips the question).

### Changed

- `soou.py` went from about 5,600 lines to about 1,200: the work now lives in 17
  layered modules under `soou_core/` (see CONTRIBUTING, "Code layout"). Data
  paths live in one place, `soou_core/rutas.py`. Behavior is unchanged: same
  tests, same `soou eval` metrics, same `status`. The split was done block by
  block with a deterministic ast-based mover, run by Codex through
  `soou run --write`, and each block was checked against a dry run before it
  was merged.
- `AGENTS.md` is now a guide for agents working on soou's own code, usable from a
  clean clone. The instructions for agents that use a brain moved to
  `agentes-cerebro.md`, still indexed as `regla-agentes`.
- The docs say what is true: what stays local and what reaches each provider,
  and that the worktree separates changes but is not a sandbox.
- Sessions of agents launched by `soou run` are no longer harvested a second
  time (they are already captured by the run).
- Sessions captured from `run` record whether the result was verified; a
  read-only answer is marked as unverified.
- Every `run` attempt leaves a trace with its reason, not only plan steps.

### Fixed

- Sessions run in the system temp folder are no longer captured. On Windows
  it sits inside your home folder (`AppData\Local\Temp`), so with no
  `capture_roots` the agents launched by `eval --tareas` and `agents
  --conformance` entered the brain with made-up content. A root you list
  inside the temp folder still counts; a broad one such as your home folder
  does not reopen it.
- `note_links` called itself forever when a note came without its index links.
- `lectura_segura` no longer fails on an adapter that does not declare a read
  restriction; such an adapter is simply not a safe reader.
- Resolving the script behind an npm shim built its path with backslashes,
  so on Linux and macOS it was never found. Only CI runners with `node`
  installed showed it.
- An attempt that ended well but failed the minimum verification stayed in the
  `completado` state with an error code. It is now `fallo`, with `E_PERMISO`
  when it touched the workspace in read mode and `E_NO_PUDO` otherwise.

- Found by running the harness on six real tasks: an agent that exited 0 but
  did nothing (for example Codex without a working terminal, answering "I could
  not...") counted as a success. No fallback was tried, a four-step plan was
  marked complete with zero changes, and an invented answer was stored in the
  brain. A write with no changes, or an attempt with no changes where the agent
  says it could not do the work, now fails over to the next agent, both in
  `run` and in each plan step.
- Project tests run by the validators no longer inherit soou's internal
  variables (`BRAIN_DISTILL` turned capture off and made five tests fail).
- The interface server answered 403/404 without reading the request body, and
  on Windows the connection was sometimes aborted (`WinError 10053`) instead of
  returning the status.

## [0.9.0] - 2026-11-05

First public release candidate.

### Added

- `pipx install` support. Installed, your data lives in `~/.soou` (or
  `SOOU_HOME`); a git checkout keeps working with the data next to the code.
- `soou eval`: retrieval quality with known-answer queries (hit@1, recall@k,
  MRR), split between lexical queries and paraphrases. `--generar N` builds a
  set from your own notes. A fixed set guards a quality floor in CI.
- Harness validators now run the project's real checks: npm lint, typecheck and
  test, pytest or unittest, cargo, go and make. A repository can declare its own
  in `.soou.json`.
- Per-step checkpoints, per-step traces in `runs/trazas.jsonl` (OpenTelemetry
  GenAI attribute names), and conservative learned routing that demotes an agent
  that keeps failing at a level.
- `sessions --limpiar` to scrub existing sessions, and a `PreCompact` hook so long
  sessions are captured before the client compacts them.
- `--version`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, issue and PR templates.

### Changed

- Sessions are bounded on disk: injected client blocks are stripped, common
  secret formats are redacted, repeated messages are dropped and each session is
  capped (`session_max_chars`).
- Search folds accents and applies a light Spanish/English stemmer. On the
  fixture set, paraphrase recall@5 went from 0.00 to 0.42 with no loss on
  lexical queries. The index rebuilds itself when the tokenizer changes.
- `compact` archives gzip-compressed, rotates logs, writes its manifest as it
  goes, and turns links to archived notes into plain text.
- All harness worktrees live under one folder, `runs/`, instead of next to each
  repository.
- CI runs on Linux, macOS and Windows.

### Fixed

- A non-absolute working directory could be treated as being under your home
  folder (for example a Windows path read on Linux), so a foreign session was
  harvested.
- `compact` reported about 200 session notes as lost after the session id
  scheme changed; `--recuperar` would have duplicated them.
- A session captured on two dates could stop `compact` halfway.
- The classifier sent short action requests ("refactor the router with tests")
  to the trivial tier.
- `recall "text"` waited for hook input on stdin.
- The plan state file was committed into run branches.
