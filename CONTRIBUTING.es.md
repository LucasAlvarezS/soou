# Contribuir a soou

[English](CONTRIBUTING.md) · [Español](CONTRIBUTING.es.md)

soou es neutral respecto del proveedor a propósito. No pertenece a ninguna
empresa de IA y debe funcionar con el agente que realmente uses. Eso hace que la
ayuda externa valga especialmente: quien usa un agente que nosotros nunca
tocamos es quien sabe cómo conectarlo bien.

Haz un fork, rómpelo, y devuélvelo mejor.

## La contribución más fácil que sí importa

**Agregar soporte para un agente.** No hace falta tocar el core, y le sirve a
cualquiera que use esa herramienta.

Cada CLI conocida vive en `RECIPE_REGISTRY`, en `soou_core/agentes.py`. Una
receta dice qué binarios buscar y cómo invocar al agente sin interfaz, una vez
con escritura autónoma y otra en solo lectura, y qué restricción aplica de
verdad su modo lectura:

```python
"tu-agente":   {"bins": ["tu-agente"],
                "model_flag": "-m",
                "write": ["run", "--auto", "{prompt}"],
                "read":  ["run", "--read-only", "{prompt}"],
                "lectura": "aprobacion_cli"},
```

`{prompt}` se reemplaza por la tarea; con `"prompt_via": "stdin"` va por la
entrada estándar, que es lo preferible. `lectura` es lo que impide que una
ejecución de solo lectura escriba: `"sandbox_os"` si lo impone el sistema
operativo, `"aprobacion_cli"` si lo imponen los permisos del propio agente,
`"no_verificada"` si no estás seguro (soou entonces rechaza la lectura con ese
agente). Si el agente no tiene modo lectura, pon `"read": None`: nunca reutilices
el comando de escritura. Se valida con
`python soou.py agents --probe`, que lanza el binario de verdad en lugar de solo
revisar el `PATH`.

Si el agente no tiene CLI pero expone una API compatible con OpenAI, no hay nada
que agregar: se conecta con el tipo `http` en `soou.config.json`. Si habla ACP,
usa el tipo `acp`: así soou decide cada permiso por sí mismo.

Antes de abrir un pull request para un agente, corre
`python soou.py agents --conformance NOMBRE`. Pasa por el mismo ejecutor que
`run`, en un repo temporal limpio y sin notas del cerebro, y comprueba que el
agente arranca, lee (o se rechaza si su modo lectura no está verificado), no
toca el workspace cuando se le pide escribir en lectura, escribe en escritura,
se detiene con un timeout corto y devuelve un resultado que respeta el contrato.
Lanza el agente hasta 4 veces, así que gasta uso real. Sale con código 1 si un
caso falla. Las mismas comprobaciones corren en CI contra los agentes falsos de
`tests/fixtures/`.

Otras contribuciones bienvenidas: un cosechador para las transcripciones de otro
cliente, una traducción, un error con una prueba que falle adjunta, o cualquier
aspereza que hayas encontrado durante la instalación.

## Principios que un cambio debe respetar

No son preferencias de estilo. Son la razón por la que el proyecto funciona como
funciona.

- **El core no tiene dependencias.** Solo biblioteca estándar. Si algo necesita un
  paquete, va detrás de un import opcional y degrada con elegancia, como la
  interfaz cuando cae de `pywebview` al navegador.
- **El Markdown es la fuente de verdad.** `soou.db` es un índice derivado. Todo lo
  que no se pueda reconstruir borrando la base y ejecutando `reindex` es un error.
- **Aditivo por defecto.** La destilación nunca borra. Solo `compact` quita
  material, archiva en vez de borrar, y simula salvo que se pase `--apply`.
- **`pinned: true` es sagrado.** Marca una corrección humana. Nada la sobrescribe.
  Cuando algo nuevo la contradice, la respuesta es una nota aparte, enlazada.
- **El conocimiento del usuario es suyo.** `wiki/`, `sources/` y `sessions/` nunca
  van al repositorio. Las notas llegan a un agente solo por los caminos listados
  en la sección Privacidad del README, y todo camino nuevo debe respetar las
  notas `private` y `brain_context`.
- **No prometer lo que el código no hace cumplir.** Si una garantía depende de
  los permisos del propio agente, se dice. Un modo lectura que no se puede hacer
  cumplir se rechaza, no se supone.

## Mapa del código

`soou.py` es la línea de comandos: el parseo de argumentos y los comandos chicos.
El trabajo vive en `soou_core/`, en capas. Un módulo solo importa de las capas de
abajo, y eso es lo que mantiene las importaciones sin ciclos.

| Capa | Módulo | Qué vive ahí |
|---|---|---|
| base | `rutas.py` | Todas las rutas de datos (`ROOT`, `WIKI`, `SESSIONS`, `DB_PATH`...), en un solo lugar |
| | `texto.py` | Tokenizador, plegado de acentos, el stemmer español/inglés, BM25, TF-IDF, TextRank |
| | `saneado.py` | Limpieza de bloques inyectados por los clientes y de secretos antes de guardar una sesión |
| | `contrato.py` | El contrato interno: `TaskRequest`, `TaskResult`, estados, códigos de error, políticas de verificación |
| | `agentes.py` | Adaptadores de las CLIs, el registro de recetas, el lanzamiento seguro de procesos |
| | `acp.py` | El cliente ACP v1 mínimo (JSON-RPC por stdio) y su adaptador |
| | `vigencia.py` | Campos de procedencia y vigencia: reemplazos, el lint de vigencia, la verificación de un run |
| | `plantillas.py`, `consola.py` | El HTML del grafo; marca y spinner de terminal |
| núcleo | `config.py` | `soou.config.json`, sus valores por defecto, modelos y el presupuesto diario con reserva |
| | `indice.py` | Notas, el índice SQLite, el grafo, la recuperación híbrida y las notas privadas |
| | `router.py` | Clasificación de tareas y el router, más `soou eval` |
| | `contexto.py` | El paquete de contexto único: proyecto, aislamiento, presupuesto, `context --explain` |
| | `registro.py` | Arma los agentes disponibles desde la configuración |
| memoria | `sesiones.py` | Captura y cosecha de sesiones (L0) |
| | `destilado.py` | Destilado Nivel A y Nivel B, sync |
| | `compactar.py` | `compact`: archivado, rotación de logs, limpieza de enlaces |
| arriba | `ejecutor.py` | El ejecutor único: candidatos, un intento (presupuesto, checkpoint, verificación, traza), fallback, `soou trace` |
| | `harness.py` | La orquestación sobre el ejecutor: worktrees, validadores, planes, `run` |
| | `conformidad.py` | `agents --conformance`: prueba un agente a través del ejecutor |
| | `evaluacion.py` | `eval --tareas`: el valor de la memoria sobre el corpus inventado, con y sin ella |
| | `instalacion.py` | `init`, `status`, `uninstall`: hooks, MCP, sync programado |
| | `ui.py` | La API de la interfaz y su servidor local |
| | `soou_mcp.py` | El servidor MCP (JSON-RPC por stdio) |

Toda llamada a un agente pasa por `ejecutor.intentar`: reserva presupuesto, toma
un checkpoint del worktree, lanza el agente, aplica la verificación mínima (que
vale en todos los niveles), deja una traza y, si el intento falló, devuelve el
worktree exactamente a su checkpoint. No agregues otro bucle que llame a
`adaptador.run` directo.

La capa de memoria nunca importa la capa superior. Cuando el destilado necesita
un agente (`sync --deep`, `persona`), la CLI le pasa un
`ejecutor.InvocadorDestilado`: el ejecutor hace las llamadas y el módulo de
destilado solo arma el lote y lee el resultado. El destilado edita el wiki real
y su trabajo es aditivo, así que esos intentos conservan lo parcial en vez de
volver a un checkpoint. El destilado corre en el nivel `trivial` con el modelo
de `config.modelo_destilado` (`models.<agente>.distill`, si falta el de
`trivial`); `persona` sigue en `complex`. Un test
(`tests/test_destilado_ejecutor.py`) revisa los imports.

Dos reglas mantienen honestas a las pruebas:

- **Las rutas de datos se leen de `rutas` al momento de usarlas**, nunca se
  copian al importar. Para apuntar soou a otro lado, asigna `soou.rutas.WIKI = ...`
  (o llama `rutas.configurar(raiz)`).
- **Entre módulos se llama `modulo.nombre`, no `from modulo import nombre`.** Así
  `patch.object(soou.config, "load_config", ...)` alcanza a todos los llamadores.
- **Un test nunca lanza un agente real ni escribe en tus datos.** Usa adaptadores
  falsos (`tests/fixtures/agente_falso.py`) y parchea `sesiones._runs_dir` y
  `config._budget_reserve`.

## Ejecutar las pruebas

```bash
python -m unittest discover -s tests
```

Unos segundos, sin red y sin servicios externos. El CI las corre en Linux, macOS
y Windows en cada push y cada pull request. Si tocas la recuperación, corre
también `soou eval`: `tests/test_eval_retrieval.py` guarda un piso de calidad.

Todo comportamiento nuevo necesita una prueba. El error que más caro salió en
este proyecto fue una colisión que hacía que varias sesiones terminaran en la
misma nota, y la suite no lo detectó porque nada cubría ese camino. Las pruebas
valen cuando cubren lo que puede fallar en silencio.

Los caminos destructivos merecen pruebas de lo que **no** debe pasar: que se
respete `pinned`, que una simulación no escriba nada, que lo archivado siga
existiendo.

## Pull requests

- Un tema por pull request.
- Explica el **porqué** en la descripción. El qué ya está en el diff.
- Verifica que la suite esté verde.
- Español o inglés, en los commits y en la descripción, da lo mismo.

Si tienes dudas de si una idea encaja, abre un issue y pregunta antes. Sale más
barato para todos que un pull request rechazado.

## Reportar un error

Incluye qué esperabas, qué pasó, y la salida de:

```bash
python soou.py status
python soou.py agents
```

No pegues notas de tu cerebro. Si hay una nota involucrada, describe su forma en
lugar de su contenido.

## Licencia

Las contribuciones se publican bajo la [licencia MIT](LICENSE), igual que el
proyecto.
