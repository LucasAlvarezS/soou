"""Cliente ACP (Agent Client Protocol, version 1) minimo, solo libreria estandar.

soou lanza al agente como subproceso y habla JSON-RPC 2.0 por stdio: un mensaje
JSON por linea, UTF-8 (https://agentclientprotocol.com/protocol/transports).
Alcance: initialize, session/new, session/prompt, session/update (progreso),
session/cancel, session/request_permission y fs/read_text_file /
fs/write_text_file. No implementa terminales, autenticacion ni carga de
sesiones: si el agente las exige, el intento falla con un motivo claro.

La diferencia con una receta CLI es quien decide los permisos. En ACP el agente
PIDE cada herramienta por mensaje y soou contesta segun el modo: en lectura solo
concede herramientas de lectura dentro del workspace; en escritura concede
dentro del workspace y nunca fuera. Eso vale para un agente que respeta el
protocolo; uno que escriba sin pedir permiso no queda impedido por el
protocolo, y por eso el ejecutor igual comprueba despues que una lectura no
toco el workspace.

Capa base: importa solo `agentes` (lanzamiento seguro y matar el arbol)."""

import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path

from soou_core import agentes

VERSION_PROTOCOLO = 1
CLIENTE = {"name": "soou", "title": "soou", "version": "1"}

# Herramientas que no modifican nada (tool call `kind` del protocolo).
KINDS_LECTURA = frozenset({"read", "search", "think", "fetch"})
KINDS_ESCRITURA = frozenset({"edit", "delete", "move", "execute", "switch_mode", "other"})

# Modos de sesion (session/set_mode) que soou elige si el agente los anuncia. Un
# agente puede arrancar en un modo que se autoaprueba (codex-acp: "agent"); en
# ese modo no pide permiso para editar y la decision de soou no llega a correr.
MODOS_LECTURA = ("read-only", "readonly", "read_only", "plan", "ask")
MODOS_ESCRITURA = ("workspace-write", "workspace", "accept-edits", "acceptEdits", "edit",
                   "code", "default")
MODOS_PROHIBIDOS = ("full", "bypass", "yolo", "danger")   # nunca se eligen

MAX_EVENTOS = 400          # el progreso se guarda acotado en el resultado
GRACIA_CANCELAR_S = 10     # tras session/cancel, cuanto se espera la respuesta


class ErrorAcp(Exception):
    """El agente respondio con un error JSON-RPC o violo el protocolo."""

    def __init__(self, mensaje, codigo=None):
        super().__init__(mensaje)
        self.codigo = codigo


class _Vencido(Exception):
    pass


def _dentro(ruta, raiz) -> bool:
    try:
        p, r = Path(ruta), Path(raiz).resolve()
        if not p.is_absolute():
            return False
        p.resolve().relative_to(r)
        return True
    except (ValueError, OSError):
        return False


def decidir_permiso(tool_call: dict, opciones: list, escritura: bool, raiz) -> tuple:
    """(outcome, concedido, motivo) para session/request_permission.

    Lectura: solo `kind` de lectura y, si hay `locations`, todas dentro de la
    raiz. Un kind desconocido o ausente se niega. Escritura: se concede dentro
    de la raiz; una ubicacion fuera se niega. Nunca se elige una opcion
    "always": cada herramienta se vuelve a preguntar."""
    kind = tool_call.get("kind") or None
    locs = [l.get("path") for l in (tool_call.get("locations") or []) if isinstance(l, dict)]
    fuera = [p for p in locs if not _dentro(p, raiz)]
    if escritura:
        conceder = not fuera
        motivo = "fuera del workspace: " + ", ".join(map(str, fuera[:3])) if fuera else \
            f"escritura dentro del workspace ({kind or 'sin kind'})"
    else:
        conceder = kind in KINDS_LECTURA and not fuera
        motivo = (f"lectura ({kind})" if conceder else
                  f"modo lectura: se niega '{kind or 'sin kind'}'" if kind not in KINDS_LECTURA
                  else "lectura fuera del workspace")
    prefs = ("allow_once", "allow_always") if conceder else ("reject_once", "reject_always")
    if not escritura and conceder:
        prefs = ("allow_once",)                # en lectura, ni siquiera "siempre"
    for k in prefs:
        op = next((o for o in opciones if o.get("kind") == k), None)
        if op:
            return {"outcome": "selected", "optionId": op.get("optionId")}, conceder, motivo
    return {"outcome": "cancelled"}, False, motivo + " (sin opcion adecuada: cancelado)"


class ClienteAcp:
    """Una conexion con un agente ACP: un proceso, una sesion, un turno."""

    def __init__(self, argv, cwd, escritura: bool, timeout: int = 1200):
        self.argv, self.cwd, self.escritura = list(argv), str(Path(cwd).resolve()), escritura
        self.limite = time.monotonic() + timeout
        self.timeout = timeout
        self._id = 0
        self._entrada = queue.Queue()
        self._stderr = []
        self.proc = None
        self._hilos = []
        self.sesion = None
        self.texto = []
        self.eventos = []
        self.herramientas = {}      # toolCallId -> ultimo estado conocido
        self.uso = None
        self.capacidades = {}
        self.permisos = []          # (kind, concedido, motivo)

    # ------------------------------------------------------------ proceso
    def iniciar(self):
        cmd = agentes._comando(self.argv)
        env = dict(os.environ, BRAIN_DISTILL="1")
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=self.cwd, env=env, bufsize=0,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            start_new_session=(os.name != "nt"))
        self._hilos = [threading.Thread(target=f, daemon=True)
                       for f in (self._leer_stdout, self._leer_stderr)]
        for h in self._hilos:
            h.start()

    def _leer_stdout(self):
        for linea in iter(self.proc.stdout.readline, b""):
            linea = linea.strip()
            if not linea:
                continue
            try:
                self._entrada.put(json.loads(linea.decode("utf-8")))
            except (ValueError, UnicodeDecodeError):
                # el protocolo prohibe escribir otra cosa en stdout
                self._entrada.put({"_invalido": linea[:200].decode("utf-8", "replace")})
        self._entrada.put(None)    # EOF

    def _leer_stderr(self):
        for linea in iter(self.proc.stderr.readline, b""):
            self._stderr.append(linea.decode("utf-8", "replace").rstrip())
            del self._stderr[:-40]

    def _enviar(self, msg: dict):
        datos = json.dumps(msg, ensure_ascii=False, separators=(",", ":"))
        try:
            self.proc.stdin.write(datos.encode("utf-8") + b"\n")
            self.proc.stdin.flush()
        except OSError as e:
            raise ErrorAcp(f"el agente cerro la conexion: {e}") from e

    def cerrar(self):
        if not self.proc:
            return
        try:
            self.proc.stdin.close()            # cierre ordenado: el agente sale solo
            self.proc.wait(timeout=3)
        except Exception:
            pass
        if self.proc.poll() is None:
            agentes._matar_arbol(self.proc)
        for h in self._hilos:
            h.join(timeout=2)           # leen hasta EOF, que llega al morir el proceso
        for f in (self.proc.stdout, self.proc.stderr):
            try:
                f.close()
            except Exception:
                pass

    # ------------------------------------------------------------ JSON-RPC
    def _siguiente(self, hasta: float):
        resta = hasta - time.monotonic()
        if resta <= 0:
            raise _Vencido()
        try:
            msg = self._entrada.get(timeout=resta)
        except queue.Empty:
            raise _Vencido() from None
        if msg is None:
            cola = " | ".join(self._stderr[-3:])
            raise ErrorAcp("el agente termino sin responder" + (f": {cola}" if cola else ""))
        if "_invalido" in msg:
            raise ErrorAcp("salida no ACP en stdout: " + msg["_invalido"], "invalido")
        return msg

    def peticion(self, metodo: str, params: dict, hasta: float | None = None):
        self._id += 1
        mid = self._id
        self._enviar({"jsonrpc": "2.0", "id": mid, "method": metodo, "params": params})
        hasta = hasta or self.limite
        while True:
            msg = self._siguiente(hasta)
            if "method" in msg:
                self._atender(msg)
            elif msg.get("id") == mid:
                if "error" in msg:
                    e = msg["error"] or {}
                    raise ErrorAcp(f"{metodo}: {e.get('message', e)}", e.get("code"))
                return msg.get("result") or {}

    def notificar(self, metodo: str, params: dict):
        self._enviar({"jsonrpc": "2.0", "method": metodo, "params": params})

    def _responder(self, mid, resultado=None, error=None):
        msg = {"jsonrpc": "2.0", "id": mid}
        msg.update({"error": error} if error else {"result": resultado or {}})
        self._enviar(msg)

    def _evento(self, ev: dict):
        if len(self.eventos) < MAX_EVENTOS:
            self.eventos.append(ev)

    # ------------------------------------------------------------ lo que pide el agente
    def _atender(self, msg: dict):
        metodo, params, mid = msg.get("method"), msg.get("params") or {}, msg.get("id")
        if mid is None:                                    # notificacion
            if metodo == "session/update":
                self._actualizacion(params.get("update") or {})
            return
        try:
            if metodo == "session/request_permission":
                self._responder(mid, self._permiso(params))
            elif metodo == "fs/read_text_file":
                self._responder(mid, self._leer_archivo(params))
            elif metodo == "fs/write_text_file":
                self._responder(mid, self._escribir_archivo(params))
            else:
                self._responder(mid, error={"code": -32601, "message": f"no soportado: {metodo}"})
        except PermissionError as e:
            self._responder(mid, error={"code": -32000, "message": str(e)})
        except (OSError, ValueError) as e:
            self._responder(mid, error={"code": -32602, "message": str(e)})

    def _actualizacion(self, u: dict):
        tipo = u.get("sessionUpdate")
        if tipo == "agent_message_chunk":
            c = u.get("content") or {}
            if c.get("type") == "text":
                self.texto.append(c.get("text", ""))
            return                                         # el texto ya va en la respuesta
        if tipo in ("tool_call", "tool_call_update"):
            tid = u.get("toolCallId")
            previo = self.herramientas.get(tid, {})
            self.herramientas[tid] = {**previo, **{k: v for k, v in u.items()
                                                   if k not in ("content", "rawOutput")}}
            h = self.herramientas[tid]
            self._evento({"tipo": tipo, "id": tid, "kind": h.get("kind"),
                          "titulo": h.get("title"), "estado": h.get("status")})
        elif tipo == "plan":
            self._evento({"tipo": "plan", "pasos": [e.get("content") for e in u.get("entries") or []]})
        elif tipo == "usage_update":
            self.uso = {k: u.get(k) for k in ("used", "size", "cost") if k in u}
        elif tipo:
            self._evento({"tipo": tipo})

    def _permiso(self, params: dict) -> dict:
        tc = dict(params.get("toolCall") or {})
        tc = {**self.herramientas.get(tc.get("toolCallId"), {}), **tc}   # kind/locations previos
        outcome, concedido, motivo = decidir_permiso(tc, params.get("options") or [],
                                                     self.escritura, self.cwd)
        self.permisos.append((tc.get("kind"), concedido, motivo))
        self._evento({"tipo": "permiso", "kind": tc.get("kind"), "titulo": tc.get("title"),
                      "concedido": concedido, "motivo": motivo})
        return {"outcome": outcome}

    def _ruta(self, params) -> Path:
        p = params.get("path") or ""
        if not _dentro(p, self.cwd):
            raise PermissionError(f"ruta fuera del workspace: {p}")
        return Path(p)

    def _leer_archivo(self, params) -> dict:
        lineas = self._ruta(params).read_text(encoding="utf-8").splitlines(keepends=True)
        desde = max(int(params.get("line") or 1), 1) - 1
        limite = params.get("limit")
        trozo = lineas[desde:desde + int(limite)] if limite else lineas[desde:]
        return {"content": "".join(trozo)}

    def _escribir_archivo(self, params) -> dict:
        if not self.escritura:
            self._evento({"tipo": "permiso", "kind": "fs/write_text_file", "concedido": False,
                          "motivo": "modo lectura"})
            raise PermissionError("modo lectura: soou no escribe archivos")
        ruta = self._ruta(params)
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_text(params.get("content") or "", encoding="utf-8")
        return {}

    # ------------------------------------------------------------ el turno
    def conectar(self):
        r = self.peticion("initialize", {
            "protocolVersion": VERSION_PROTOCOLO,
            "clientCapabilities": {"fs": {"readTextFile": True, "writeTextFile": True},
                                   "terminal": False},
            "clientInfo": CLIENTE})
        if r.get("protocolVersion") != VERSION_PROTOCOLO:
            raise ErrorAcp(f"version de protocolo no soportada: {r.get('protocolVersion')}",
                           "version")
        self.capacidades = r.get("agentCapabilities") or {}
        s = self.peticion("session/new", {"cwd": self.cwd, "mcpServers": []})
        self.sesion = s.get("sessionId")
        if not self.sesion:
            raise ErrorAcp("session/new no devolvio sessionId")
        self._elegir_modo(s.get("modes") or {})

    def _elegir_modo(self, modos: dict):
        """Pone la sesion en el modo que corresponde al pedido, si el agente
        anuncia modos. En lectura, sin un modo de solo lectura, queda solo la
        decision de permisos de soou y se deja constancia en los eventos."""
        disponibles = [m.get("id") for m in modos.get("availableModes") or [] if m.get("id")]
        if not disponibles:
            return
        actual = modos.get("currentModeId")
        preferidos = MODOS_LECTURA if not self.escritura else MODOS_ESCRITURA
        elegido = next((p for p in preferidos if p in disponibles), None)
        if elegido is None:
            if not self.escritura or any(x in str(actual).lower() for x in MODOS_PROHIBIDOS):
                self._evento({"tipo": "modo", "modo": actual, "aviso":
                              "el agente no anuncia un modo adecuado: solo rigen los permisos"})
            return
        if elegido != actual:
            self.peticion("session/set_mode", {"sessionId": self.sesion, "modeId": elegido})
        self._evento({"tipo": "modo", "modo": elegido, "previo": actual})

    def turno(self, prompt: str) -> str:
        """Envia el prompt y espera el stopReason. Si vence el tiempo, cancela
        (session/cancel) y espera la respuesta un rato; luego se cierra."""
        try:
            r = self.peticion("session/prompt", {
                "sessionId": self.sesion, "prompt": [{"type": "text", "text": prompt}]})
            return r.get("stopReason") or "end_turn"
        except _Vencido:
            self.notificar("session/cancel", {"sessionId": self.sesion})
            hasta = time.monotonic() + GRACIA_CANCELAR_S
            try:
                while True:
                    msg = self._siguiente(hasta)
                    if "method" in msg:
                        if msg.get("method") == "session/request_permission" and "id" in msg:
                            # MUST: los permisos pendientes se contestan "cancelled"
                            self._responder(msg["id"], {"outcome": {"outcome": "cancelled"}})
                        else:
                            self._atender(msg)
                    elif msg.get("id") == self._id:
                        break
            except (_Vencido, ErrorAcp):
                pass
            return "_timeout"


def ejecutar(argv, prompt: str, cwd, escritura: bool, timeout: int = 1200,
             nombre: str = "acp") -> dict:
    """Un turno completo. Devuelve la salida historica de un adaptador
    ({ok, output, estado, codigo_error, eventos, uso}) para `contrato.desde_legado`."""
    cli = ClienteAcp(argv, cwd or os.getcwd(), escritura, timeout)
    try:
        cli.iniciar()
    except Exception as e:
        return agentes.fallo_lanzamiento(nombre, e)
    try:
        cli.conectar()
        motivo = cli.turno(prompt)
    except _Vencido:
        motivo = "_timeout"
    except ErrorAcp as e:
        return {"ok": False, "estado": "fallo", "output": f"{nombre} (acp): {e}",
                "codigo_error": "E_SALIDA_INVALIDA" if e.codigo in ("invalido", "version")
                else "E_PROCESO", "eventos": cli.eventos}
    finally:
        cli.cerrar()
    texto = "".join(cli.texto).strip()
    base = {"output": texto, "eventos": cli.eventos, "uso": cli.uso}
    if motivo == "_timeout":
        return {**base, "ok": False, "estado": "timeout", "codigo_error": "E_TIMEOUT",
                "output": (texto + "\n" if texto else "")
                + f"{nombre}: tiempo agotado ({timeout}s); se cancelo el turno y se detuvo el agente"}
    if motivo == "end_turn":
        return {**base, "ok": True, "estado": "completado"}
    if motivo == "cancelled":
        return {**base, "ok": False, "estado": "cancelado", "codigo_error": "E_PROCESO"}
    if motivo == "refusal":
        return {**base, "ok": False, "estado": "fallo", "codigo_error": "E_NO_PUDO",
                "output": texto or f"{nombre}: el agente rechazo la tarea"}
    return {**base, "ok": False, "estado": "fallo", "codigo_error": "E_PROCESO",
            "output": (texto + "\n" if texto else "") + f"{nombre}: el turno termino por {motivo}"}


class AcpAdapter(agentes.AgentAdapter):
    """Cualquier agente que hable ACP por stdio, p.ej. en soou.config.json:
    {"name": "claude-acp", "type": "acp", "command": ["claude-agent-acp"]}."""
    restriccion_lectura = "cliente_acp"

    def __init__(self, name, command, lectura="cliente_acp"):
        self.name = name
        self.command = command if isinstance(command, list) else str(command).split()
        self.restriccion_lectura = lectura

    def _bin(self):
        return self.command[0]

    def available(self) -> bool:
        return self.resolve() is not None

    def probe(self, timeout: int = 25):
        """Preflight real: handshake `initialize`, sin abrir sesion ni gastar modelo."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            cli = ClienteAcp(self.command, tmp, False, timeout)
            try:
                cli.iniciar()
                r = cli.peticion("initialize", {"protocolVersion": VERSION_PROTOCOLO,
                                                "clientCapabilities": {}, "clientInfo": CLIENTE})
                info = r.get("agentInfo") or {}
                return (r.get("protocolVersion") == VERSION_PROTOCOLO,
                        f"ACP v{r.get('protocolVersion')} {info.get('name', '')} "
                        f"{info.get('version', '')}".strip())
            except (_Vencido, ErrorAcp, OSError, agentes.LanzamientoRechazado) as e:
                return (False, str(e)[:150] or "sin respuesta a initialize")
            finally:
                cli.cerrar()

    def run(self, prompt, allow_write=False, timeout=1200, cwd=None, model=None,
            reasoning=None) -> dict:
        if not allow_write and not agentes.lectura_segura(self):
            return agentes.rechazo(self.name, "su restriccion de lectura no esta verificada")
        # model/reasoning: ACP v1 no los fija al crear la sesion; se usa el del agente.
        return ejecutar(self.command, prompt, cwd, allow_write, timeout, self.name)
