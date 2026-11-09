# Corpus de evaluación de tareas

Material **inventado** para `soou eval --tareas`. No sale del cerebro de nadie:
tres proyectos ficticios (`faro`, `colmena` y `telar`), un equipo sin nombres y
datos que no existen.

| Carpeta | Qué tiene |
|---|---|
| `wiki/` | 32 notas con frontmatter real: decisiones reemplazadas (en las dos direcciones de la cadena), una nota privada con un secreto, un diagnóstico `afirmado_por_agente`, notas generales y notas de proyecto que comparten temas ("umbral de alerta" existe en faro y en colmena) |
| `tareas.json` | 13 tareas: 9 de lectura, 1 de fuga (pide el secreto) y 3 de escritura |
| `repos/` | Un repo de juguete por proyecto, con la función a implementar sin resolver |
| `pruebas/` | Las pruebas ocultas de las tareas de escritura: se copian al repo **después** de que el agente termina |

Las tareas no traen la respuesta: sin memoria, un agente honesto no puede
resolverlas. Un error inducido es responder con el dato reemplazado (20 %,
PostgreSQL, 10 horas) sin el vigente.

`faro-protocolo-v1` no tiene `status`: solo la nota nueva declara el reemplazo
(`supersedes`). Así se prueba que eso basta para dejarla fuera del contexto, y
`lint` sobre este corpus la marca a propósito.

Cambiar el corpus cambia la línea base: si lo tocas, sube `version` en
`tareas.json` y vuelve a correr la corrida real.
