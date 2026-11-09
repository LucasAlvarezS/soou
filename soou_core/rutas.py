"""Rutas de datos de soou: el UNICO lugar donde viven.

Todos los modulos las leen como `rutas.WIKI`, `rutas.SESSIONS`, etc. en el momento
de usarlas (no las copian al importar), asi que cambiar una aca la cambia para
todos. Es lo que permite a los tests apuntar soou a una carpeta temporal y a un
usuario mover sus datos con SOOU_HOME.

`configurar(raiz)` recalcula todas las rutas derivadas de una raiz nueva. Asignar
una ruta suelta (p.ej. solo SESSIONS) tambien vale: las demas no se tocan.
"""

import os
from contextlib import contextmanager
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent.parent
ASSETS = CODE_DIR if (CODE_DIR / "ui").exists() else CODE_DIR / "soou_data"


def _data_root() -> Path:
    """SOOU_HOME manda; un checkout guarda los datos junto al codigo; instalado,
    en ~/.soou."""
    env = os.environ.get("SOOU_HOME")
    if env:
        return Path(env).expanduser().resolve()
    if any((CODE_DIR / m).exists() for m in (".git", "wiki", "soou.config.json")):
        return CODE_DIR
    return Path.home() / ".soou"


# Fuentes externas (las escriben los clientes, soou solo las lee)
CODEX_SESSIONS = Path.home() / ".codex" / "sessions"
CLAUDE_SESSIONS = Path.home() / ".claude" / "projects"

# `eval --tareas` y la conformidad la fijan para los agentes que lanzan (y los
# hooks que esos agentes disparan la heredan): sin MCP de soou, sin hooks de
# soou y sin sesiones guardadas. Un agente evaluado no ve el cerebro real.
EVAL_ENV = "SOOU_EVAL"


def en_evaluacion() -> bool:
    return bool(os.environ.get(EVAL_ENV))


@contextmanager
def modo_evaluacion():
    """Fija EVAL_ENV mientras dura el bloque y deja el entorno como estaba."""
    previo = os.environ.get(EVAL_ENV)
    os.environ[EVAL_ENV] = "1"
    try:
        yield
    finally:
        if previo is None:
            os.environ.pop(EVAL_ENV, None)
        else:
            os.environ[EVAL_ENV] = previo

# Derivadas de la raiz de datos: se fijan en configurar()
ROOT = WIKI = SOURCES = SESSIONS = DISTILL_DIR = ARCHIVO = EVAL_DIR = None
DB_PATH = CONFIG_PATH = BUDGET_PATH = HEARTBEAT_PATH = None

NOMBRES = ("ROOT", "WIKI", "SOURCES", "SESSIONS", "DISTILL_DIR", "ARCHIVO", "EVAL_DIR",
           "DB_PATH", "CONFIG_PATH", "BUDGET_PATH", "HEARTBEAT_PATH",
           "CODEX_SESSIONS", "CLAUDE_SESSIONS")


def configurar(raiz) -> None:
    """Apunta todas las rutas de datos a `raiz`."""
    global ROOT, WIKI, SOURCES, SESSIONS, DISTILL_DIR, ARCHIVO, EVAL_DIR
    global DB_PATH, CONFIG_PATH, BUDGET_PATH, HEARTBEAT_PATH
    ROOT = Path(raiz)
    WIKI = ROOT / "wiki"
    SOURCES = ROOT / "sources"
    SESSIONS = ROOT / "sessions" / "raw"
    DISTILL_DIR = ROOT / "sessions" / "distill-log"
    ARCHIVO = ROOT / "archivo"
    EVAL_DIR = ROOT / "eval"
    DB_PATH = ROOT / "soou.db"            # indice DERIVADO; los .md son la verdad
    CONFIG_PATH = ROOT / "soou.config.json"
    BUDGET_PATH = ROOT / ".soou-budget.json"
    HEARTBEAT_PATH = ROOT / ".soou-heartbeat.json"


configurar(_data_root())
