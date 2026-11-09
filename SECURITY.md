# Security

soou reads your agent transcripts from disk, keeps a local knowledge base, and
can launch coding agents that write to a git worktree. Those are the parts where
a bug can hurt, so reports about them are very welcome.

## Reporting a vulnerability

Please **do not open a public issue** for a security problem.

Use GitHub's private reporting instead: **Security → Report a vulnerability** on
this repository. Include what you found, how to reproduce it, and what an
attacker could do with it.

You will get an acknowledgement within a few days. Fixes for confirmed issues are
released as soon as they are ready, and you will be credited in the changelog
unless you prefer otherwise.

## Supported versions

Only the latest release receives security fixes while the project is below 1.0.

## What is in scope

- **Data leaving your machine.** soou is local-first. Anything that sends notes,
  sessions or paths somewhere you did not configure is a bug.
- **Secret redaction.** Sessions are scrubbed before they are written to disk
  (API keys, tokens, private keys, `password=` assignments, credentials in
  connection URLs). The scrubbing is pattern based and best effort. A common
  secret format that slips through is a valid report.
- **Capture policy.** Only sessions whose working directory is under your home
  folder (or `capture_roots`) should be captured, and a session with no known
  folder should be dropped unless `capture_unknown_cwd` is on.
- **Private notes.** A note marked `private: true`, or under `private_paths`,
  reaching an agent through recall, MCP, `query`, `run` or the persona.
- **The harness.** soou never pushes and never merges on its own. In scope: soou
  itself touching your main branch, a read-only run reaching an agent's write
  command, or task text being interpreted by a shell (for example `cmd.exe` on
  Windows). The git worktree under `runs/` separates changes; it is not a
  sandbox, so what an agent does within its own permissions is that agent's
  security boundary, not soou's.
- **The MCP server and the interface server**, which run locally and should not
  be reachable from other machines.

## What is not in scope

- What a third-party agent (Claude Code, Codex, and so on) does with the context
  soou gives it. Review the policies of the agents you connect.
- Secrets you deliberately write into a note.
