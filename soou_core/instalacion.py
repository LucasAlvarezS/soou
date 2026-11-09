"""Modulo instalacion de soou (extraido de soou.py).

soou.py lo re-exporta; dentro de soou_core se usa como `instalacion.nombre`."""

from pathlib import Path
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
from soou_core import consola, indice, registro, rutas


def _bootstrap_workspace() -> list:
    done = []
    if not rutas.CONFIG_PATH.exists() and (rutas.ASSETS / "soou.config.example.json").exists():
        rutas.CONFIG_PATH.write_text((rutas.ASSETS / "soou.config.example.json").read_text(encoding="utf-8"),
                               encoding="utf-8")
        done.append("soou.config.json (desde la plantilla)")
    ex = rutas.ASSETS / "examples" / "wiki"
    has_notes = rutas.WIKI.exists() and any(rutas.WIKI.rglob("*.md"))
    if ex.exists() and not has_notes:
        for src in ex.rglob("*.md"):
            dst = rutas.WIKI / src.relative_to(ex)
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        done.append("wiki/ (notas de ejemplo)")
    for d in (rutas.SESSIONS, rutas.SOURCES):
        d.mkdir(parents=True, exist_ok=True)
    indice.build_index()
    done.append("índice soou.db")
    return done


def _sync_exe():
    """(ejecutable sin consola, script) detectados en runtime — portable por máquina."""
    py = Path(sys.executable)
    pyw = py.with_name("pythonw.exe")
    return (str(pyw if (os.name == "nt" and pyw.exists()) else py), str(rutas.CODE_DIR / "soou.py"))


def _install_sync_trigger(interval_min: int = 5, force: bool = False) -> str:
    exe, script = _sync_exe()
    if os.name == "nt":
        if not force:                 # con force se reemplaza (Register-ScheduledTask usa -Force)
            for tn in ("SoouSync", "SoouCodexSync"):
                q = subprocess.run(["schtasks", "/Query", "/TN", tn],
                                   capture_output=True, text=True)
                if q.returncode == 0:
                    return f"ya existe una tarea de sync ({tn}); no se duplica."
        ps = (f"$a=New-ScheduledTaskAction -Execute '{exe}' -Argument '\"{script}\" sync';"
              f"$t=New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval "
              f"(New-TimeSpan -Minutes {interval_min});"
              f"$s=New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew;"
              f"Register-ScheduledTask -TaskName 'SoouSync' -Action $a -Trigger $t -Settings $s -Force|Out-Null")
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True)
        return (f"tarea 'SoouSync' creada (cada {interval_min} min)."
                if r.returncode == 0 else f"no se pudo crear la tarea: {(r.stderr or '')[:160]}")
    # unix: crontab
    line = f'*/{interval_min} * * * * "{exe}" "{script}" sync >/dev/null 2>&1  # soou-sync'
    cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    existing = cur.stdout if cur.returncode == 0 else ""
    if "# soou-sync" in existing and not force:
        return "ya existe la entrada cron soou-sync; no se duplica."
    if force:                         # saca la linea vieja antes de reescribirla
        existing = "".join(x + chr(10) for x in existing.splitlines()
                           if "# soou-sync" not in x)
    new = (existing + ("\n" if existing and not existing.endswith("\n") else "")) + line + "\n"
    p = subprocess.run(["crontab", "-"], input=new, capture_output=True, text=True)
    return (f"cron 'soou-sync' agregado (cada {interval_min} min)."
            if p.returncode == 0 else f"no se pudo agregar cron: {(p.stderr or '')[:160]}")


def _purgar_hooks_soou(hooks: dict) -> int:
    """Saca las entradas de soou de la config de hooks. El guardia viejo comparaba
    por subcadena y se negaba a reinstalar, dejando rutas muertas para siempre."""
    quitadas = 0
    for ev in list(hooks):
        nuevas = []
        for entrada in hooks.get(ev) or []:
            cmds = (entrada or {}).get("hooks") or []
            if any("soou.py" in str(c.get("command", "")) for c in cmds):
                quitadas += 1
                continue
            nuevas.append(entrada)
        if nuevas:
            hooks[ev] = nuevas
        else:
            hooks.pop(ev, None)
    return quitadas


def _install_claude_hooks(force: bool = False) -> str:
    if not shutil.which("claude"):
        return "claude no está instalado; se omiten los hooks (el trigger de sync ya cubre la captura)."
    settings = Path.home() / ".claude" / "settings.json"
    try:
        data = json.loads(settings.read_text(encoding="utf-8")) if settings.exists() else {}
    except Exception:
        return "no se pudo leer ~/.claude/settings.json; omito los hooks."
    _, script = _sync_exe()
    py = sys.executable
    hooks = data.setdefault("hooks", {})
    ya = "soou.py" in json.dumps(hooks)
    if ya and not force:
        return ("los hooks de soou ya estan en ~/.claude/settings.json; no se duplican "
                "(usa: soou.py status --repair para refrescar sus rutas).")
    if ya:
        _purgar_hooks_soou(hooks)     # reemplazo limpio, sin acumular rutas viejas
    # Solo los hooks POR SESION. Los por-turno (UserPromptSubmit->recall y
    # Stop->reindex) sumaban ~554 ms a cada vuelta en todos los proyectos, y el
    # recall ya esta disponible bajo demanda por MCP, que se registra a nivel
    # usuario. El indice lo refresca el sync programado.
    # PreCompact: captura ANTES de que el cliente compacte una sesion larga; el
    # SessionEnd posterior reescribe el mismo archivo (mismo sid), sin duplicar.
    events = {"SessionStart": "context", "PreCompact": "capture",
              "SessionEnd": "session-end"}
    for ev, sub in events.items():
        entry = {"hooks": [{"type": "command", "command": f'"{py}" "{script}" {sub}'}]}
        hooks.setdefault(ev, []).append(entry)
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return "hooks de Claude instalados (context/recall/reindex/session-end)."


def _registrar_mcp_en_claude(python_exe: str, mcp_script: str) -> str:
    """Registra soou en Claude Code con alcance de USUARIO, de forma idempotente.

    Dos correcciones: el alcance por defecto de `claude mcp add` es "local" (por
    proyecto), asi que el cerebro quedaba visible solo dentro de la carpeta de soou.
    Y reejecutar el alta NO sobrescribe: falla con "already exists". Por eso, si ya
    estaba, se remueve y se da de alta de nuevo: asi `init` es repetible y ademas
    repara la ruta cuando el repo o el interprete se movieron."""
    alta = ["claude", "mcp", "add", "soou", "--scope", "user",
            "--", python_exe, mcp_script]
    r = subprocess.run(alta, capture_output=True, text=True)
    if r.returncode == 0:
        return "Claude: registrado (alcance usuario)."
    salida = ((r.stdout or "") + " " + (r.stderr or "")).lower()
    if "already exists" in salida:
        subprocess.run(["claude", "mcp", "remove", "soou", "--scope", "user"],
                       capture_output=True, text=True)
        r = subprocess.run(alta, capture_output=True, text=True)
        if r.returncode == 0:
            return "Claude: ya estaba; ruta actualizada (alcance usuario)."
    detalle = ((r.stderr or r.stdout or "").strip())[:160]
    return f"Claude: no se pudo registrar (codigo {r.returncode}): {detalle}"


def _install_mcp() -> list[str]:
    statuses = []
    python_exe = sys.executable
    mcp_script = str(rutas.CODE_DIR / "soou_mcp.py")

    toml_path = Path.home() / ".codex" / "config.toml"
    # antes: si el config de Codex no existia, se omitia el registro en silencio.
    # Ahora manda si Codex ESTA INSTALADO; el config se crea si hace falta.
    if not shutil.which("codex"):
        statuses.append("Codex: no instalado; se omite.")
    elif toml_path.exists() and "[mcp_servers.soou]" in toml_path.read_text(encoding="utf-8"):
        statuses.append("Codex: ya registrado.")
    else:
        toml_path.parent.mkdir(parents=True, exist_ok=True)
        block = (f"\n\n[mcp_servers.soou]\n"
                 f"command = '{python_exe}'\n"
                 f"args = ['{mcp_script}']\n")
        with toml_path.open("a", encoding="utf-8") as config:
            config.write(block)
        statuses.append("Codex: registrado.")

    if not shutil.which("claude"):
        statuses.append("Claude: no instalado; se omite.")
    else:
        statuses.append(_registrar_mcp_en_claude(python_exe, mcp_script))

    return statuses


def _install_shortcut() -> str:
    """Crea un acceso directo de escritorio a la UI (Windows), sin consola (pythonw)."""
    if os.name != "nt":
        return "solo Windows (en Linux/Mac lanzá con: python soou.py ui)"
    try:
        pyw = Path(sys.executable).with_name("pythonw.exe")
        target = str(pyw if pyw.exists() else Path(sys.executable))
        script = str(rutas.CODE_DIR / "soou.py")
        lnk = os.path.join(os.path.expanduser("~"), "Desktop", "soou.lnk")
        ps = (f"$w=New-Object -ComObject WScript.Shell;$s=$w.CreateShortcut('{lnk}');"
              f"$s.TargetPath='{target}';$s.Arguments='\"{script}\" ui';"
              f"$s.WorkingDirectory='{str(rutas.ROOT)}';$s.Description='soou UI';$s.Save()")
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True)
        if os.path.exists(lnk):
            return "acceso directo creado en el escritorio: soou.lnk"
        return "no se pudo crear el acceso directo: " + (r.stderr or "")[:160]
    except Exception as e:
        return f"error creando el acceso directo: {e}"


def _rutas_de_comando(texto: str) -> list:
    """Rutas entrecomilladas dentro de una linea de comando."""
    return re.findall(r'"([^"]+)"', texto or "")


def _tarea_sync_instalada():
    """(ejecutable, argumentos) del disparador de sync, o None si no esta.

    En Windows se consulta por objeto con PowerShell en vez de parsear la salida
    de schtasks, que viene traducida al idioma del sistema."""
    if os.name == "nt":
        ps = ("$t=Get-ScheduledTask -TaskName 'SoouSync' -ErrorAction SilentlyContinue;"
              "if($t){$a=$t.Actions[0];Write-Output $a.Execute;Write-Output $a.Arguments}")
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                               capture_output=True, text=True, timeout=60)
        except Exception:
            return None
        lineas = [l.strip() for l in (r.stdout or "").splitlines() if l.strip()]
        if not lineas:
            return None
        return lineas[0].strip('"'), (lineas[1] if len(lineas) > 1 else "")
    try:
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=30)
    except Exception:
        return None
    for linea in (r.stdout or "").splitlines():
        if "# soou-sync" in linea:
            partes = _rutas_de_comando(linea)
            return (partes[0] if partes else ""), " ".join(partes[1:])
    return None


def _chequear_instalacion() -> list:
    """Estado de cada pieza: [(nombre, ok, detalle, clave_reparable)]."""
    guion = str(rutas.CODE_DIR / "soou.py")
    fuera = []

    tarea = _tarea_sync_instalada()
    if not tarea:
        fuera.append(("sync automatico", False, "no instalado", "trigger"))
    else:
        exe, argumentos = tarea
        partes = _rutas_de_comando(argumentos)
        apunta = any(Path(x) == Path(guion) for x in partes)
        if not Path(exe).exists():
            fuera.append(("sync automatico", False,
                          "el interprete ya no existe: " + exe, "trigger"))
        elif not apunta:
            fuera.append(("sync automatico", False,
                          "apunta a otra copia de soou.py: " + (partes[0] if partes else "?"),
                          "trigger"))
        else:
            fuera.append(("sync automatico", True, "tarea viva con " + exe, None))

    ajustes = Path.home() / ".claude" / "settings.json"
    try:
        datos = json.loads(ajustes.read_text(encoding="utf-8")) if ajustes.exists() else {}
    except Exception:
        datos = {}
    comandos = []
    for entradas in (datos.get("hooks") or {}).values():
        for entrada in entradas or []:
            for h in (entrada or {}).get("hooks") or []:
                if "soou.py" in str(h.get("command", "")):
                    comandos.append(str(h.get("command")))
    if not comandos:
        fuera.append(("hooks de Claude", False, "no instalados", "hooks"))
    else:
        rotos = []
        for c in comandos:
            partes = _rutas_de_comando(c)
            if not partes or not Path(partes[0]).exists():
                rotos.append("interprete muerto")
            elif len(partes) > 1 and Path(partes[1]) != Path(guion):
                rotos.append("apunta a otra copia")
        if rotos:
            fuera.append(("hooks de Claude", False,
                          str(len(rotos)) + " de " + str(len(comandos)) + " rotos: "
                          + rotos[0], "hooks"))
        else:
            fuera.append(("hooks de Claude", True, str(len(comandos)) + " hooks vivos", None))

    cfg_claude = Path.home() / ".claude.json"
    try:
        dc = json.loads(cfg_claude.read_text(encoding="utf-8")) if cfg_claude.exists() else {}
    except Exception:
        dc = {}
    srv = (dc.get("mcpServers") or {}).get("soou")
    if not srv:
        fuera.append(("servidor MCP", False, "no registrado a nivel usuario", "mcp"))
    else:
        cmd = str(srv.get("command", ""))
        args = [str(a) for a in (srv.get("args") or [])]
        if not Path(cmd).exists():
            fuera.append(("servidor MCP", False, "el interprete ya no existe: " + cmd, "mcp"))
        elif not any(Path(a) == Path(rutas.CODE_DIR / "soou_mcp.py") for a in args):
            fuera.append(("servidor MCP", False, "apunta a otra copia de soou_mcp.py", "mcp"))
        else:
            fuera.append(("servidor MCP", True, "registrado con " + cmd, None))

    try:
        hb = json.loads(rutas.HEARTBEAT_PATH.read_text(encoding="utf-8"))
        marca = dt.datetime.fromisoformat(hb["ts"])
        minutos = int((dt.datetime.now() - marca).total_seconds() // 60)
        vivo = minutos <= 30
        fuera.append(("ultimo sync", vivo,
                      ("hace " + str(minutos) + " min") if vivo
                      else ("hace " + str(minutos) + " min: la automatizacion no esta corriendo"),
                      None))
    except Exception:
        fuera.append(("ultimo sync", False, "nunca corrio", None))

    return fuera


def cmd_status(args):
    """Verifica que la automatizacion este instalada Y VIVA; con --repair la arregla."""
    print("== soou status ==" + chr(10))
    with consola.Girador("verificando la instalacion..."):
        filas = _chequear_instalacion()
    ancho = max(len(n) for n, _, _, _ in filas)
    for nombre, ok, detalle, _ in filas:
        print("  " + ("[si]" if ok else "[--]") + " " + nombre.ljust(ancho) + "  " + detalle)
    rotos = {clave for _, ok, _, clave in filas if not ok and clave}
    if not rotos:
        print(chr(10) + "Todo en orden.")
        return
    if not getattr(args, "repair", False):
        print(chr(10) + "Hay " + str(len(rotos)) + " pieza(s) que reparar. "
              "Corre:  python soou.py status --repair")
        return
    print(chr(10) + "Reparando:")
    if "trigger" in rotos:
        print("  " + _install_sync_trigger(getattr(args, "interval", 5) or 5, force=True))
    if "hooks" in rotos:
        print("  " + _install_claude_hooks(force=True))
    if "mcp" in rotos:
        for linea in _install_mcp():
            print("  " + linea)
    print(chr(10) + "Verificando de nuevo:")
    for nombre, ok, detalle, _ in _chequear_instalacion():
        print("  " + ("[si]" if ok else "[--]") + " " + nombre.ljust(ancho) + "  " + detalle)


def _quitar_seccion_toml(texto: str, seccion: str) -> str:
    """Saca una seccion [x] de un TOML sin dependencias: descarta desde su cabecera
    hasta la proxima cabecera de seccion o el final del archivo."""
    salida, dentro = [], False
    for linea in texto.splitlines(keepends=True):
        limpia = linea.strip()
        if limpia.startswith("["):
            dentro = (limpia == "[" + seccion + "]")
        if not dentro:
            salida.append(linea)
    resto = "".join(salida).rstrip()
    return (resto + chr(10)) if resto else ""


def _desinstalar_trigger(dry: bool) -> str:
    if os.name == "nt":
        halladas = []
        for tn in ("SoouSync", "SoouCodexSync"):
            q = subprocess.run(["schtasks", "/Query", "/TN", tn],
                               capture_output=True, text=True)
            if q.returncode == 0:
                halladas.append(tn)
        if not halladas:
            return "sync automatico: no habia tarea programada."
        if dry:
            return "sync automatico: se quitaria(n) " + ", ".join(halladas) + "."
        quitadas = [tn for tn in halladas
                    if subprocess.run(["schtasks", "/Delete", "/TN", tn, "/F"],
                                      capture_output=True, text=True).returncode == 0]
        return ("sync automatico: quitada(s) " + ", ".join(quitadas) + "."
                if quitadas else "sync automatico: no se pudo quitar la tarea.")
    try:
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    except Exception:
        return "sync automatico: no se pudo leer el crontab."
    actual = r.stdout if r.returncode == 0 else ""
    if "# soou-sync" not in actual:
        return "sync automatico: no habia entrada cron."
    if dry:
        return "sync automatico: se quitaria la entrada cron soou-sync."
    nuevo = "".join(x + chr(10) for x in actual.splitlines() if "# soou-sync" not in x)
    w = subprocess.run(["crontab", "-"], input=nuevo, capture_output=True, text=True)
    return ("sync automatico: entrada cron quitada." if w.returncode == 0
            else "sync automatico: no se pudo reescribir el crontab.")


def _desinstalar_hooks(dry: bool) -> str:
    ajustes = Path.home() / ".claude" / "settings.json"
    if not ajustes.exists():
        return "hooks de Claude: no habia settings.json."
    try:
        datos = json.loads(ajustes.read_text(encoding="utf-8"))
    except Exception:
        return "hooks de Claude: no se pudo leer settings.json; se deja intacto."
    hooks = datos.get("hooks") or {}
    if "soou.py" not in json.dumps(hooks):
        return "hooks de Claude: no habia hooks de soou."
    if dry:
        return "hooks de Claude: se quitarian sus entradas de ~/.claude/settings.json."
    quitadas = _purgar_hooks_soou(hooks)
    datos["hooks"] = hooks
    try:
        ajustes.write_text(json.dumps(datos, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        return "hooks de Claude: no se pudo escribir settings.json: " + str(e)
    return ("hooks de Claude: " + str(quitadas) +
            " entrada(s) quitada(s); las de otras herramientas quedan intactas.")


def _desinstalar_mcp(dry: bool) -> list:
    salidas = []
    cfg = Path.home() / ".claude.json"
    registrado = False
    try:
        if cfg.exists():
            datos = json.loads(cfg.read_text(encoding="utf-8"))
            registrado = "soou" in (datos.get("mcpServers") or {})
    except Exception:
        registrado = False
    if not registrado:
        salidas.append("MCP en Claude: no estaba registrado.")
    elif dry:
        salidas.append("MCP en Claude: se quitaria del alcance de usuario.")
    elif not shutil.which("claude"):
        salidas.append("MCP en Claude: figura registrado pero claude no esta instalado; "
                       "quitalo a mano de ~/.claude.json.")
    else:
        r = subprocess.run(["claude", "mcp", "remove", "soou", "--scope", "user"],
                           capture_output=True, text=True)
        salidas.append("MCP en Claude: quitado." if r.returncode == 0
                       else "MCP en Claude: no se pudo quitar: "
                            + ((r.stderr or r.stdout or "").strip())[:120])

    toml_path = Path.home() / ".codex" / "config.toml"
    try:
        texto = toml_path.read_text(encoding="utf-8") if toml_path.exists() else ""
    except OSError:
        texto = ""
    if "[mcp_servers.soou]" not in texto:
        salidas.append("MCP en Codex: no estaba registrado.")
    elif dry:
        salidas.append("MCP en Codex: se quitaria la seccion de su config.toml.")
    else:
        try:
            toml_path.write_text(_quitar_seccion_toml(texto, "mcp_servers.soou"),
                                 encoding="utf-8")
            salidas.append("MCP en Codex: seccion quitada.")
        except OSError as e:
            salidas.append("MCP en Codex: no se pudo escribir config.toml: " + str(e))
    return salidas


def _desinstalar_shortcut(dry: bool) -> str:
    lnk = Path(os.path.expanduser("~")) / "Desktop" / "soou.lnk"
    if not lnk.exists():
        return "acceso directo: no habia."
    if dry:
        return "acceso directo: se borraria " + str(lnk) + "."
    try:
        lnk.unlink()
        return "acceso directo: borrado del escritorio."
    except OSError as e:
        return "acceso directo: no se pudo borrar: " + str(e)


_DERIVADOS = ("soou.db", "soou.db-wal", "soou.db-shm", ".soou-heartbeat.json",
              ".soou-budget.json", ".distill.lock", ".distill-lite.lock",
              "router-log.jsonl", "graph.html", ".index.json", "last-sync.json")


def _borrar_derivados(dry: bool) -> str:
    """Borra SOLO artefactos derivados, todos reconstruibles con reindex.
    Nunca toca notas, sesiones ni fuentes."""
    presentes = [n for n in _DERIVADOS if (rutas.ROOT / n).exists()]
    if not presentes:
        return "cache derivada: no habia nada que borrar."
    if dry:
        return "cache derivada: se borrarian " + str(len(presentes)) + " archivo(s)."
    borrados = 0
    for n in presentes:
        try:
            (rutas.ROOT / n).unlink()
            borrados += 1
        except OSError:
            pass
    return ("cache derivada: " + str(borrados)
            + " archivo(s) borrado(s); se rehacen con 'soou.py reindex'.")


def cmd_uninstall(args):
    """Quita la INTEGRACION de soou con el sistema. Tu conocimiento no se toca:
    wiki/, sessions/ y sources/ quedan donde estan."""
    dry = getattr(args, "dry_run", False)
    print("== soou uninstall ==" + (" (simulacion)" if dry else "") + chr(10))
    print("  " + _desinstalar_trigger(dry))
    print("  " + _desinstalar_hooks(dry))
    for linea in _desinstalar_mcp(dry):
        print("  " + linea)
    print("  " + _desinstalar_shortcut(dry))
    if getattr(args, "cache", False):
        print("  " + _borrar_derivados(dry))
    print()
    if dry:
        print("Simulacion: no se toco nada. Corre sin --dry-run para aplicarlo.")
        return
    print("Tu conocimiento NO se toco. Sigue aca:")
    print("   " + str(rutas.WIKI))
    print("   " + str(rutas.SESSIONS.parent))
    print("   " + str(rutas.SOURCES))
    print("Si tambien lo queres borrar, borra esas carpetas a mano: es tuyo, "
          "no lo decide el desinstalador.")


def _acciones_init(args) -> list:
    """Lo que `init` haria, en el orden en que lo hace: (titulo, detalle).
    Es la fuente de --preview y de la confirmacion: lo que se muestra es lo que
    se ejecuta, no una descripcion aparte que se pueda desincronizar."""
    acc = [("Workspace", f"datos en {rutas.ROOT}: soou.config.json si falta, notas de "
                         "ejemplo si wiki/ esta vacia, carpetas e indice soou.db")]
    if not getattr(args, "no_trigger", False):
        cada = getattr(args, "interval", 5) or 5
        donde = ("tarea 'SoouSync' en el Programador de tareas de Windows" if os.name == "nt"
                 else "una linea '# soou-sync' en tu crontab")
        acc.append(("Sincronizacion automatica", f"{donde}, cada {cada} min: cosecha y "
                    "destila sesiones de Claude Code y Codex bajo tu carpeta personal"))
    if not getattr(args, "no_hooks", False):
        acc.append(("Hooks de Claude Code", "agrega a ~/.claude/settings.json la captura de "
                    "cada sesion (SessionEnd, PreCompact) y el recall que inyecta notas "
                    "relevantes en cada prompt (UserPromptSubmit)"))
    if not getattr(args, "no_mcp", False):
        acc.append(("Servidor MCP", "registra soou en ~/.codex/config.toml y en Claude Code "
                    "(~/.claude.json): los agentes podran consultar y escribir el cerebro"))
    if getattr(args, "shortcut", False):
        acc.append(("Acceso directo", "un acceso a la interfaz en el escritorio"))
    return acc


def _mostrar_acciones(acciones):
    for i, (titulo, detalle) in enumerate(acciones, 1):
        print(f"  {i}) {titulo}: {detalle}")


def cmd_init(args):
    consola.mostrar_marca("segundo cerebro + harness multiagente")
    if getattr(args, "minimal", False):            # solo workspace e indice
        args.no_trigger = args.no_hooks = args.no_mcp = True
        args.shortcut = False
    acciones = _acciones_init(args)
    if getattr(args, "preview", False):
        print("== soou init --preview: esto es lo que se haria (no se cambia nada) ==")
        _mostrar_acciones(acciones)
        print("\nPara probar sin integraciones: soou init --minimal  ·  para deshacer: soou uninstall")
        return
    if (len(acciones) > 1 and not getattr(args, "yes", False)
            and sys.stdin is not None and sys.stdin.isatty()):
        print("soou init va a:")
        _mostrar_acciones(acciones)
        try:
            resp = input("\n¿Continuar? [s/N] ").strip().lower()
        except EOFError:
            resp = ""
        if resp not in ("s", "si", "sí", "y", "yes"):
            print("Cancelado; no se cambio nada. Prueba 'soou init --minimal' o --preview.")
            return
    print("== soou init ==")
    print("1) Workspace:")
    for x in _bootstrap_workspace():
        print(f"   ✓ {x}")
    print("2) Agentes:")
    inst = [ad.name for ad in registro.build_adapters() if ad.available()]
    print(f"   detectados: {', '.join(inst) or 'ninguno (instalá una IA agéntica)'}")
    if not getattr(args, "no_trigger", False):
        print("3) Sincronización automática (universal):")
        with consola.Girador("programando la sincronizacion..."):
            estado = _install_sync_trigger(getattr(args, "interval", 5) or 5)
        print("   " + estado)
    if not getattr(args, "no_hooks", False):
        print("4) Hooks de Claude (captura/recall en tiempo real):")
        print("   " + _install_claude_hooks())
    if not getattr(args, "no_mcp", False):
        print("5) MCP:")
        for status in _install_mcp():
            print("   " + status)
    if getattr(args, "shortcut", False):
        print("6) Acceso directo:")
        print("   " + _install_shortcut())
    print("\nListo. Probá:  soou.py sync   ·   soou.py agents --probe   ·   soou.py ui")
    print("Para deshacer las integraciones: soou.py uninstall")
