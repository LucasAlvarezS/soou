#!/usr/bin/env python3
"""Servidor MCP del cerebro — stdio, JSON-RPC 2.0, SIN dependencias (stdlib).

Expone las operaciones de LECTURA del cerebro como tools MCP para que cualquier
agente compatible (Codex, Claude Code, …) consulte la LLM-Wiki:
  · brain_query   busqueda rankeada (BM25 + TF-IDF, RRF)
  · brain_recall  contexto para una pregunta (semillas + vecinos del grafo)

Reusa el core soou.py sin tocarlo; el core sigue cero-deps. Esta capa tampoco
añade dependencias: implementa el transporte MCP (JSON-RPC line-delimited) a mano.
"""
import os
import sys
import re
import json
import threading
import importlib.util
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# stdout SOLO para JSON-RPC; sin traduccion \r\n (Windows). Logs a stderr.
try:
    sys.stdout.reconfigure(encoding="utf-8", newline="")
    sys.stdin.reconfigure(encoding="utf-8")   # queries con acentos/ñ desde el cliente
except Exception:
    pass

def log(*a):
    print(*a, file=sys.stderr, flush=True)

# importar soou.py como modulo (tiene guard __main__, seguro)
_spec = importlib.util.spec_from_file_location("soou", ROOT / "soou.py")
brain = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(brain)
contexto = brain.contexto   # el paquete de contexto unico (soou_core/contexto.py)

# Version del protocolo que este servidor implementa de verdad. La spec obliga a
# responder con una version soportada: si el cliente pide otra, se le devuelve esta
# (antes se le devolvia la suya, haciendole creer que habia features inexistentes).
PROTOCOL_VERSION = "2024-11-05"
SUPPORTED_PROTOCOL_VERSIONS = (PROTOCOL_VERSION,)
SERVER_INFO = {"name": "soou", "version": getattr(brain, "__version__", "0")}

QUERY_LIMIT_DEFAULT = 8
QUERY_LIMIT_MAX = 50
LINKS_PER_NOTE_MAX = 8
CAPTURE_MAX_CHARS = 20000
MAX_TOOL_WORKERS = 4

TOOLS = [
    {
        "name": "brain_query",
        "description": ("Busca en el cerebro (LLM-Wiki personal del usuario) las notas "
                        "mas relevantes a una consulta, con ranking hibrido BM25 + TF-IDF "
                        "fusionado por RRF. Devuelve nombre, area, score y un fragmento de "
                        "cada nota top, con sus enlaces salientes."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "texto de busqueda"},
                "limit": {"type": "integer",
                          "minimum": 1, "maximum": QUERY_LIMIT_MAX,
                          "description": (f"maximo de resultados. Por defecto "
                                          f"{QUERY_LIMIT_DEFAULT}, tope {QUERY_LIMIT_MAX}: "
                                          f"un valor mayor se recorta al tope.")},
            },
            "required": ["query"],
        },
    },
    {
        "name": "brain_recall",
        "description": ("Recupera contexto del cerebro para una pregunta: si es un lookup de "
                        "nombre/dato devuelve la(s) nota(s) exacta(s); si es tematica, devuelve "
                        "semillas rankeadas mas sus vecinos del grafo. Pensado para inyectar "
                        "conocimiento del usuario antes de responder."),
        "inputSchema": {
            "type": "object",
            "properties": {"prompt": {"type": "string", "description": "la pregunta o tema"}},
            "required": ["prompt"],
        },
    },
    {
        "name": "brain_capture",
        "description": ("El agente APORTA conocimiento al cerebro (una decision, hecho o "
                        "resumen). Encola el texto como sesion L0 para que el destilador lo "
                        "integre (aditivo, curado); NO sobrescribe notas existentes."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "el conocimiento a aportar"},
                "title": {"type": "string", "description": "titulo opcional"},
                "area": {"type": "string", "enum": ["code", "personal", "work"],
                         "description": ("area opcional; si se indica, el destilador "
                                         "la respeta en vez de inferirla")},
            },
            "required": ["text"],
        },
    },
    {
        "name": "brain_orchestrate",
        "description": ("Dada una tarea, devuelve la DECISION de orquestacion (advisory): "
                        "nivel de complejidad, flujo recomendado, senales y el contexto "
                        "relevante del cerebro. El agente la usa para decidir si resolver "
                        "directo o descomponer/fan-out. Auto-gateada por el router. "
                        "SOLO LECTURA salvo que pases execute=true, que SI tiene efectos: "
                        "lanza agentes reales, escribe en una rama/worktree de git y "
                        "consume presupuesto diario."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "la tarea a orquestar"},
                "execute": {
                    "type": "boolean",
                    "description": ("si es true DISPARA la orquestacion (fan-out) en vez "
                                    "de solo recomendar. Efectos: lanza agentes, crea una "
                                    "rama soou/run-* con cambios y gasta presupuesto. "
                                    "Dejalo en false para una recomendacion sin efectos."),
                },
            },
            "required": ["task"],
        },
    },
    {
        "name": "brain_note_get",
        "description": ("Devuelve una nota COMPLETA del cerebro por su nombre exacto: "
                        "frontmatter, cuerpo entero y enlaces. brain_query y brain_recall "
                        "solo devuelven fragmentos; usa esta cuando necesites el "
                        "contenido real de una nota."),
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string",
                                    "description": "nombre exacto de la nota"}},
            "required": ["name"],
        },
    },
    {
        "name": "brain_note_write",
        "description": ("ESCRIBE en el cerebro: crea una nota o reemplaza el cuerpo de "
                        "una existente. Usala para CORREGIR conocimiento mal "
                        "sintetizado; para aportar material nuevo sin curar preferi "
                        "brain_capture, que es aditivo. Nunca borra, y se NIEGA a "
                        "tocar una nota pinned (correccion humana o regla del "
                        "sistema): ante un conflicto crea una nota aparte y "
                        "enlazalas."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "nombre en kebab-case"},
                "body": {"type": "string",
                         "description": "cuerpo en Markdown; enlaza con [[otra-nota]]"},
                "area": {"type": "string", "enum": ["code", "personal", "work"],
                         "description": "area de la nota (def personal)"},
                "type": {"type": "string",
                         "enum": ["concept", "decision", "person", "source",
                                  "reference", "event"],
                         "description": "tipo de nota (def concept)"},
                "pinned": {"type": "boolean",
                           "description": ("marca la nota como correccion humana: "
                                           "nadie la sobreescribe despues")},
            },
            "required": ["name", "body"],
        },
    },
    {
        "name": "brain_health",
        "description": ("Estado del cerebro: enlaces rotos, notas huerfanas, placeholders "
                        "sin sintetizar, notas obsoletas, duplicados y contradicciones "
                        "registradas. Sirve para decidir que arreglar antes de seguir "
                        "agregando conocimiento."),
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _links_resumidos(note):
    """Enlaces de la nota: deduplicados y acotados. El cuerpo ya se recortaba a
    240 caracteres, pero esta lista no tenia tope: una nota-hub con cientos de
    enlaces podia inflar la respuesta sin limite. Se avisa cuantos quedaron
    fuera para no recortar en silencio."""
    vistos, unicos = set(), []
    for l in brain.note_links(note):   # links_in ya deduplica, pero soou.db puede
                                       # traer _links viejos con repetidos hasta reindexar
        if l not in vistos:
            vistos.add(l)
            unicos.append(l)
    if not unicos:
        return ""
    visibles = unicos[:LINKS_PER_NOTE_MAX]
    resto = len(unicos) - len(visibles)
    return ", ".join(visibles) + (f"  (+{resto} enlace(s) mas)" if resto else "")


def _fmt(note, score=None):
    body = " ".join(note["body"].split())[:240]
    ls = _links_resumidos(note)
    head = f"[{note['area']}] {note['name']}"
    if score is not None:
        head += f"  (rrf {score:.4f})"
    tail = f"\n    -> {ls}" if ls else ""
    return f"{head}\n    {body}{tail}"


def _clamp_limit(raw):
    """limit acotado: sin techo, un valor grande devolvia el cerebro entero."""
    if raw is None or raw == "":
        return QUERY_LIMIT_DEFAULT
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return QUERY_LIMIT_DEFAULT
    return max(1, min(n, QUERY_LIMIT_MAX))


def tool_query(args):
    notes = brain.indice.para_agentes(brain.load_notes())
    q = (args.get("query") or "").strip()
    if not q:
        return "Consulta vacia."
    fused, backend = brain.rank_notes(q, notes)
    if not fused:
        return "Sin coincidencias."
    limit = _clamp_limit(args.get("limit"))
    out = [f"(BM25 + {backend} - RRF - {len(fused)} coincidencias)"]
    for score, n in fused[:limit]:
        out.append(_fmt(n, score))
    return "\n\n".join(out)


def _recall_impl(prompt, notes):
    """El mismo paquete que el hook y run (soou_core/contexto.py), en el formato
    del MCP. Sin resumen del proyecto: es un recall para este pedido."""
    if len(prompt) < 3:
        return "Prompt demasiado corto."
    paq = contexto.armar_paquete(prompt, notes, cwd=os.getcwd(), recorte=240,
                                 presupuesto=4000, resumen_proyecto=False)
    if not paq.piezas:
        return "Sin coincidencias."
    modo = "nota exacta" if paq.modo == "nota exacta" else "semillas + vecinos del grafo"
    out = [f"(recall - {modo} - BM25 + {paq.backend}/RRF)"]
    for n in paq.notas:
        out.append(_fmt(n))
    return "\n\n".join(out)


def tool_capture(args):
    text = (args.get("text") or "").strip()
    if not text:
        return "Texto vacio."
    title = (args.get("title") or "").strip()
    area = (args.get("area") or "").strip().lower()
    if area and area not in brain.AREAS:
        return ("area invalida: " + area + ". Validas: " + ", ".join(brain.AREAS) + ".")
    cuerpo_txt, recortado = text, False
    if len(cuerpo_txt) > CAPTURE_MAX_CHARS:
        cuerpo_txt, recortado = cuerpo_txt[:CAPTURE_MAX_CHARS], True
    ahora = brain.dt.datetime.now()
    fecha = ahora.strftime("%Y-%m-%d %H:%M")
    carpeta = brain.SESSIONS / ahora.strftime("%Y/%m/%d")
    carpeta.mkdir(parents=True, exist_ok=True)
    base = "session-mcp-" + ahora.strftime("%Y%m%d-%H%M%S")
    dest = carpeta / (base + ".md")
    n = 1
    while dest.exists():          # dos capturas en el mismo segundo no se pisan
        dest = carpeta / (base + "-" + str(n) + ".md")
        n += 1
    # _parse_session_raw lee UNA linea por marca **assistant:**; una marca por
    # linea evita que se pierda todo el texto despues del primer salto.
    lineas = [l.strip() for l in cuerpo_txt.splitlines() if l.strip()]
    cabecera = ["<!-- distilled: no -->",
                "# Captura MCP " + (title or fecha),
                "Fecha: " + fecha,
                "Fuente: mcp brain_capture"]
    if area:
        cabecera.append("Area: " + area)
    contenido = ("\n".join(cabecera) + "\n\n"
                 "**user:** " + (title or "captura") + "\n\n"
                 + "\n\n".join("**assistant:** " + l for l in lineas) + "\n")
    dest.write_text(contenido, encoding="utf-8")
    rel = dest.relative_to(brain.ROOT)
    msg = ["Capturado en la cola L0: " + str(rel) + "."]
    if area:
        msg.append("Area declarada: " + area + ".")
    if recortado:
        msg.append("AVISO: el texto superaba " + str(CAPTURE_MAX_CHARS) +
                   " caracteres y se guardo recortado; volve a capturar el resto.")
    msg.append("Se integrara en la proxima destilacion (soou sync).")
    return " ".join(msg)


def tool_orchestrate(args):
    task = (args.get("task") or "").strip()
    if not task:
        return "Tarea vacia."
    notes = brain.load_notes()
    fused, _ = brain.rank_notes(task, notes)
    # la clasificacion usa todo el cerebro (es local); el contexto que se devuelve
    # al agente es el paquete unico, sin privadas ni reemplazadas.
    ctx = contexto.como_prompt(contexto.armar_paquete(
        task, notes, cwd=os.getcwd(), presupuesto=contexto.presupuesto()))
    tier, reason, strat = brain.classify_task(task, notes, fused)
    level, flow = brain.flow_for(tier, reason)
    if args.get("execute"):
        if level in ("trivial", "contextual"):
            return ("Tarea simple: resolvela directo con este contexto:\n\n" + ctx)
        if level in ("complex", "ambiguous/high-risk"):
            if not brain._budget_allow():
                return ("[budget] sin presupuesto hoy (" + brain._budget_left_str() +
                        "); no se dispara.")
            if not brain._launch_plan_detached(task):
                return "No se pudo lanzar la orquestacion en segundo plano."
            return ("Orquestacion disparada en segundo plano (fan-out multiagente en un "
                    "git worktree aparte). El trabajo quedara en una rama soou/run-* para "
                    "revisar/mergear; sin auto-merge. Cada intento queda en 'soou trace'.")
    rec = ("resolve directo con el contexto" if level in ("trivial", "contextual")
           else "descompone y usa el harness (fan-out multi-agente)")
    return "\n".join([
        "Nivel: " + level,
        "Flujo: " + flow,
        "Senales: " + reason,
        "Recomendacion: " + rec,
        "",
        "Contexto del cerebro:",
        ctx,
    ])


NOTE_MAX_CHARS = 12000


def tool_note_get(args):
    nombre = (args.get("name") or "").strip()
    if not nombre:
        return "Falta el nombre de la nota."
    nota = brain.buscar_nota(nombre)
    if nota is not None and brain.indice.es_privada(nota):
        nota = None                       # una privada no existe para un agente
    if nota is None:
        parecidas = [n["name"] for n in brain.indice.para_agentes(brain.load_notes())
                     if nombre.lower() in n["name"].lower()][:5]
        extra = ("  Parecidas: " + ", ".join(parecidas)) if parecidas else ""
        return "No existe la nota '" + nombre + "'." + extra
    cuerpo, aviso = nota["body"], ""
    if len(cuerpo) > NOTE_MAX_CHARS:
        cuerpo = cuerpo[:NOTE_MAX_CHARS]
        aviso = "\n\n[AVISO: cuerpo recortado a " + str(NOTE_MAX_CHARS) + " caracteres]"
    meta = nota["meta"]
    cabecera = ["[" + nota["area"] + "] " + nota["name"],
                "tipo: " + str(meta.get("type", "?"))
                + "  ·  pinned: " + str(meta.get("pinned", "false"))
                + "  ·  actualizada: " + str(meta.get("updated", "?"))]
    enlaces = _links_resumidos(nota)
    if enlaces:
        cabecera.append("enlaces: " + enlaces)
    return "\n".join(cabecera) + "\n\n" + cuerpo + aviso


def tool_note_write(args):
    ok, mensaje = brain.guardar_nota(
        args.get("name"),
        args.get("body"),
        area=(args.get("area") or "personal"),
        tipo=(args.get("type") or "concept"),
        pinned=args.get("pinned"),
        # lo escribio un agente: queda registrado asi, no como hecho comprobado
        procedencia={"recorded_at": brain.vigencia.ahora(), "origin": "mcp",
                     "verification": "afirmado_por_agente"},
    )
    return mensaje


def tool_health(args):
    s = brain.resumen_salud()
    lineas = ["Cerebro: " + str(s["total"]) + " nota(s)."]

    def seccion(titulo, items, fmt=str):
        lineas.append("")
        lineas.append(titulo + ": " + str(len(items)))
        for it in items[:10]:
            lineas.append("  - " + fmt(it))
        if len(items) > 10:
            lineas.append("  ... y " + str(len(items) - 10) + " mas")

    seccion("Enlaces rotos", s["broken"], lambda x: x["from"] + " -> [[" + x["to"] + "]]")
    seccion("Huerfanas (nadie las enlaza)", s["orphans"])
    seccion("Placeholders sin sintetizar", s["placeholders"])
    seccion("Obsoletas (la fuente cambio)", s["stale"])
    seccion("Posibles duplicados", s["duplicates"], lambda x: x[0] + " ~ " + x[1])
    seccion("Contradicciones registradas", s["contradictions"])
    seccion("Vigencia (reemplazos rotos o vigentes en conflicto)", s["vigencia"],
            lambda x: x["nota"] + ": " + x["problema"])
    pendientes = (len(s["broken"]) + len(s["placeholders"]) + len(s["stale"])
                  + len(s["vigencia"]))
    lineas.append("")
    lineas.append("Salud: " + ("OK" if pendientes == 0
                               else str(pendientes) + " pendiente(s)"))
    return "\n".join(lineas)


_send_lock = threading.Lock()
_POOL = ThreadPoolExecutor(max_workers=MAX_TOOL_WORKERS, thread_name_prefix="soou-tool")


def send(msg):
    # una sola escritura atomica: con tools concurrentes, dos writes sin lock
    # podian intercalarse y romper el framing line-delimited.
    data = json.dumps(msg, ensure_ascii=False) + "\n"
    with _send_lock:
        sys.stdout.write(data)
        sys.stdout.flush()


_TOOL_FUNCS = {
    "brain_query": tool_query,
    "brain_recall": lambda a: _recall_impl((a.get("prompt") or "").strip(),
                                           brain.indice.para_agentes(brain.load_notes())),
    "brain_capture": tool_capture,
    "brain_orchestrate": tool_orchestrate,
    "brain_note_get": tool_note_get,
    "brain_note_write": tool_note_write,
    "brain_health": tool_health,
}


def _run_tool(rid, params):
    """Corre una tool fuera del hilo del lector: una llamada lenta ya no bloquea
    el resto de la sesion (antes ni ping respondia mientras se orquestaba)."""
    name = params.get("name")
    args = params.get("arguments") or {}
    fn = _TOOL_FUNCS.get(name)
    if fn is None:
        send({"jsonrpc": "2.0", "id": rid,
              "error": {"code": -32602, "message": f"tool desconocida: {name}"}})
        return
    try:
        text = fn(args)
    except Exception as e:
        log("error en tool", name, e)
        send({"jsonrpc": "2.0", "id": rid,
              "result": {"content": [{"type": "text", "text": f"error: {e}"}],
                         "isError": True}})
        return
    send({"jsonrpc": "2.0", "id": rid,
          "result": {"content": [{"type": "text", "text": text}]}})


def handle(req):
    method = req.get("method")
    rid = req.get("id")
    params = req.get("params") or {}
    if method == "initialize":
        pedida = params.get("protocolVersion")
        pv = pedida if pedida in SUPPORTED_PROTOCOL_VERSIONS else PROTOCOL_VERSION
        send({"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": pv,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO}})
    elif method in ("notifications/initialized", "initialized"):
        pass  # notificacion, sin respuesta
    elif method == "ping":
        send({"jsonrpc": "2.0", "id": rid, "result": {}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        _POOL.submit(_run_tool, rid, params)
    elif rid is not None:
        send({"jsonrpc": "2.0", "id": rid,
              "error": {"code": -32601, "message": f"metodo no soportado: {method}"}})


def main():
    log("[brain-mcp] servidor listo (stdio)")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        try:
            handle(req)
        except Exception as e:
            log("[brain-mcp] error:", e)
    _POOL.shutdown(wait=True)   # stdin cerrado: deja terminar lo que este en vuelo


if __name__ == "__main__":
    main()
