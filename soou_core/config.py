"""Configuracion de soou: soou.config.json con sus defaults (perfiles, agentes, routing, modelos), y la valvula de presupuesto diario de runs.

soou.py lo re-exporta; dentro de soou_core se usa como `config.nombre`."""

import contextlib
import datetime as dt
import json
import os
import time
from pathlib import Path
from soou_core import rutas


_DEFAULT_PROFILES = {
    "single":     {"multiuser": False, "parallel_agents": False, "budget_per_day": None},
    "team":       {"multiuser": True,  "parallel_agents": True,  "budget_per_day": 20},
    "enterprise": {"multiuser": True,  "parallel_agents": True,  "budget_per_day": 100},
}


_DEFAULT_AGENTS = [{"name": "claude-code", "type": "claude"},
                   {"name": "codex", "type": "codex"}]


# Routing por nivel = EJE COSTO: "el modelo más barato que hace el trabajo bien".
# Triviales/contextuales → agente barato primero; complejo/alto-riesgo → frontier primero.
# Es orden de PREFERENCIA (no exclusión): si el preferido no está, cae al siguiente.
_DEFAULT_ROUTING = {
    "trivial":             ["codex", "claude-code"],
    "contextual":          ["codex", "claude-code"],
    "complex":             ["claude-code", "codex"],
    "ambiguous/high-risk": ["claude-code", "codex"],
}


# DOMINIOS del usuario (verticales) — VACÍO por defecto: un producto genérico no
# presupone dominios. El router hace fan-out solo si el usuario define los suyos en
# soou.config.json → "verticals": {nombre: [sinónimos]}. Sin verticales, todo es "core".
_DEFAULT_VERTICALS = {}


_CFG_CACHE = {"mtime": None, "data": None}


def load_config() -> dict:
    """Perfil + capacidades + agentes + routing + verticales del usuario. Cacheado por
    mtime (el router lo consulta por-nota; evita releer el archivo N veces)."""
    try:
        mt = rutas.CONFIG_PATH.stat().st_mtime if rutas.CONFIG_PATH.exists() else 0
    except OSError:
        mt = 0
    if _CFG_CACHE["data"] is not None and _CFG_CACHE["mtime"] == mt:
        return _CFG_CACHE["data"]
    data = {}
    try:
        if rutas.CONFIG_PATH.exists():
            data = json.loads(rutas.CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    profiles = {**_DEFAULT_PROFILES, **(data.get("profiles") or {})}
    prof = data.get("profile", "single")
    caps = dict(profiles.get(prof, profiles["single"]))
    caps.update(data.get("override") or {})       # override puntual sin cambiar de perfil
    cfg = {"profile": prof, "caps": caps,
           "agents": data.get("agents") or _DEFAULT_AGENTS,
           "models": data.get("models") or {},
           "reasoning": data.get("reasoning") or {},
           "routing": {**_DEFAULT_ROUTING, **(data.get("routing") or {})},
           "verticals": {**_DEFAULT_VERTICALS, **(data.get("verticals") or {})},
           "core_markers": [m.lower() for m in (data.get("core_markers") or [])],
           "capture_roots": data.get("capture_roots") or [],
           "exclude_projects": data.get("exclude_projects") or [],
           "capture_unknown_cwd": bool(data.get("capture_unknown_cwd", False)),
           "private_paths": data.get("private_paths") or [],
           # False = soou no inyecta contexto del cerebro por su cuenta (ni el hook
           # de recall ni `run`); las herramientas MCP siguen respondiendo si se usan.
           "brain_context": data.get("brain_context", True) is not False,
           "runs_dir": data.get("runs_dir") or None,
           "session_max_chars": data.get("session_max_chars") or None,
           "profiles": list(profiles.keys())}
    _CFG_CACHE.update(mtime=mt, data=cfg)
    return cfg


def _model_for(agent_name, level):
    m = load_config()["models"].get(agent_name) or {}
    return m.get(level)


def _reasoning_for(agent_name, level):
    m = load_config()["reasoning"].get(agent_name) or {}
    return m.get(level)


def _budget_today() -> int:
    """Runs de agente consumidos hoy (0 si no hay ledger o falla la lectura)."""
    try:
        data = json.loads(rutas.BUDGET_PATH.read_text(encoding="utf-8"))
        return int(data.get(dt.date.today().isoformat(), 0))
    except Exception:
        return 0


def _budget_allow() -> bool:
    """True si aún hay presupuesto (o si es ilimitado)."""
    cap = load_config()["caps"].get("budget_per_day")
    if cap is None:
        return True
    return _budget_today() < cap


# El presupuesto es un LIMITE DE INVOCACIONES de agente por dia, no de dinero:
# soou no conoce el precio de cada llamada.
LOCK_VIEJO_S = 30          # un lock mas viejo que esto es de un proceso que murio
LOCK_ESPERA_S = 10


@contextlib.contextmanager
def _lock_presupuesto():
    """Exclusion mutua entre procesos sobre el ledger (O_CREAT|O_EXCL). Cubre leer,
    comprobar y reservar: sin esto dos runs leen el mismo saldo y ambos pasan.
    Un lock abandonado (proceso muerto) se recupera por antiguedad."""
    lock = Path(str(rutas.BUDGET_PATH) + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    limite = time.time() + LOCK_ESPERA_S
    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{os.getpid()} {time.time():.0f}".encode())
            os.close(fd)
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > LOCK_VIEJO_S:
                    lock.unlink()
                    continue
            except OSError:
                continue
            if time.time() > limite:
                raise TimeoutError("el ledger del presupuesto esta bloqueado")
            time.sleep(0.05)
    try:
        yield
    finally:
        try:
            lock.unlink()
        except OSError:
            pass


def _ledger() -> dict:
    try:
        data = json.loads(rutas.BUDGET_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _escribir_ledger(data: dict):
    """Escritura atomica: nunca queda un ledger a medio escribir."""
    tmp = Path(str(rutas.BUDGET_PATH) + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, rutas.BUDGET_PATH)


def _budget_reserve(n: int = 1) -> bool:
    """Reserva n invocaciones ANTES de lanzar. False si no alcanza el cupo de hoy.
    Si el ledger no se puede bloquear, se niega: mejor no lanzar que pasarse."""
    cap = load_config()["caps"].get("budget_per_day")
    try:
        with _lock_presupuesto():
            data = _ledger()
            hoy = dt.date.today().isoformat()
            usados = int(data.get(hoy, 0))
            if cap is not None and usados + n > cap:
                return False
            data[hoy] = usados + n
            _escribir_ledger(data)
            return True
    except TimeoutError:
        return cap is None
    except OSError:
        return cap is None          # sin cupo configurado no hay nada que proteger


def _budget_consume(n: int = 1) -> None:
    """Compatibilidad: consumo sin comprobar cupo (usa la misma reserva atomica)."""
    try:
        with _lock_presupuesto():
            data = _ledger()
            hoy = dt.date.today().isoformat()
            data[hoy] = int(data.get(hoy, 0)) + n
            _escribir_ledger(data)
    except Exception:
        pass


def _budget_left_str() -> str:
    """Texto tipo 'X/Y runs' (usados/cap) o 'ilimitado' según el cap."""
    cap = load_config()["caps"].get("budget_per_day")
    if cap is None:
        return "ilimitado"
    return f"{_budget_today()}/{cap} invocaciones"
