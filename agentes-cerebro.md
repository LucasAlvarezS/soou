# Instrucciones para agentes

## Qué es soou

soou es un segundo cerebro personal basado en el patrón LLM-Wiki.
Su propósito es conservar y recuperar el contexto acumulado del usuario.

El conocimiento vive en archivos Markdown dentro de `wiki/*.md`.
Ese contenido se indexa y se sirve mediante MCP.
El servidor MCP del repositorio es `soou_mcp.py`.
Sus herramientas principales son `brain_recall` y `brain_query`.

## Instrucción principal

> **ANTES DE RESPONDER cualquier pregunta sobre los proyectos, notas,
> decisiones o dominio del usuario, consulta PRIMERO el cerebro mediante
> la herramienta MCP `brain_recall`. Usa `brain_query` cuando necesites
> una búsqueda explícita o más específica.**

Trata el cerebro como la fuente de verdad sobre el mundo del usuario.
No dependas únicamente del historial visible, suposiciones o memoria general.
Recupera contexto incluso cuando la pregunta parezca sencilla o familiar.

Las reglas del proyecto son parte del cerebro: `schema.yaml` y este archivo se indexan como las notas `regla-schema` y `regla-agentes`, así que se recuperan con `brain_recall` como cualquier otro conocimiento. Están marcadas `pinned`, o sea que ninguna herramienta las sobrescribe.

Al inicio de una sesión, consulta la **persona** del usuario (capa L3) con
`brain_query "soou-persona"` o leyendo `wiki/personal/soou-persona.md` si existe:
resume quién es, en qué trabaja, sus decisiones/convenciones recurrentes y su estilo.
Úsala para adaptar tus respuestas al contexto y las preferencias del usuario.

Formula la consulta con los conceptos centrales de la solicitud actual.
Si el resultado es insuficiente, amplía o reformula la búsqueda.
Combina varias notas cuando el tema cruce proyectos o decisiones.
Respeta contradicciones, fechas y estado de las decisiones registradas.
Cuando falte información, indícalo; no inventes contenido del cerebro.

Esta consulta es de lectura y debe ocurrir antes de razonar la respuesta final.
Después, usa el contexto recuperado junto con los archivos actuales del repo.

## Herramientas MCP

- `brain_recall`: recupera automáticamente el contexto más relevante.
- `brain_query`: busca notas mediante una consulta explícita.
- `brain_note_get`: devuelve una nota **completa**. `query` y `recall` solo dan fragmentos; usa esta cuando necesites el contenido real.
- `brain_note_write`: **escribe** en el cerebro. Úsala para CORREGIR una nota mal sintetizada. Se niega a tocar una nota `pinned`.
- `brain_capture`: aporta material nuevo sin curar; es aditivo y lo integra el destilador.
- `brain_health`: enlaces rotos, huérfanas, placeholders, obsoletas y duplicados.

Prefiere `brain_recall` como punto de entrada habitual.
Usa `brain_query` para profundizar, verificar o localizar un tema concreto.

## Uso desde CLI

```bash
python soou.py query "consulta"
python soou.py sync
python soou.py run "tarea"
```

- `query` busca conocimiento en el cerebro.
- `sync` captura y destila las sesiones hacia el cerebro.
- `run` orquesta una tarea mediante el harness de agentes.

## Memoria acumulativa

soou captura las sesiones automáticamente mediante sincronización y hooks.
El conocimiento útil se acumula y queda disponible para futuras interacciones.
Trabaja asumiendo continuidad: consulta primero, actúa con contexto y evita
repetir decisiones ya documentadas.

## Orquestación automática

- Para CUALQUIER solicitud no trivial del usuario, llama PRIMERO a la herramienta
  MCP `brain_orchestrate` con la tarea. Devuelve el nivel de complejidad, el flujo
  recomendado y contexto del cerebro.
- Si la recomendación es *resolver directo*, resuelve tú mismo usando ese contexto.
- Si la recomendación es *descompone y usa el harness (fan-out multi-agente)*,
  descompón la tarea en subtareas y delega la ejecución al harness de soou
  (`python soou.py run "subtarea"`).
- El usuario NO tiene que pedir orquestación: tú decides automáticamente según
  `brain_orchestrate`. No orquestes tareas triviales.

## Aportar al cerebro

- Cuando tomes una DECISIÓN, descubras un HECHO relevante, o termines algo que
  valga la pena recordar, llama a la herramienta MCP `brain_capture` con un
  resumen breve (y un `title`). Eso lo encola para destilación; es aditivo y no
  pisa nada.
- No esperes al `sync`: captura en el momento las decisiones y aprendizajes de
  la sesión.
