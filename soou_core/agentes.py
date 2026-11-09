"""Adaptadores de agentes CLI (Claude Code, Codex, recetas para otros 13 y
binarios genericos). Lanzan el proceso y normalizan la salida; la seleccion
y el routing viven en soou.py.

Extraido de soou.py (que lo re-exporta: `soou.<nombre>` sigue valiendo)."""

from pathlib import Path
import json
import os
import re
import shutil
import subprocess
import sys

from soou_core import rutas


class AgentAdapter:
    """Interfaz común. Lanza un agente headless y devuelve {ok, output}."""
    name = "base"
    probe_args = ["--version"]   # invocación barata para el preflight
    can_write = True             # False = solo lectura/plan (no aplica cambios en disco)
    # Qué restricción APLICA el agente cuando soou le pide solo lectura (no qué modo
    # se pidió): "sandbox_os" (el SO lo impide), "aprobacion_cli" (el propio agente
    # niega las herramientas de escritura), "texto" (solo devuelve texto: API),
    # "cliente_acp" (soou concede o niega cada herramienta por ACP),
    # "no_verificada" o None (sin modo lectura). Solo las de RESTRICCIONES_LECTURA
    # cuentan como lectura segura; con las demás, la lectura se rechaza.
    restriccion_lectura = None

    def _bin(self) -> str:
        """Nombre/ruta del ejecutable a resolver (override por adaptador)."""
        return self.name

    def available(self) -> bool:
        return False

    def resolve(self):
        """Ruta real del ejecutable, o None. Ruta absoluta/con separador → tal cual."""
        b = self._bin()
        if os.path.isabs(b) or ("/" in b) or ("\\" in b):
            return b if Path(b).exists() else None
        return shutil.which(b)

    def probe(self, timeout: int = 25):
        """PREFLIGHT: lanza el agente DE VERDAD (--version) para confirmar que se puede
        ejecutar. Combate el falso positivo de 'existe en PATH' pero 'no se puede lanzar'
        (el caso de codex.CMD → WinError 2). Devuelve (ok, detalle)."""
        try:
            r = _launch([self._bin(), *self.probe_args], timeout=timeout)
            out = " ".join(((r.stdout or "") + " " + (r.stderr or "")).split())[:90]
            return (r.returncode == 0, out or "(sin salida)")
        except Exception as e:
            return (False, str(e)[:150])

    def run(self, prompt: str, allow_write: bool = False, timeout: int = 1200,
            model=None, reasoning=None) -> dict:
        raise NotImplementedError


# "cliente_acp": el agente pide cada herramienta por ACP y soou decide (soou_core/acp.py).
RESTRICCIONES_LECTURA = ("sandbox_os", "aprobacion_cli", "cliente_acp", "texto")


class LanzamientoRechazado(Exception):
    """soou se niega a lanzar: el comando no se puede construir de forma segura."""


# Lista BLANCA (no negra) de lo que puede ir sin riesgo en una linea de cmd.exe.
_ARG_SEGURO_CMD = re.compile(r"[A-Za-z0-9 _\-.=:/\\,+@~]*\Z")


def _script_de_shim(exe):
    """[node, script.js] de un shim .cmd de npm, o None si no tiene esa forma.
    Lanzar node directo evita que cmd.exe reinterprete los argumentos."""
    try:
        txt = Path(exe).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    m = re.search(r'"%dp0%\\([^"%]+?\.[cm]?js)"', txt)
    if not m:
        return None
    # el shim usa "\\" como separador: partirlo vale tambien fuera de Windows
    script = Path(exe).parent.joinpath(*m.group(1).split("\\"))
    local = Path(exe).parent / "node.exe"
    node = str(local) if local.exists() else shutil.which("node")
    return [node, str(script)] if (node and script.exists()) else None


def rechazo(nombre, motivo) -> dict:
    """Resultado uniforme cuando el agente no puede cumplir lo pedido: no se lanza."""
    return {"ok": False, "estado": "rechazado", "codigo_error": "E_PERMISO",
            "output": f"{nombre}: rechazado: {motivo}"}


def lectura_segura(adaptador) -> bool:
    # sin restriccion declarada no es lectura segura (y no debe tumbar el run)
    return getattr(adaptador, "restriccion_lectura", None) in RESTRICCIONES_LECTURA


def _comando(argv):
    """argv listo para Popen, con el blindaje de Windows: shim de npm -> node
    directo; otro .cmd/.bat -> cmd /c solo con argumentos de la lista blanca;
    .ps1 -> powershell -File. Lanza LanzamientoRechazado si no es seguro."""
    raw = argv[0]
    exe = raw if (os.path.isabs(raw) or "/" in raw or "\\" in raw) else (shutil.which(raw) or raw)
    rest = list(argv[1:])
    low = exe.lower()
    if os.name == "nt" and low.endswith((".cmd", ".bat")):
        directo = _script_de_shim(exe)
        if directo:                      # npm: node <script.js>, sin pasar por cmd
            cmdarg = [*directo, *rest]
        else:
            # cmd reinterpreta comillas, %VAR%, &, |, saltos de linea... dentro de la
            # linea: solo se admiten argumentos de una lista blanca estricta. El texto
            # libre (el prompt) tiene que ir por stdin o por archivo.
            malos = [a for a in rest if not _ARG_SEGURO_CMD.match(str(a))]
            if malos:
                raise LanzamientoRechazado(
                    f"{Path(exe).name} es un script de cmd y {len(malos)} argumento(s) "
                    "no son seguros en su linea de comandos; pasa el prompt por stdin "
                    "o instala el agente como ejecutable")
            cmdarg = 'cmd /c "' + subprocess.list2cmdline([exe, *rest]) + '"'
    elif os.name == "nt" and low.endswith(".ps1"):
        cmdarg = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", exe, *rest]
    else:
        cmdarg = [exe, *rest]
    return cmdarg


def _launch(argv, *, stdin_text=None, timeout=1200, cwd=None):
    """Lanza argv con el guard BRAIN_DISTILL=1 (corta los hooks de soou), robusto a cómo
    esté instalado el agente:
    - ruta absoluta / con separador → se usa tal cual (agente fuera de PATH);
    - si no, se resuelve por PATH con `which` (respeta PATHEXT: .exe/.cmd/…);
    - shim .cmd/.bat (npm) → `cmd /c "..."` con comillas exteriores (subprocess no ejecuta
      .cmd directo → WinError 2; y las rutas con espacios rompen cmd sin las comillas);
    - script .ps1 → `powershell -File`.
    Para WSL/docker/`python -m`, el command del GenericAdapter YA es el launcher (wsl/docker/
    python) y esto lo lanza directo."""
    cmdarg = _comando(argv)
    env = dict(os.environ, BRAIN_DISTILL="1")
    # CREATE_NO_WINDOW: sin esto, cada agente lanzado abre una consola (cmd) que
    # aparece/desaparece en Windows — molesto al orquestar varias subtareas.
    noconsole = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    proc = subprocess.Popen(cmdarg, stdin=subprocess.PIPE if stdin_text is not None else
                            subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace", env=env, cwd=cwd,
                            creationflags=noconsole, start_new_session=(os.name != "nt"))
    try:
        out, err = proc.communicate(input=stdin_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        # matar SOLO el proceso no alcanza: un agente deja hijos (node, shells,
        # tests) que siguen escribiendo en el worktree mientras se limpia o se
        # lanza el siguiente agente. Se mata el arbol entero antes de seguir.
        _matar_arbol(proc)
        try:
            proc.communicate(timeout=10)
        except Exception:
            pass
        raise
    return subprocess.CompletedProcess(cmdarg, proc.returncode, out, err)


def _matar_arbol(proc):
    """Termina un proceso y todos sus descendientes."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           capture_output=True, timeout=30,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            import signal
            os.killpg(proc.pid, signal.SIGKILL)       # su propio grupo de procesos
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass


def fallo_lanzamiento(nombre, e) -> dict:
    """Resultado uniforme cuando el lanzamiento no llego a buen termino."""
    if isinstance(e, LanzamientoRechazado):
        return rechazo(nombre, str(e))
    if isinstance(e, subprocess.TimeoutExpired):
        return {"ok": False, "estado": "timeout", "codigo_error": "E_TIMEOUT",
                "output": f"{nombre}: tiempo agotado ({e.timeout}s); se detuvo el agente y sus procesos"}
    return {"ok": False, "estado": "fallo", "codigo_error": "E_LANZAMIENTO",
            "output": f"error lanzando {nombre}: {e}"}


class ClaudeAdapter(AgentAdapter):
    name = "claude-code"
    restriccion_lectura = "aprobacion_cli"

    def _bin(self) -> str:
        return "claude"

    def available(self) -> bool:
        return self.resolve() is not None

    def run(self, prompt, allow_write=False, timeout=1200, cwd=None, model=None,
            reasoning=None) -> dict:
        tools = ["Read", "Grep", "Glob"] + (["Write", "Edit"] if allow_write else [])
        argv = ["claude", "-p"]                    # el prompt va por stdin
        if model:
            argv += ["--model", model]
        argv += ["--allowedTools", *tools]
        if allow_write:
            argv += ["--permission-mode", "acceptEdits"]
        else:
            # acceptEdits aprobaria ediciones aunque no esten en allowedTools: en
            # lectura se usa el modo por defecto (headless niega lo no permitido) y
            # ademas se niegan explicitamente las herramientas que escriben.
            argv += ["--permission-mode", "default",
                     "--disallowedTools", "Write", "Edit", "NotebookEdit", "Bash"]
        argv += ["--output-format", "json"]
        if rutas.en_evaluacion():
            # eval: sin servidores MCP (ni el de soou), sin hooks (los de soou
            # inyectan el cerebro real) y sin guardar la sesion. Igual con y sin
            # memoria: solo cambia el contexto que va en el prompt.
            argv += ["--strict-mcp-config", "--settings", '{"disableAllHooks":true}',
                     "--no-session-persistence"]
        try:
            r = _launch(argv, stdin_text=prompt, timeout=timeout, cwd=cwd)
        except Exception as e:
            return fallo_lanzamiento(self.name, e)
        try:
            d = json.loads(r.stdout)
            return {"ok": (not d.get("is_error")) and r.returncode == 0,
                    "output": d.get("result", "")}
        except Exception:
            return {"ok": r.returncode == 0, "output": (r.stdout or r.stderr or "")}


class CodexAdapter(AgentAdapter):
    name = "codex"
    restriccion_lectura = "sandbox_os"          # -s read-only

    def _bin(self) -> str:
        return "codex"

    def available(self) -> bool:
        return self.resolve() is not None

    def run(self, prompt, allow_write=False, timeout=1200, cwd=None, model=None,
            reasoning=None) -> dict:
        # allow_write: `--approve-for-me` = escritura autónoma auto-aprobada en el
        # workspace (implica sandbox workspace-write; por eso NO admite -s a la vez).
        # Sin escritura: -s read-only (planificar/verificar sin tocar archivos).
        # -C fija la raíz de trabajo (el worktree aislado); sin él, codex exec en un
        # dir nuevo solo DESCRIBÍA el cambio en vez de aplicarlo.
        flags = ["--approve-for-me"] if allow_write else ["-s", "read-only"]
        # el worktree aislado NO está en los [projects] trusted de Codex → sin esto falla
        # con "Not inside a trusted directory"; lo salteamos (ya estamos sandboxeados por el worktree).
        flags += ["--skip-git-repo-check"]
        if cwd:
            flags += ["-C", str(cwd)]
        if model:
            flags += ["-m", model]
        if reasoning:
            flags += ["-c", "model_reasoning_effort=" + reasoning]
        if rutas.en_evaluacion():
            # eval: sin el MCP de soou (no consulta el cerebro real) y sin guardar la
            # sesion. Codex no tiene hooks de soou; el resto de su configuracion
            # es la misma con y sin memoria.
            flags += ["-c", "mcp_servers.soou.enabled=false", "--ephemeral"]
        # el prompt va por STDIN (`-`) para no pelear con el quoting de cmd/.CMD
        try:
            r = _launch(["codex", "exec", *flags, "-"], stdin_text=prompt,
                        timeout=timeout, cwd=cwd)
        except Exception as e:
            return fallo_lanzamiento(self.name, e)
        return {"ok": r.returncode == 0, "output": (r.stdout or r.stderr or "")}


class GenericAdapter(AgentAdapter):
    """Conecta CUALQUIER IA/CLI por config (Eje 6, agnóstico de proveedor).
    En soou.config.json:  {"name":"deepseek","type":"cmd",
                           "command":"deepseek","args":["run","{prompt}"]}
    {prompt} se sustituye por la tarea; si ningún arg lo trae, se añade al final."""
    def __init__(self, name, command, cmd_args=None, probe_args=None, lectura=None):
        self.name = name
        self._command = command
        self._args = cmd_args or ["{prompt}"]
        # un comando arbitrario no garantiza nada: la lectura solo se admite si el
        # usuario declara la restriccion que aplica ("lectura" en soou.config.json)
        self.restriccion_lectura = lectura
        if probe_args is not None:
            self.probe_args = probe_args

    def _bin(self) -> str:
        return self._command

    def available(self) -> bool:
        return self.resolve() is not None

    def run(self, prompt, allow_write=False, timeout=1200, cwd=None, model=None,
            reasoning=None) -> dict:
        if not allow_write and not lectura_segura(self):
            return rechazo(self.name, "comando generico sin restriccion de lectura declarada")
        argv = [self._command]
        used = False
        for a in self._args:
            if "{prompt}" in a:
                argv.append(a.replace("{prompt}", prompt)); used = True
            else:
                argv.append(a)
        if not used:
            argv.append(prompt)
        try:
            r = _launch(argv, timeout=timeout, cwd=cwd)   # maneja shims .cmd (gemini, etc.)
        except Exception as e:
            return fallo_lanzamiento(self.name, e)
        return {"ok": r.returncode == 0, "output": (r.stdout or r.stderr or "")}


RECIPE_REGISTRY = {
    "aider":        {"bins": ["aider"],
                     "model_flag": "--model",
                     "write": ["--yes-always", "--no-pretty", "--no-stream", "--message", "{prompt}"],
                     "read":  ["--dry-run", "--no-pretty", "--no-stream", "--message", "{prompt}"],
                     "lectura": "aprobacion_cli"},
    "gemini":       {"bins": ["gemini"],
                     "model_flag": "-m",
                     "write": ["--approval-mode", "yolo", "-p", "{prompt}"],
                     "read":  ["--approval-mode", "plan", "-p", "{prompt}"],
                     "lectura": "aprobacion_cli"},
    "qwen":         {"bins": ["qwen"],
                     "model_flag": "-m",
                     "write": ["--approval-mode", "yolo", "-p", "{prompt}"],
                     "read":  ["--approval-mode", "plan", "-p", "{prompt}"],
                     "lectura": "aprobacion_cli"},
    "cursor-agent": {"bins": ["cursor-agent"],
                     "write": ["-p", "--force", "--output-format", "text", "{prompt}"],
                     "read":  ["-p", "--output-format", "text", "{prompt}"],
                     "lectura": "no_verificada"},
    "opencode":     {"bins": ["opencode"],
                     "model_flag": "-m",
                     "write": ["run", "{prompt}", "--auto"], "read": ["run", "{prompt}"],
                     "lectura": "no_verificada"},
    "goose":        {"bins": ["goose"],
                     "write": ["run", "--no-session", "-t", "{prompt}"],
                     "read":  None},          # mismo comando que write: no hay solo lectura
    "amp":          {"bins": ["amp"],
                     "write": ["-x", "{prompt}"], "read": None},
    "crush":        {"bins": ["crush"],
                     "write": ["run", "--yolo", "{prompt}"], "read": ["run", "{prompt}"],
                     "lectura": "no_verificada"},
    "copilot":      {"bins": ["copilot"],
                     "write": ["-p", "{prompt}", "--allow-all-tools"],
                     "read":  ["-p", "{prompt}"],
                     "lectura": "aprobacion_cli"},   # sin --allow-all-tools niega herramientas
    "openhands":    {"bins": ["openhands"],
                     "write": ["--headless", "-t", "{prompt}"],
                     "read":  None},
    "droid":        {"bins": ["droid"],
                     "model_flag": "-m",
                     "write": ["exec", "--auto", "medium", "{prompt}"],
                     "read":  ["exec", "{prompt}"],
                     "lectura": "aprobacion_cli"},   # droid exec es read-only por defecto
    "amazon-q":     {"bins": ["q"],
                     "write": ["chat", "--no-interactive", "--trust-all-tools", "{prompt}"],
                     "read":  ["chat", "--no-interactive", "{prompt}"],
                     "lectura": "no_verificada"},
    "continue":     {"bins": ["cn"],
                     "write": ["-p", "{prompt}", "--allow", "*"],
                     "read":  ["-p", "{prompt}"],
                     "lectura": "no_verificada"},
}


class RecipeAdapter(AgentAdapter):
    """Agente genérico guiado por una receta del RECIPE_REGISTRY (o pasada en config).
    Cubre las CLIs 'prompt → aplica → imprime' (aider/gemini/qwen/cursor-agent/opencode/
    goose/amp/crush). El éxito = returncode 0 (sin parseo especial de salida)."""
    def __init__(self, name, recipe):
        self.name = name
        self.r = recipe
        self.probe_args = recipe.get("probe", ["--version"])
        self.restriccion_lectura = recipe.get("lectura") if recipe.get("read") else None

    def _bin(self) -> str:
        for b in self.r["bins"]:
            if shutil.which(b):
                return b
        return self.r["bins"][0]

    def available(self) -> bool:
        return any(shutil.which(b) for b in self.r["bins"])

    def run(self, prompt, allow_write=False, timeout=1200, cwd=None, model=None,
            reasoning=None) -> dict:
        if allow_write:
            tmpl = self.r["write"]
        else:
            # NUNCA se usa la receta de escritura para leer: sin modo lectura, o con
            # uno cuya restriccion no esta verificada, el agente queda fuera.
            tmpl = self.r.get("read")
            if not tmpl:
                return rechazo(self.name, "la receta no tiene modo de solo lectura")
            if not lectura_segura(self):
                return rechazo(self.name, "su modo lectura no esta verificado ("
                               + str(self.restriccion_lectura) + "); si confias en el, "
                               'declara "lectura": "aprobacion_cli" en soou.config.json')
        por_stdin = self.r.get("prompt_via") == "stdin"
        argv = [self._bin()]
        if model and self.r.get("model_flag"):
            argv += [self.r["model_flag"], model]
        if por_stdin:
            argv += [t for t in tmpl if t != "{prompt}"]
        else:
            argv += [(prompt if t == "{prompt}" else t) for t in tmpl]
        try:
            r = _launch(argv, stdin_text=prompt if por_stdin else None,
                        timeout=timeout, cwd=cwd)
        except Exception as e:
            return fallo_lanzamiento(self.name, e)
        return {"ok": r.returncode == 0, "output": (r.stdout or r.stderr or "")}


def lanzar_desacoplado(args: list, cwd=None) -> bool:
    """Lanza `soou.py <args>` como proceso independiente, sin consola ni salida,
    que sobrevive a quien lo lanzo (un hook, la UI, una herramienta MCP)."""
    try:
        flags = 0
        if os.name == "nt":   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
            flags = 0x00000008 | subprocess.CREATE_NEW_PROCESS_GROUP
        codigo = Path(__file__).resolve().parent.parent
        subprocess.Popen([sys.executable, str(codigo / "soou.py"), *args],
                         cwd=str(cwd or os.getcwd()), creationflags=flags, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, env=dict(os.environ),
                         start_new_session=(os.name != "nt"))
        return True
    except Exception:
        return False
