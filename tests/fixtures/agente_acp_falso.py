"""Agente ACP falso (protocolo v1 por stdio) para probar el cliente sin agentes
reales. Lo que hace depende de palabras en el prompt:

    (nada)          pide leer README.md (kind read) y termina con end_turn
    ESCRIBE x.txt   pide editar x.txt (kind edit) y, si se concede, lo escribe
                    con fs/write_text_file; informa cada resultado
    FUERA           pide editar un archivo FUERA del workspace
    EJECUTA         pide una herramienta kind execute
    LENTO           espera session/cancel y responde stopReason "cancelled"
    SORDO           ignora session/cancel y no responde nunca
    RECHAZA         termina con stopReason "refusal"
    BASURA          escribe algo que no es ACP en stdout

    python agente_acp_falso.py [--version N] [--modos | --modos-sin-lectura]

Con --modos imita a codex-acp: anuncia modos y arranca en "auto", donde edita
SIN pedir permiso; en "read-only" pide permiso para editar.
"""

import json
import os
import sys
import time

VERSION = int(sys.argv[sys.argv.index("--version") + 1]) if "--version" in sys.argv else 1
_id = [100]
cwd = os.getcwd()
sesion = "sess_falsa"
MODOS = (["read-only", "workspace-write", "auto", "full-access"] if "--modos" in sys.argv else
         ["auto", "full-access"] if "--modos-sin-lectura" in sys.argv else [])
modo = ["auto" if MODOS else None]


def enviar(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def leer():
    linea = sys.stdin.readline()
    if not linea:
        sys.exit(0)
    return json.loads(linea)


def pedir(metodo, params):
    """Peticion del agente al cliente; devuelve result o error."""
    _id[0] += 1
    mid = _id[0]
    enviar({"jsonrpc": "2.0", "id": mid, "method": metodo, "params": params})
    while True:
        msg = leer()
        if msg.get("id") == mid and "method" not in msg:
            return msg


def actualizar(update):
    enviar({"jsonrpc": "2.0", "method": "session/update",
            "params": {"sessionId": sesion, "update": update}})


def decir(texto):
    actualizar({"sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": texto + "\n"}})


def permiso(tid, titulo, kind, ruta):
    actualizar({"sessionUpdate": "tool_call", "toolCallId": tid, "title": titulo,
                "kind": kind, "status": "pending",
                "locations": [{"path": ruta}] if ruta else []})
    r = pedir("session/request_permission", {
        "sessionId": sesion, "toolCall": {"toolCallId": tid},
        "options": [{"optionId": "si", "name": "Permitir", "kind": "allow_once"},
                    {"optionId": "siempre", "name": "Siempre", "kind": "allow_always"},
                    {"optionId": "no", "name": "Rechazar", "kind": "reject_once"}]})
    outcome = (r.get("result") or {}).get("outcome") or {}
    concedido = outcome.get("outcome") == "selected" and outcome.get("optionId") in ("si", "siempre")
    actualizar({"sessionUpdate": "tool_call_update", "toolCallId": tid,
                "status": "completed" if concedido else "failed"})
    return concedido, outcome


def turno(mid, texto):
    actualizar({"sessionUpdate": "plan", "entries": [
        {"content": "leer", "priority": "high", "status": "pending"}]})
    if "BASURA" in texto:
        sys.stdout.write("esto no es json\n")
        sys.stdout.flush()
        time.sleep(30)
        return
    if "RECHAZA" in texto:
        enviar({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "refusal"}})
        return
    if "soou-conformidad.txt" in texto:
        texto = "ESCRIBE soou-conformidad.txt"
    if "conformidad-timeout" in texto:
        texto = "LENTO"
    if "LENTO" in texto or "SORDO" in texto:
        while True:
            msg = leer()
            if msg.get("method") == "session/cancel" and "LENTO" in texto:
                enviar({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "cancelled"}})
                return
    if "ESCRIBE" in texto and modo[0] in ("auto", "full-access"):
        nombre = texto.split("ESCRIBE", 1)[1].split()[0]
        with open(os.path.join(cwd, nombre), "w", encoding="utf-8") as f:
            f.write("sin pedir permiso")            # modo autoaprobado: no pregunta
        decir("escribi sin pedir permiso (modo " + modo[0] + ")")
    elif "ESCRIBE" in texto:
        nombre = texto.split("ESCRIBE", 1)[1].split()[0]
        ruta = os.path.join(cwd, nombre)
        ok, _ = permiso("t-edit", "Editar " + nombre, "edit", ruta)
        decir("permiso escritura: " + ("concedido" if ok else "denegado"))
        r = pedir("fs/write_text_file", {"sessionId": sesion, "path": ruta, "content": "hola"})
        decir("fs escritura: " + ("ok" if "result" in r else "error " + str(r["error"]["code"])))
    elif "FUERA" in texto:
        ruta = os.path.join(os.path.dirname(cwd), "fuera-del-workspace.txt")
        ok, _ = permiso("t-fuera", "Editar fuera", "edit", ruta)
        decir("permiso fuera: " + ("concedido" if ok else "denegado"))
        r = pedir("fs/write_text_file", {"sessionId": sesion, "path": ruta, "content": "x"})
        decir("fs fuera: " + ("ok" if "result" in r else "error"))
    elif "EJECUTA" in texto:
        ok, _ = permiso("t-exec", "Correr tests", "execute", None)
        decir("permiso ejecutar: " + ("concedido" if ok else "denegado"))
    else:
        ok, outcome = permiso("t-read", "Leer README", "read", os.path.join(cwd, "README.md"))
        decir("permiso lectura: " + ("concedido" if ok else "denegado")
              + " opcion=" + str(outcome.get("optionId")))
        r = pedir("fs/read_text_file", {"sessionId": sesion, "path": os.path.join(cwd, "README.md")})
        decir("contenido: " + (r.get("result") or {}).get("content", "ERROR").strip())
    actualizar({"sessionUpdate": "usage_update", "used": 42, "size": 1000})
    decir("hecho")
    enviar({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn"}})


while True:
    msg = leer()
    metodo, mid = msg.get("method"), msg.get("id")
    if metodo == "initialize":
        enviar({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": VERSION, "agentCapabilities": {"loadSession": False},
            "agentInfo": {"name": "falso", "version": "0.1"}, "authMethods": []}})
    elif metodo == "session/new":
        cwd = msg["params"]["cwd"]
        r = {"sessionId": sesion}
        if MODOS:
            r["modes"] = {"currentModeId": modo[0],
                          "availableModes": [{"id": m, "name": m} for m in MODOS]}
        enviar({"jsonrpc": "2.0", "id": mid, "result": r})
    elif metodo == "session/set_mode":
        if msg["params"].get("modeId") in MODOS:
            modo[0] = msg["params"]["modeId"]
            enviar({"jsonrpc": "2.0", "id": mid, "result": {}})
        else:
            enviar({"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "modo"}})
    elif metodo == "session/prompt":
        texto = " ".join(b.get("text", "") for b in msg["params"]["prompt"])
        turno(mid, texto)
    elif mid is not None:
        enviar({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "no"}})
