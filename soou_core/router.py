"""Modulo router de soou (extraido de soou.py).

soou.py lo re-exporta; dentro de soou_core se usa como `router.nombre`."""

import datetime as dt
import json
import re
from soou_core import config, indice, rutas


# marcadores de pregunta TEMATICA (favorecen expansion por grafo)
THEMATIC_MARKERS = {
    "como", "cómo", "porque", "porqué", "relacion", "relación", "relacionan", "entre",
    "comparar", "compara", "comparacion", "diferencia", "diferencias", "flujo", "conectan",
    "conecta", "conexion", "conexión", "arquitectura", "general", "panorama", "todos", "todas",
    "cuales", "cuáles", "funciona", "integra", "encaja", "vinculo", "vínculo", "mapa", "afecta",
}


def looks_like_entity(prompt: str, terms: list) -> bool:
    """Entity gate (tonydzi): True = lookup de nombre/dato -> NO expandir grafo.
    Conservador: solo bloquea la expansion en casos claros."""
    low = prompt.lower()
    if any(f" {m} " in f" {low} " for m in THEMATIC_MARKERS):
        return False
    if re.search(r"[a-z0-9]+_[a-z0-9]+", low):        # identificador de codigo (snake_case)
        return True
    if re.search(r"qu[eé] es |definici[oó]n de |qu[eé] significa |cual es el ", low):
        return True
    if len(terms) <= 3:                                # consulta corta y concreta
        return True
    return False


# marcadores de tarea ambigua/amplia (empujan a mayor complejidad)
AMBIGUITY_MARKERS = {
    "diseñ", "disen", "refactor", "reescrib", "arquitectura", "planifica", "estrategia",
    "todo el", "todos los", "end-to-end", "de punta", "orquest", "unificar", "en general"}


# sinonimos por vertical para el "piso de amplitud por mencion" (#1)
# Los VERTICALES (dominios del usuario) ya NO se hardcodean: vienen de soou.config.json
# ("verticals": {nombre: [sinónimos]}). Genérico por defecto (vacío). Ver load_config().
BUILD_VERBS = ("constru", "crea", "creá", "implementa", "agrega", "genera", "arma", "monta")


EDIT_VERBS = ("edita", "modifica", "refactor", "arregla", "corrig", "cambia", "renombra",
              "mueve", "migra", "actualiza", "reescrib", "portar", "porta ")


AUDIT_VERBS = ("audita", "auditar", "evalua", "evalúa", "analiza", "diagnostic", "optimiza")


# tareas sobre el propio cerebro/notas/docs: bajo riesgo y enfocadas → nunca fan-out
META_MARKERS = ("nota ", "notas", "cerebro", " wiki", "memoria", " brain", "readme", "grafo",
                "documenta", " doc ", " docs")


# señales de ALTO RIESGO → fuerzan doble-check antes de actuar (independiente del tier)
RISK_MARKERS = ("borra", "elimin", "drop ", " rm ", "reset", "migrac", "migra ", "deploy",
                "despliega", "producci", " prod ", "sobrescrib", " pisa ", "force", "truncate",
                "wipe", "--force", "destruc")


# "todos los verticales" y variantes = alcance multi-vertical EXPLÍCITO (todos)
ALLVERT_MARKERS = ("todos los vertical", "todas las vertical", "cada vertical",
                   "all vertical", "los verticales", "cada uno de los vertical", "6 vertical")


def task_vertical(note: dict) -> str | None:
    for l in indice.note_links(note):
        if l.startswith("vert-"):
            return l[5:]
    v = classify_vertical(note["name"].lower())
    return None if v == "otros" else v


def task_kind(low: str) -> str:
    if any(v in low for v in BUILD_VERBS):
        return "build"
    if any(v in low for v in EDIT_VERBS):
        return "edit"
    if any(v in low for v in AUDIT_VERBS):
        return "audit"
    return "question"


def named_verticals(low: str) -> set:
    return {v for v, syns in config.load_config()["verticals"].items()
            if any(s.strip().lower() in low for s in syns)}


def classify_task(prompt: str, notes: list, fused: list) -> tuple:
    """Router v2 (advisory): piso de amplitud por mención (#1), umbral relativo de
    reach (#2), eje tipo-de-tarea (#3), fan-out podado (#5)."""
    low = " " + prompt.lower() + " "
    terms = re.findall(r"[a-záéíóúñ0-9\-]{4,}", low)
    entity = looks_like_entity(prompt, terms)
    kind = task_kind(low)
    ambiguous = any(m in low for m in AMBIGUITY_MARKERS)

    # #2 reach por umbral relativo (no top-N crudo)
    top = fused[0][0] if fused else 0.0
    thresh = max(top * 0.5, 1e-9)
    relevant = [n for s, n in fused if s >= thresh][:10]
    reach = len(relevant)

    # nombrar verticales explícitamente es el alcance AUTORITATIVO; "todos los verticales"
    # cuenta como todos. Sin mención → solo retrieval (ruidoso, sin 'core', no confirma alcance).
    say_verts = named_verticals(low)
    if any(m in low for m in ALLVERT_MARKERS):
        say_verts = set(config.load_config()["verticals"])
    ret_verts = {v for v in (task_vertical(n) for n in relevant) if v and v != "core"}
    nverts = len(say_verts)
    breadth = nverts if say_verts else len(ret_verts)
    meta = any(m in low for m in META_MARKERS)
    risk = any(m in low for m in RISK_MARKERS)

    fan = (sorted(say_verts) if say_verts else sorted(ret_verts))[:4]   # #5 poda, tope 4
    flist = ", ".join(fan) if fan else "—"

    if meta and kind != "question":
        tier, strat = "T1", "Edición del cerebro/notas/docs: directo, inline. Sin agentes."
    elif (entity or reach <= 1) and kind == "question":
        tier, strat = "T0", "Responder inline (lookup directo). Sin agentes."
    elif (entity or reach <= 1) and nverts == 0:
        # Un verbo de accion (refactor, crea, audita...) es TRABAJO, no un lookup,
        # aunque la frase sea corta: antes "refactor del router con tests" caia en T0.
        if ambiguous:
            tier, strat = "T2", (f"Plan antes de actuar ({kind} de alcance amplio/ambiguo): "
                                 "descomponer y validar cada paso.")
        else:
            tier, strat = "T1", f"Directo: {kind} acotado, un agente."
    elif nverts >= 2:                                   # multi-vertical EXPLÍCITO
        if kind in ("edit", "build", "audit"):
            tier, strat = "T3", (f"Plan → workflow con verificación ({kind} multi-vertical). "
                                 f"Agentes: {flist} (cada uno con su subgrafo).")
        else:
            tier, strat = "T2", f"Fan-out de lectura ({flist}); luego sintetizar."
    elif nverts == 1:
        tier, strat = "T1", "Directo: un vertical, alcance acotado."
    else:                                               # sin vertical nombrado → conservador
        if kind == "question" and breadth >= 2 and ambiguous:
            tier, strat = "T2", f"Fan-out de lectura ({flist}); luego sintetizar."
        else:
            tier, strat = "T1", "Directo; alcance multi-vertical no confirmado (nómbralos si aplica)."

    if risk:
        strat += "  ⚠️ ALTO RIESGO → doble-check: confirmar alcance y plan ANTES de actuar."

    reason = (f"tipo={kind} · meta={str(meta).lower()} · riesgo={str(risk).lower()} · "
              f"entity={str(entity).lower()} · nverts={nverts} · reach={reach} · "
              f"amplitud={breadth} · ambigua={str(ambiguous).lower()}")
    return tier, reason, strat


def log_classification(prompt: str, tier: str, reason: str):
    """Registra cada decisión del router para calibrar umbrales con uso real (#8)."""
    try:
        line = json.dumps({"ts": dt.datetime.now().isoformat(timespec="seconds"),
                           "tier": tier, "reason": reason, "prompt": prompt[:160]},
                          ensure_ascii=False)
        with open(rutas.ROOT / "router-log.jsonl", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def evaluar_retrieval(consultas: list, notes: list, k: int = 5) -> dict:
    """Metricas por consulta y agregadas (global y por 'tipo')."""
    filas = []
    for c in consultas:
        relevantes = set(c.get("relevantes") or [])
        if not c.get("q") or not relevantes:
            continue
        fused, _ = indice.rank_notes(c["q"], notes)
        nombres = [n["name"] for _, n in fused[:max(k, 10)]]
        rango = next((i + 1 for i, nm in enumerate(nombres) if nm in relevantes), None)
        filas.append({"q": c["q"], "tipo": c.get("tipo", "manual"),
                      "relevantes": sorted(relevantes), "top": nombres[:k], "rango": rango,
                      "hit1": rango == 1,
                      "recall": len(relevantes & set(nombres[:k])) / len(relevantes),
                      "rr": 1 / rango if rango else 0.0})

    def agregado(fs):
        n = len(fs) or 1
        return {"n": len(fs), "hit@1": sum(f["hit1"] for f in fs) / n,
                f"recall@{k}": sum(f["recall"] for f in fs) / n,
                "mrr@10": sum(f["rr"] for f in fs) / n}
    por_tipo = {}
    for f in filas:
        por_tipo.setdefault(f["tipo"], []).append(f)
    return {"k": k, "global": agregado(filas),
            "por_tipo": {t: agregado(fs) for t, fs in sorted(por_tipo.items())},
            "filas": filas}


def generar_consultas(notes: list, n: int = 40, semilla: int = 7) -> list:
    """Consultas 'known-item' desde TUS notas: una frase del cuerpo (no el titulo)
    con el nombre de la nota quitado. Es un punto de partida automatico; las
    parafrasis reales (como preguntarias vos) se agregan a mano al archivo."""
    import random
    candidatas = [x for x in notes if x["meta"].get("type") in indice.CONCEPT_TYPES
                  and len(x.get("body", "")) > 200]
    random.Random(semilla).shuffle(candidatas)
    out = []
    for nota in candidatas[:n]:
        cuerpo = re.sub(r"\[\[([^\]|]+)(\|[^\]]*)?\]\]", r"\1", nota["body"])
        frases = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n", cuerpo)
                  if 40 <= len(s.strip()) <= 220 and not s.strip().startswith(("#", "|", "-", "*", ">"))]
        if not frases:
            continue
        frase = frases[min(1, len(frases) - 1)]
        for w in nota["name"].split("-"):
            if len(w) > 3:
                frase = re.sub(re.escape(w), "", frase, flags=re.I)
        q = " ".join(frase.split()[:14])
        if len(q) >= 20:
            out.append({"q": q, "relevantes": [nota["name"]], "tipo": "auto"})
    return out


def flow_for(tier, reason):
    """Traduce el tier del router al modelo de orquestación de 4 niveles (Planner
    de [[soou-harness]]). La señal de riesgo sube al nivel 4."""
    if "riesgo=true" in reason:
        return "ambiguous/high-risk", "Brain → plan → multi-agent  (fan-out + verificación · alto riesgo)"
    return {
        "T0": ("trivial", "single agent  (directo, sin cerebro)"),
        "T1": ("contextual", "Brain → single agent  (contexto del cerebro + 1 agente)"),
        "T2": ("complex", "Brain → plan → agent  (contexto + plan · 1 agente ejecuta)"),
        "T3": ("ambiguous/high-risk", "Brain → plan → multi-agent  (contexto + plan · varios agentes)"),
    }.get(tier, ("contextual", "Brain → single agent"))


AREA_HINTS = [
    (("boleta", "financ", "rendic", "factura", "pdf", "xml"), "work"),
    (("nota", "checklist"), "personal"),
]


SKIP_DIRS = {"brain", "soou", "node_modules", ".git", "__pycache__", "dist", "build", ".next",
             ".venv", "venv", "env", "site-packages", ".claude", "vendor", "target",
             ".pytest_cache", "coverage", ".cache", "dist-info", "bin", "obj"}


def classify_vertical(name: str) -> str:
    """Vertical de un slug/nombre según los dominios del usuario (soou.config.json).
    Genérico por defecto: sin verticales configurados, todo cae a 'otros' (o 'core' si
    coincide un core_marker)."""
    low = name.lower()
    cfg = config.load_config()
    for v, syns in cfg["verticals"].items():
        if any(s.strip().lower() in low for s in syns):
            return v
    if any(m in low for m in cfg["core_markers"]):
        return "core"
    return "otros"


def classify_area(name: str) -> str:
    low = name.lower()
    for keys, a in AREA_HINTS:
        if any(k in low for k in keys):
            return a
    return "code"
