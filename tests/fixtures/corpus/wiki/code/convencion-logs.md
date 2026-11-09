---
name: convencion-logs
area: code
type: concept
sources: []
pinned: false
updated: 2026-10-01
---

# Convencion de logs

Todos los servicios escriben sus logs en JSON lines, un objeto por linea, con un campo `trace_id` para seguir una operacion entre servicios. El nivel por defecto es INFO. Aplica a [[proj-faro]], [[proj-colmena]] y [[proj-telar]].
