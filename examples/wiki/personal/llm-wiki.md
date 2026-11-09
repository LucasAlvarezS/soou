---
name: llm-wiki
area: personal
type: concept
sources: []
pinned: true
updated: 2026-01-01
---

# LLM Wiki (el patrón)

En vez de recuperar documentos crudos en cada consulta (RAG), el modelo mantiene
**incrementalmente** una wiki en Markdown: notas cortas, cada una sobre **un
concepto**, enlazadas entre sí con `[[wikilinks]]`. La memoria deja de ser un
montón de texto y pasa a ser un **artefacto navegable** que crece con el uso.

soou implementa este patrón: captura sesiones, las **destila** en notas-concepto
y las **enlaza** en un grafo. La recuperación combina BM25 + TF-IDF con fusión RRF.

Ver también [[nota-de-bienvenida]] y [[proyecto-ejemplo]].
