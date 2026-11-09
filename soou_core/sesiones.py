"""Modulo sesiones de soou (extraido de soou.py).

soou.py lo re-exporta; dentro de soou_core se usa como `sesiones.nombre`."""

from pathlib import Path
import datetime as dt
import json
import os
import re
import sys
import tempfile
import time
from soou_core import config, indice, rutas, saneado


def read_hook_stdin() -> dict:
    if sys.stdin.isatty():
        return {}
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except Exception:
        return {}


def _runs_dir() -> Path:
    """Carpeta UNICA de los runs del harness (worktrees + trazas).

    Antes cada worktree se creaba al lado del repo (p.ej. en el Escritorio:
    'MiRepo--soou-run-...'), sembrando carpetas sueltas. Ahora todo vive
    bajo soou/runs/ (ignorado por git y por el scan), o donde diga 'runs_dir'."""
    try:
        cfg = config.load_config().get("runs_dir")
    except Exception:
        cfg = None
    return Path(cfg).expanduser() if cfg else rutas.ROOT / "runs"


def _acotar_cuerpo(body: list, max_chars: int | None = None) -> list:
    """Dedupe (mensajes repetidos) + tope total por sesion. Si se pasa del tope se
    conserva el arranque (el pedido) y sobre todo el final (lo que se concluyo)."""
    if max_chars is None:
        try:
            max_chars = int(config.load_config().get("session_max_chars") or saneado.SESSION_MAX_CHARS)
        except Exception:
            max_chars = saneado.SESSION_MAX_CHARS
    vistos, unicos = set(), []
    for linea in body:
        if not linea:
            continue
        clave = linea.strip()
        if clave in vistos:
            continue
        vistos.add(clave)
        unicos.append(linea)
    total = sum(len(l) + 2 for l in unicos)
    if max_chars <= 0 or total <= max_chars:
        return unicos
    cabeza_max, cola_max = max_chars // 3, max_chars - max_chars // 3
    cabeza, usado = [], 0
    for l in unicos:
        if usado + len(l) > cabeza_max:
            break
        cabeza.append(l); usado += len(l) + 2
    cola, usado = [], 0
    for l in reversed(unicos[len(cabeza):]):
        if usado + len(l) > cola_max:
            break
        cola.insert(0, l); usado += len(l) + 2
    omitidos = len(unicos) - len(cabeza) - len(cola)
    return cabeza + [f"_[… {omitidos} mensaje(s) omitidos para acotar la sesion …]_"] + cola


def _msg_text(obj: dict) -> str:
    msg = obj.get("message") if isinstance(obj.get("message"), dict) else obj
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [c.get("text", "") for c in content
                 if isinstance(c, dict) and c.get("type") == "text"]
        return " ".join(p for p in parts if p)
    return ""


def _msg_role(obj: dict) -> str:
    msg = obj.get("message") if isinstance(obj.get("message"), dict) else obj
    return msg.get("role") or obj.get("type") or ""


def cmd_capture(args):
    """SessionEnd: guarda la transcripcion (solo texto util) para destilar despues."""
    if os.environ.get("BRAIN_DISTILL"):   # no capturar la propia subsesion de destilacion
        return
    hook = read_hook_stdin()
    tpath = hook.get("transcript_path") or hook.get("transcriptPath")
    # id COMPLETO (antes truncado a 12 -> colisiones entre sesiones distintas);
    # sanitizado para nombre de archivo seguro.
    sid = str(hook.get("session_id") or hook.get("sessionId") or "unknown")
    sid = re.sub(r"[^A-Za-z0-9_-]", "", sid) or "unknown"
    if not tpath:
        return
    # la captura en vivo respeta la misma politica que la cosecha (capture_roots,
    # exclude_projects, sin ruta conocida...): antes solo la cosecha la aplicaba.
    if not _captura_permitida(hook.get("cwd"))[0]:
        return
    src = Path(tpath)
    try:
        if not src.exists() or src.stat().st_size < 4096:  # ignora sesiones triviales
            return
    except Exception:
        return

    body = []
    try:
        for raw in src.read_text(encoding="utf-8", errors="ignore").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            linea = saneado._linea_sesion(_msg_role(obj), _msg_text(obj))
            if linea:
                body.append(linea)
    except Exception:
        return
    body = _acotar_cuerpo(body)
    if not body:
        return

    now = dt.datetime.now()
    cuerpo = "\n\n".join(body)
    # Misma sesion = mismo archivo: PreCompact y SessionEnd (o dos dias distintos)
    # no deben dejar dos copias. Si no cambio nada, no se reescribe.
    previa = next(rutas.SESSIONS.rglob(f"session-{sid}.md"), None) if rutas.SESSIONS.exists() else None
    if previa is not None:
        try:
            actual = previa.read_text(encoding="utf-8", errors="ignore")
            if actual.split("\n\n", 1)[-1] == cuerpo or cuerpo in actual:
                return
        except OSError:
            pass
        dest = previa
    else:
        dest = rutas.SESSIONS / now.strftime("%Y/%m/%d") / f"session-{sid}.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
    cabecera = [f"<!-- distilled: no -->", f"# Sesion {sid} · {now.strftime('%Y-%m-%d %H:%M')}",
                f"Fuente: {src.name}"]
    cwd = hook.get("cwd")
    if cwd:
        cabecera.append(f"Proyecto: {cwd}")
    dest.write_text("\n".join(cabecera) + "\n\n" + cuerpo, encoding="utf-8")
    print(f"[ok] sesion capturada: {dest.relative_to(rutas.ROOT).as_posix()}")


def extract_codex_rollout(path: Path):
    """Devuelve (session_id, lineas 'rol: texto') de un rollout de Codex.
    Fuente limpia: event_msg item_completed con item.type UserMessage/AgentMessage
    (los response_item role=user incluyen inyecciones de contexto = ruido)."""
    sid, cwd = path.stem, None
    body = []
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            if o.get("type") == "session_meta":
                meta = o.get("payload") or {}
                sid = meta.get("session_id") or sid
                cwd = meta.get("cwd") or cwd
                continue
            if o.get("type") != "event_msg":
                continue
            p = o.get("payload") or {}
            payload_type = p.get("type")
            if payload_type == "item_completed":
                # Formato de rollout usado por versiones anteriores de Codex.
                item = p.get("item") or {}
                it = item.get("type")
                if it not in ("UserMessage", "AgentMessage"):
                    continue
                role = "user" if it == "UserMessage" else "assistant"
                text = " ".join(c.get("text", "") for c in item.get("content", [])
                                 if isinstance(c, dict) and c.get("text")).strip()
            elif payload_type in ("user_message", "agent_message"):
                # Formato actual: los mensajes llegan directamente en event_msg.
                role = "user" if payload_type == "user_message" else "assistant"
                text = str(p.get("message") or "").strip()
            else:
                continue
            # descartar inyecciones de contexto que Codex mete como 'user'
            if text and not text.lstrip().startswith("<environment_context"):
                linea = saneado._linea_sesion(role, text)
                if linea:
                    body.append(linea)
    except Exception:
        return sid, cwd, []
    return sid, cwd, body


def _pull_codex_sessions(refresh: bool = False) -> int:
    """Importa a sessions/raw las sesiones de Codex. Devuelve cuantas (re)importadas.
    refresh=True re-importa una sesion que CRECIO (rollout mas nuevo que la nota) y no
    fue destilada por IA todavia -> resetea su marca para que el Nivel A la re-destile."""
    if os.environ.get("BRAIN_DISTILL") or not rutas.CODEX_SESSIONS.exists():
        return 0
    grouped = {}
    # Un mismo hilo puede ocupar varios rollouts después de una compactación. Codex
    # conserva el session_id original en todos ellos, así que primero los agrupamos y
    # luego fusionamos sus mensajes en vez de dejar que el último pise al anterior.
    for roll in sorted(rutas.CODEX_SESSIONS.rglob("rollout-*.jsonl")):
        try:
            if roll.stat().st_size < 2048:      # sesion trivial
                continue
            if time.time() - roll.stat().st_mtime < 180:
                continue
        except OSError:
            continue
        parts = roll.relative_to(rutas.CODEX_SESSIONS).parts   # AAAA/MM/DD/rollout-*.jsonl
        datedir = "/".join(parts[:3]) if len(parts) >= 4 and parts[0].isdigit() \
            else dt.datetime.now().strftime("%Y/%m/%d")
        sid, cwd, body = extract_codex_rollout(roll)
        if not _captura_permitida(cwd)[0] or _es_subsesion_de_soou(body):
            continue
        sid = re.sub(r"[^A-Za-z0-9_-]", "", str(sid)) or "unknown"
        if not body:
            continue
        grouped.setdefault((datedir, sid), []).append((roll, body))

    n = 0
    for (datedir, sid), segments in grouped.items():
        body, seen = [], set()
        for _, messages in segments:
            for message in messages:
                if message not in seen:
                    seen.add(message)
                    body.append(message)
        body = _acotar_cuerpo(body)
        dest_dir = rutas.SESSIONS / datedir
        dest = dest_dir / f"session-codex-{sid}.md"
        sources = ", ".join(roll.name for roll, _ in segments)
        header = (f"<!-- distilled: no -->\n# Sesion codex {sid} - {datedir}\n"
                  f"Fuente: {sources} (Codex)\n\n")
        desired = header + "\n\n".join(body)
        if dest.exists():
            current = dest.read_text(encoding="utf-8", errors="ignore")
            head = current[:60]
            if "distilled: yes" in head:      # ya sintetizada por IA -> no tocar
                continue
            normalized_current = re.sub(
                r"distilled:\s*\w+", "distilled: no", current, count=1)
            if not refresh or normalized_current == desired:
                continue
            # Creció o apareció otro segmento: se reescribe con marca 'no' para
            # volver a ejecutar Nivel A antes de la síntesis conceptual.
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest.write_text(desired, encoding="utf-8")
        n += 1
    if n:
        print(f"[ok] {n} sesion(es) de Codex importadas a sessions/raw.")
    return n


def cmd_capture_codex(args):
    if _pull_codex_sessions(refresh=True) == 0:
        print("[capture-codex] sin sesiones nuevas de Codex.")


def _bajo(ruta: Path, raiz: Path) -> bool:
    """True si `ruta` cuelga de `raiz`.

    Una ruta NO absoluta nunca cuenta: resolve() la pegaria al directorio actual
    y, si este esta bajo tu carpeta personal, una ruta ajena (p.ej. un cwd de
    Windows leido en Linux) pasaria por tuya."""
    if not ruta.is_absolute():
        return False
    try:
        ruta.resolve().relative_to(raiz.resolve())
        return True
    except (ValueError, OSError):
        return False


def _es_subsesion_de_soou(body: list) -> bool:
    """La sesion de un agente que lanzo el propio harness empieza con el prompt que
    arma soou. Ya la registro el run; cosecharla otra vez la duplica (y en uso real
    trajo de vuelta respuestas que el run habia marcado como no verificadas)."""
    return bool(body) and body[0].startswith(("**user:** Contexto del cerebro (soou)",
                                              "**user:** Subtarea ("))


def _captura_permitida(cwd) -> tuple:
    """(permitida, motivo) para una sesion segun su directorio de trabajo.

    Si no se conoce el directorio se DESCARTA: sin ruta no se puede comprobar
    capture_roots ni exclude_projects, y lo que no se puede atribuir no entra.
    `capture_unknown_cwd: true` en soou.config.json lo permite explicitamente."""
    cfg = config.load_config()
    if not cwd:
        if cfg.get("capture_unknown_cwd"):
            return True, "sin ruta conocida (capture_unknown_cwd)"
        return False, "sin ruta conocida"
    ruta = Path(str(cwd))
    raices = [Path(r).expanduser() for r in (cfg.get("capture_roots") or [])]
    if raices:                                    # lista blanca explicita
        if not any(_bajo(ruta, r) for r in raices):
            return False, "fuera de capture_roots"
    elif not _bajo(ruta, Path.home()):            # por defecto: solo tu carpeta personal
        return False, "fuera de tu carpeta personal"
    # el temporal del sistema cuelga de tu carpeta en Windows (AppData\Local\Temp):
    # ahi corren eval --tareas, la conformidad y los tests. Sus sesiones no son
    # trabajo tuyo, y con contenido inventado ensuciaban el cerebro.
    # Solo entra si TU listaste una raiz que ya esta dentro del temporal; una raiz
    # amplia (p.ej. tu carpeta personal) no lo reabre.
    temporal = Path(tempfile.gettempdir())
    if _bajo(ruta, temporal) and not any(_bajo(r, temporal) and _bajo(ruta, r) for r in raices):
        return False, "directorio temporal del sistema (eval, conformidad, pruebas)"
    # el agente que corre un `soou run` trabaja dentro de runs/: esa sesion ya la
    # captura el propio run (con su verificacion); cosecharla de nuevo la duplica.
    if _bajo(ruta, _runs_dir()):
        return False, "worktree de un run de soou (lo captura el run)"
    if indice.slugify(ruta.name) in set(cfg.get("exclude_projects") or []):
        return False, "proyecto en exclude_projects"
    return True, "ok"


def extract_claude_transcript(path: Path):
    """(session_id, cwd, lineas 'rol: texto') de una transcripcion de Claude Code."""
    sid, cwd, body = path.stem, None, []
    try:
        for linea in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            linea = linea.strip()
            if not linea:
                continue
            try:
                o = json.loads(linea)
            except Exception:
                continue
            sid = o.get("sessionId") or o.get("session_id") or sid
            cwd = cwd or o.get("cwd")
            linea = saneado._linea_sesion(_msg_role(o), _msg_text(o))
            if linea:
                body.append(linea)
    except OSError:
        return sid, cwd, []
    return sid, cwd, _acotar_cuerpo(body)


def _pull_claude_sessions(refresh: bool = False) -> int:
    """Importa a sessions/raw las transcripciones de Claude Code.

    Escribe con el MISMO nombre que usa el hook SessionEnd, asi la sesion queda
    capturada una sola vez corra el hook, corra el cosechador, o corran los dos.
    """
    if os.environ.get("BRAIN_DISTILL") or not rutas.CLAUDE_SESSIONS.exists():
        return 0
    n = 0
    for tr in sorted(rutas.CLAUDE_SESSIONS.glob("*/*.jsonl")):
        try:
            st = tr.stat()
            if st.st_size < 4096:                    # sesion trivial
                continue
            if time.time() - st.st_mtime < 180:      # todavia abierta
                continue
        except OSError:
            continue
        sid, cwd, body = extract_claude_transcript(tr)
        if not body or not _captura_permitida(cwd)[0] or _es_subsesion_de_soou(body):
            continue
        sid = re.sub(r"[^A-Za-z0-9_-]", "", str(sid)) or "unknown"
        previa = next(rutas.SESSIONS.rglob("session-" + sid + ".md"), None)
        if previa is not None:
            if "distilled: yes" in previa.read_text(encoding="utf-8", errors="ignore")[:60]:
                continue                             # ya sintetizada por IA: no tocar
            try:
                if not refresh or st.st_mtime <= previa.stat().st_mtime:
                    continue
            except OSError:
                continue
            dest = previa
        else:
            dest = (rutas.SESSIONS / dt.datetime.fromtimestamp(st.st_mtime).strftime("%Y/%m/%d")
                    / ("session-" + sid + ".md"))
            dest.parent.mkdir(parents=True, exist_ok=True)
        cabecera = ["<!-- distilled: no -->",
                    "# Sesion " + sid + " · "
                    + dt.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
                    "Fuente: " + tr.name]
        if cwd:
            cabecera.append("Proyecto: " + str(cwd))
        dest.write_text("\n".join(cabecera) + "\n\n" + "\n\n".join(body), encoding="utf-8")
        n += 1
    return n


def _relativa(p: Path) -> str:
    try:
        return p.relative_to(rutas.ROOT).as_posix()
    except ValueError:
        return p.as_posix()


def _buscar_transcripcion(ref: str):
    """(ruta, cliente) de una sesion por ruta o por id; (None, None) si no esta."""
    p = Path(ref).expanduser()
    if p.is_file():
        return p, ("codex" if p.name.startswith("rollout-") else "claude-code")
    sid = re.sub(r"[^A-Za-z0-9_-]", "", ref)
    if not sid:
        return None, None
    if rutas.CLAUDE_SESSIONS.exists():
        hit = next(rutas.CLAUDE_SESSIONS.glob(f"*/{sid}.jsonl"), None)
        if hit:
            return hit, "claude-code"
    if rutas.CODEX_SESSIONS.exists():
        hit = next(rutas.CODEX_SESSIONS.rglob(f"rollout-*{sid}*.jsonl"), None)
        if hit:
            return hit, "codex"
    return None, None


def importar_sesion(ref: str, proyecto: str) -> tuple:
    """Importacion EXPLICITA de una sesion que la captura automatica descarto
    (p.ej. porque el cliente arranco en una carpeta de sistema). Se atribuye al
    proyecto que indicas, y ese proyecto tiene que pasar la misma politica
    (capture_roots, exclude_projects): no se amplia nada para compensar. La
    cabecera deja la procedencia: directorio original y que fue explicita."""
    if not proyecto:
        return False, "falta --proyecto: la carpeta real del proyecto al que pertenece"
    ok, motivo = _captura_permitida(str(Path(proyecto).expanduser().resolve()))
    if not ok:
        return False, f"el proyecto {proyecto} no se puede capturar: {motivo}"
    ruta, cliente = _buscar_transcripcion(ref)
    if ruta is None:
        return False, f"no encontre la sesion {ref} (pasa la ruta del .jsonl o su id)"
    if cliente == "codex":
        sid, cwd, body = extract_codex_rollout(ruta)
        body = _acotar_cuerpo(body)
    else:
        sid, cwd, body = extract_claude_transcript(ruta)
    if not body:
        return False, "la sesion no tiene mensajes utiles"
    if _es_subsesion_de_soou(body):
        return False, "es una subsesion lanzada por soou: ya la captura su run"
    sid = re.sub(r"[^A-Za-z0-9_-]", "", str(sid)) or "unknown"
    prefijo = "session-codex-" if cliente == "codex" else "session-"
    previa = next(rutas.SESSIONS.rglob(prefijo + sid + ".md"), None) \
        if rutas.SESSIONS.exists() else None
    if previa is not None:
        return False, f"ya estaba capturada: {_relativa(previa)}"
    momento = dt.datetime.fromtimestamp(ruta.stat().st_mtime)
    dest = rutas.SESSIONS / momento.strftime("%Y/%m/%d") / (prefijo + sid + ".md")
    dest.parent.mkdir(parents=True, exist_ok=True)
    cabecera = ["<!-- distilled: no -->",
                f"# Sesion {sid} · {momento.strftime('%Y-%m-%d %H:%M')}",
                f"Fuente: {ruta.name} ({cliente})",
                f"Proyecto: {Path(proyecto).expanduser().resolve()}",
                f"Importada: explicita (directorio original: {cwd or 'desconocido'})"]
    dest.write_text("\n".join(cabecera) + "\n\n" + "\n\n".join(body), encoding="utf-8")
    return True, f"importada: {_relativa(dest)}"


def _session_sources():
    """Fuentes de sesion que el sync cosecha. Ambas leen carpetas globales de
    la maquina y comparten la misma politica de filtrado por proyecto."""
    return [("codex", _pull_codex_sessions), ("claude-code", _pull_claude_sessions)]


def _pull_all_sessions(refresh: bool = False) -> int:
    total = 0
    for name, fn in _session_sources():
        try:
            total += fn(refresh=refresh)
        except Exception as e:
            print(f"[aviso] pull {name}: {e}")
    return total


def cmd_distill_mark(args):
    """Marca notas 'doc' como distilled: yes (su conocimiento vive ya en un concepto)."""
    targets = set(args.names)
    n = 0
    for p in rutas.WIKI.rglob("*.md"):
        note = indice.parse_note(p)
        if note["name"] in targets and note["meta"].get("type") == "doc":
            raw = p.read_text(encoding="utf-8")
            if "distilled: yes" in raw:
                continue
            raw = re.sub(r"(\ntype: doc\n)", r"\1distilled: yes\n", raw, count=1)
            p.write_text(raw, encoding="utf-8")
            n += 1
    print(f"[ok] {n} docs marcados como destilados.")


def _matches_session_source(path: Path, source: str | None) -> bool:
    if not source:
        return True
    if source == "codex":
        return path.name.startswith("session-codex-")
    return not path.name.startswith("session-codex-")


def pending_sessions(source: str | None = None) -> list[Path]:
    """Sesiones capturadas (L0) aún sin destilar a notas."""
    if not rutas.SESSIONS.exists():
        return []
    out = []
    for f in sorted(rutas.SESSIONS.rglob("session-*.md")):
        if not _matches_session_source(f, source):
            continue
        head = f.read_text(encoding="utf-8", errors="ignore")[:60]
        if "distilled: yes" not in head:
            out.append(f)
    return out


_MSG_L0 = re.compile(r"^\*\*(user|assistant):\*\*[ \t]?", re.M)


def _sanear_crudo(texto: str) -> str:
    """Re-sanea un L0 ya escrito: conserva la cabecera, limpia cada mensaje y acota."""
    primero = _MSG_L0.search(texto)
    if not primero:
        return texto
    cabecera = texto[:primero.start()].rstrip("\n")
    marcas = list(_MSG_L0.finditer(texto))
    body = []
    for k, m in enumerate(marcas):
        fin = marcas[k + 1].start() if k + 1 < len(marcas) else len(texto)
        linea = saneado._linea_sesion(m.group(1), texto[m.end():fin])
        if linea:
            body.append(linea)
    body = _acotar_cuerpo(body)
    return cabecera + "\n\n" + "\n\n".join(body) if body else cabecera + "\n"


def _limpiar_sesiones(aplicar: bool) -> dict:
    """Aplica el saneado a todos los L0 existentes. Antes de escribir guarda una copia
    comprimida de sessions/raw en archivo/. No cambia marcas de destilado."""
    import tarfile
    archivos = sorted(rutas.SESSIONS.rglob("session-*.md")) if rutas.SESSIONS.exists() else []
    antes = despues = cambiados = 0
    nuevos = {}
    for f in archivos:
        txt = f.read_text(encoding="utf-8", errors="ignore")
        limpio = _sanear_crudo(txt)
        antes += len(txt.encode("utf-8"))
        despues += len(limpio.encode("utf-8"))
        if limpio != txt:
            cambiados += 1
            nuevos[f] = limpio
    respaldo = None
    if aplicar and nuevos:
        respaldo = rutas.ARCHIVO / f"sessions-raw-antes-de-limpiar-{dt.datetime.now():%Y%m%d-%H%M%S}.tar.gz"
        respaldo.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(respaldo, "w:gz") as tar:
            tar.add(rutas.SESSIONS, arcname="sessions/raw")
        for f, limpio in nuevos.items():
            # se conserva el mtime: compact mide la edad del crudo con el, y
            # is_stale lo compara contra las notas que lo citan como fuente.
            st = f.stat()
            f.write_text(limpio, encoding="utf-8")
            os.utime(f, (st.st_atime, st.st_mtime))
    return {"total": len(archivos), "cambiados": cambiados, "antes": antes,
            "despues": despues, "respaldo": respaldo}


def cmd_sessions(args):
    if getattr(args, "importar", None):
        ok, msg = importar_sesion(args.importar, getattr(args, "proyecto", None))
        print(("[ok] " if ok else "[no] ") + msg)
        return
    if not rutas.SESSIONS.exists():
        print("Sin sesiones capturadas todavia.")
        return
    if getattr(args, "limpiar", False):
        aplicar = getattr(args, "apply", False)
        r = _limpiar_sesiones(aplicar)
        print(f"== sessions --limpiar ==" + ("" if aplicar else "  (simulacion)"))
        print(f"  {r['cambiados']} de {r['total']} sesion(es) con ruido inyectado o secretos")
        print(f"  tamaño L0: {r['antes']/1024:.0f} KB -> {r['despues']/1024:.0f} KB")
        if r["respaldo"]:
            print(f"  respaldo previo: {r['respaldo'].relative_to(rutas.ROOT).as_posix()}")
        elif not aplicar:
            print("  Simulacion: no se escribio nada. Corre con --apply para aplicarlo.")
        return
    files = sorted(rutas.SESSIONS.rglob("session-*.md"))
    pend = 0
    for f in files:
        head = f.read_text(encoding="utf-8", errors="ignore")[:60]
        done = "distilled: yes" in head
        pend += 0 if done else 1
        print(f"  {'✔ destilada' if done else '· pendiente'}  {f.relative_to(rutas.ROOT).as_posix()}")
    print(f"\n{len(files)} sesiones · {pend} sin destilar.")
    total_bytes = sum(f.stat().st_size for f in rutas.SESSIONS.rglob("*") if f.is_file())
    print(f"Tamaño total de sessions/raw: {total_bytes / 1024:.1f} KB")
    if pend:
        print("Corre 'python soou.py distill' para destilarlas autonomamente "
              "con un agente de escritura disponible, o pedile a un agente compatible "
              "'destila las pendientes'.")


_SESSION_PREFIXES = ("codex", "mcp", "run")


def _source_of_session(filename: str) -> str:
    """Fuente real de una sesion L0 segun el prefijo del archivo. Antes todo lo que
    no fuera Codex se etiquetaba "claude-code", y las capturas del MCP y del runner
    quedaban mal atribuidas."""
    for pref in _SESSION_PREFIXES:
        if filename.startswith("session-" + pref + "-"):
            return pref
    return "claude-code"


_SID_MAX = 15


def _sid_of_session(filename: str) -> str:
    """Identificador estable y UNICO de una sesion L0.

    Recortar a secas rompia de dos maneras: "mcp-20260920-163238" quedaba en
    "mcp-2026", y TODAS las capturas del MCP destilaban a la misma nota; y al
    recortar "20260920-173813-1" se perdia justo el sufijo que distingue dos
    capturas del mismo segundo. Cuando hay que recortar se ancla con un hash
    corto del id completo: acotado, unico y determinista (el mismo archivo
    siempre da el mismo id, asi re-destilar no duplica)."""
    import hashlib
    m = re.search(r"session-(?:codex-|mcp-|run-)?(.+)\.md$", filename)
    crudo = m.group(1) if m else Path(filename).stem
    if len(crudo) <= _SID_MAX:
        return crudo
    return crudo[:_SID_MAX] + "-" + hashlib.sha1(crudo.encode("utf-8")).hexdigest()[:6]


def _parse_session_raw(path: Path) -> dict:
    txt = path.read_text(encoding="utf-8", errors="ignore")
    src = _source_of_session(path.name)
    sid = _sid_of_session(path.name)
    dm = re.search(r"(\d{4}-\d{2}-\d{2})", txt[:200])
    date = dm.group(1) if dm else indice.today()
    am = re.search(r"^Area:\s*([a-z]+)\s*$", txt[:400], re.MULTILINE)
    area = am.group(1) if am else None
    user_asks, assistant = [], []
    for mm in re.finditer(r"\*\*(user|assistant):\*\*\s*(.+)", txt):
        (user_asks if mm.group(1) == "user" else assistant).append(mm.group(2).strip())
    return {"source": src, "sid": sid, "date": date, "area": area,
            "user_asks": user_asks, "assistant": assistant,
            "n_msgs": len(user_asks) + len(assistant)}


def _mark_raw(path: Path, state: str):
    txt = path.read_text(encoding="utf-8", errors="ignore")
    path.write_text(re.sub(r"distilled:\s*\w+", f"distilled: {state}", txt, count=1),
                    encoding="utf-8")


def _reset_marks(source: str | None = None) -> int:
    """--force: devuelve las sesiones a 'distilled: no' para re-destilarlas de cero
    (Nivel A y B). Aditivo/idempotente: solo reescribe la marca de cabecera."""
    if not rutas.SESSIONS.exists():
        return 0
    n = 0
    for f in sorted(rutas.SESSIONS.rglob("session-*.md")):
        if not _matches_session_source(f, source):
            continue
        txt = f.read_text(encoding="utf-8", errors="ignore")
        new = re.sub(r"distilled:\s*\w+", "distilled: no", txt, count=1)
        if new != txt:
            f.write_text(new, encoding="utf-8")
            n += 1
    return n


def lite_pending(source: str | None = None) -> list:
    """Sesiones sin Nivel A ni B (ni 'lite' ni 'yes' en la cabecera)."""
    if not rutas.SESSIONS.exists():
        return []
    out = []
    for f in sorted(rutas.SESSIONS.rglob("session-*.md")):
        if not _matches_session_source(f, source):
            continue
        head = f.read_text(encoding="utf-8", errors="ignore")[:60]
        if "distilled: yes" not in head and "distilled: lite" not in head:
            out.append(f)
    return out
