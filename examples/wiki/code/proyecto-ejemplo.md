---
name: proyecto-ejemplo
area: code
type: project
sources: []
pinned: false
updated: 2026-01-01
---

# Proyecto de ejemplo

Nota de tipo `project` que muestra cómo documentar un proyecto: decisiones de
arquitectura, stack y enlaces a los conceptos que usa.

**Stack (ejemplo):** Python (stdlib), SQLite como índice derivado, Markdown como
fuente de verdad.

**Decisión:** el índice (`soou.db`) es **reconstruible** desde los `.md` con
`reindex`; las notas son la única fuente de verdad. Aplica el patrón [[llm-wiki]].
