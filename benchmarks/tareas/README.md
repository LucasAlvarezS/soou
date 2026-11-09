# `eval --tareas` results

Each `.json` file is one run of `python soou.py eval --tareas --agente NAME --guardar`.
Runs are never merged or overwritten: a repetition is a new file. After the fact,
only two things change in a file: the temp folder paths are replaced by `<tmp>`,
and notes are added under `anotaciones`.

## What a run measures

- **Corpus:** fictional and fixed (`tests/fixtures/corpus/`). It has 32 notes on
  three made-up projects (faro, colmena, telar) and 13 tasks:
  - 9 read tasks;
  - 1 task that tries to leak a private note;
  - 3 write tasks, each on a toy repo.
- **Conditions:** each task runs twice with the same agent, flags, permissions and
  tools. The only difference is whether the context package goes in the prompt.
  The answers exist only in the corpus. So the "without memory" condition cannot
  solve the tasks by design: the gap shows whether the agent uses the context it
  receives, not how useful memory is in general.
- **Solved:**
  - a read task, when every expected fact appears in the answer (regular
    expressions in `tareas.json`);
  - a write task, when the hidden test passes. The test is copied in only after
    the agent finishes.
- **Isolation (from the version after these runs on):** soou sets `SOOU_EVAL=1`
  in the agent's environment.
  - soou's own hooks (`context`, `recall`, `session-end`) do nothing while it is
    set.
  - Claude Code runs with `--strict-mcp-config`, `--settings
    {"disableAllHooks":true}` and `--no-session-persistence`.
  - Codex runs with `mcp_servers.soou.enabled=false` and `--ephemeral`.
  - Every other setting comes from the local agent install and is the same in
    both conditions.
  - The report lists any instruction files the agent would load by itself
    (`AGENTS.md`, `CLAUDE.md`).
  - It also lists any files in the real brain that contain corpus data, because
    an agent that reads them through the file system would contaminate the
    "without memory" condition.
- **The `eco` agent** returns its prompt. It is a deterministic CI check of
  context assembly and of the evaluator itself; it solves no task and measures
  no agent. Its numbers are not results.

## Runs on 2026-11-09

| Run | Agent | Version | Status | File |
|---|---|---|---|---|
| 1 | Codex | codex-cli 0.166.0 | incomplete: evaluator crash, nothing saved | `2026-11-09-codex-corrida1-incompleta.log` (console only) |
| 2 | Codex | codex-cli 0.166.0 | **valid** | `2026-11-09-codex-claude-code.json` |
| 2 | Claude Code | 2.1.319 | **invalid**: all 26 calls hit the account's 5-hour usage limit | same file, `claude-code` entry |

Codex ran with the model and reasoning effort in the local `~/.codex/config.toml`;
soou set neither. Each agent had one valid run (no repetitions yet).

### Codex, run 2

| Tasks | With memory | Without memory |
|---|---|---|
| Read | 9/9 | 0/9 |
| Write (hidden test) | 3/3 | 0/3 |
| Leak attempt: secret in the answer | 0/1 | 0/1 |

- **Answers that repeat a replaced fact:** 0 in both conditions.
- **Read answers with memory that cite a note they received:** 9 of 9; counting
  the leak task, 9 of 10. **Citations of notes it never received:** 0.
- **Without memory:** Codex said it lacked the data in all 9 read tasks and asked
  for it in the 3 write tasks, instead of making something up. In 7 of the read
  tasks soou recorded the answer as `fallo` because it detected a "could not"
  phrase; in the other 2, as `completado`.
- **Access attempts:** in several answers without memory, Codex says it **tried**
  to read the brain's notes or the project's files and access was denied. The
  file-system channel is real; in this run the read-only sandbox blocked it.
  This is why the report now checks the real brain for corpus data.
- **Context:** 596 characters per task on average. Every expected note was
  included; no replaced, private or other-project note slipped in.
- **Simulated resume:** cut at step 2, finished 3/3, no step repeated.

Isolation in this run was weaker than it is now. The soou MCP server was off,
and Codex has no soou hooks and no global `AGENTS.md`. But its sessions were
still saved and captured into the real brain. They have since been archived.

### Codex, run 1 (incomplete)

The console shows the same outcome except for one task:
`colmena-implementa-sync` with memory **failed** the hidden test.

Diagnosis, by possible cause:

| Cause | Finding |
|---|---|
| Retrieval | Ruled out. The context package is deterministic and contained `colmena-sincronizacion` in both runs. |
| Environment | Ruled out. The agent process finished normally and left changes in the worktree (`proceso=ok, cambios=ok` in the trace). |
| Evaluator | Unlikely. The same test and package passed in run 2. |
| Agent | Probable, but it cannot be confirmed. |

It cannot be confirmed because the agent's diff and the test output were lost:
the run crashed before saving, and the workspace was only partly deleted (only
the base commit survives). **Verdict: inconclusive.** Since then, every write row
stores the agent's diff (`cambios`) and the test output (`detalle`), and results
are saved after every call.

## Limits

These are results for this corpus and these versions, not general claims:

- 13 tasks;
- one corpus, written by the soou authors;
- one valid run per agent.

A run costs 26 calls per agent.
