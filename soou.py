#!/usr/bin/env python3
"""
soou.py — CLI de tu "LLM Wiki" personal (patron de Andrej Karpathy).

Sin dependencias externas: solo la libreria estandar de Python 3.
Operaciones:
  ingest   registra una fuente cruda (inmutable) y crea un placeholder en la wiki
  query    busca en la wiki por texto y aristas (full-text + enlaces)
  lint     chequea salud: enlaces rotos, notas huerfanas, placeholders, duplicados
  graph    genera un grafo interactivo (HTML autonomo) con el nivel de ingesta
  stats    resumen rapido en la terminal

Uso tipico:
  python soou.py ingest ruta/al/archivo.pdf --area work
  python soou.py lint
  python soou.py graph --open
"""

import argparse
import concurrent.futures
import datetime as dt
import difflib
import html
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

try:  # Windows: asegurar salida UTF-8 (emojis, acentos)
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

__version__ = "0.9.0"

# CODIGO vs DATOS. En un checkout del repo los dos viven en la misma carpeta (como
# siempre). Instalado con pip/pipx el codigo queda en site-packages, que no es lugar
# para tu cerebro: los datos van a SOOU_HOME o, por defecto, a ~/.soou.
CODE_DIR = Path(__file__).resolve().parent
# soou_core/ vive junto a soou.py; se importa igual si soou.py se carga desde otra
# carpeta (tests, servidor MCP, tarea programada) o instalado en site-packages.
if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))

from soou_core import rutas  # noqa: E402  (rutas de datos: UNICO lugar)
from soou_core.rutas import _data_root  # noqa: E402,F401  (re-export)
from soou_core import ui  # noqa: E402
from soou_core.ui import (  # noqa: E402,F401  (re-export)
    BrainUIApi,
    _crear_servidor_ui,
    _note_badge,
    _servir_ui,
    cmd_ui,
)
from soou_core import instalacion  # noqa: E402
from soou_core.instalacion import (  # noqa: E402,F401  (re-export)
    _DERIVADOS,
    _bootstrap_workspace,
    _borrar_derivados,
    _chequear_instalacion,
    _desinstalar_hooks,
    _desinstalar_mcp,
    _desinstalar_shortcut,
    _desinstalar_trigger,
    _install_claude_hooks,
    _install_mcp,
    _install_shortcut,
    _install_sync_trigger,
    _purgar_hooks_soou,
    _quitar_seccion_toml,
    _registrar_mcp_en_claude,
    _rutas_de_comando,
    _sync_exe,
    _tarea_sync_instalada,
    cmd_init,
    cmd_status,
    cmd_uninstall,
)
from soou_core import harness  # noqa: E402
from soou_core import contexto  # noqa: E402
from soou_core import vigencia  # noqa: E402,F401
from soou_core import conformidad  # noqa: E402
from soou_core import evaluacion  # noqa: E402
from soou_core import contrato, ejecutor  # noqa: E402,F401
from soou_core.ejecutor import (  # noqa: E402,F401  (re-export)
    TRAZA_MIN_MUESTRAS,
    TRAZA_VIDA_MEDIA_DIAS,
    _NO_PUDO,
    _estado_git,
    _historial_agentes,
    _ordenar_por_historial,
    _resultado_util,
    _traza,
    cmd_distill,
    cmd_persona,
    cmd_sync,
)
from soou_core.harness import (  # noqa: E402,F401  (re-export)
    VALIDATOR_TIMEOUT,
    _brain_context,
    _capture_run,
    _checkpoint,
    _detectar_validadores,
    _entorno_validador,
    _git_root,
    _launch_plan_detached,
    _make_worktree,
    _parse_json_list,
    _plan_task,
    _run_plan_execute,
    _run_validators,
    _write_plan_state,
    cmd_run,
)
from soou_core import compactar  # noqa: E402
from soou_core.compactar import (  # noqa: E402,F401  (re-export)
    COMPACT_RAW_DIAS,
    DISTILL_LOG_DIAS,
    LOG_MAX_LINEAS,
    _buscar_nota_de_crudo,
    _desenlazar_archivadas,
    _es_pinned,
    _estado_crudo,
    _mover_al_archivo,
    _nota_de_crudo,
    _nota_legacy_de_crudo,
    _notas_en_archivo,
    _reapuntar_enlaces,
    _rotar_logs,
    _stub,
    cmd_compact,
    plan_de_compactacion,
)
from soou_core import destilado  # noqa: E402
from soou_core.destilado import (  # noqa: E402,F401  (re-export)
    _auto_links,
    _distill_run,
    _escribir_heartbeat,
    _launch_distill_detached,
    _write_last_sync,
    build_distill_prompt,
    cmd_session_end,
    distill_lite_one,
    run_lite,
)
from soou_core import sesiones  # noqa: E402
from soou_core.sesiones import (  # noqa: E402,F401  (re-export)
    _MSG_L0,
    _SESSION_PREFIXES,
    _SID_MAX,
    _acotar_cuerpo,
    _bajo,
    _captura_permitida,
    _es_subsesion_de_soou,
    _limpiar_sesiones,
    _mark_raw,
    _matches_session_source,
    _msg_role,
    _msg_text,
    _parse_session_raw,
    _pull_all_sessions,
    _pull_claude_sessions,
    _pull_codex_sessions,
    _reset_marks,
    _runs_dir,
    _sanear_crudo,
    _session_sources,
    _sid_of_session,
    _source_of_session,
    cmd_capture,
    cmd_capture_codex,
    cmd_distill_mark,
    cmd_sessions,
    extract_claude_transcript,
    extract_codex_rollout,
    lite_pending,
    pending_sessions,
    read_hook_stdin,
)
from soou_core import registro  # noqa: E402
from soou_core.registro import (  # noqa: E402,F401  (re-export)
    HttpAdapter,
    _ADAPTER_TYPES,
    _agent_status,
    build_adapters,
    cmd_agents,
)
from soou_core import consola  # noqa: E402
from soou_core.consola import (  # noqa: E402,F401  (re-export)
    Girador,
    _MARCA_ASCII,
    _MARCA_BLOQUES,
    _soporta_bloques,
    mostrar_marca,
)
from soou_core import router  # noqa: E402
from soou_core.router import (  # noqa: E402,F401  (re-export)
    ALLVERT_MARKERS,
    AMBIGUITY_MARKERS,
    AREA_HINTS,
    AUDIT_VERBS,
    BUILD_VERBS,
    EDIT_VERBS,
    META_MARKERS,
    RISK_MARKERS,
    SKIP_DIRS,
    THEMATIC_MARKERS,
    classify_area,
    classify_task,
    classify_vertical,
    evaluar_retrieval,
    flow_for,
    generar_consultas,
    log_classification,
    looks_like_entity,
    named_verticals,
    task_kind,
    task_vertical,
)
from soou_core import indice  # noqa: E402
from soou_core.indice import (  # noqa: E402,F401  (re-export)
    ARCHIVOS_DE_REGLAS,
    AREAS,
    CODE_SPAN_RE,
    CONCEPT_TYPES,
    LINK_RE,
    PLACEHOLDER_TAG,
    TIPOS_DE_NOTA,
    _db,
    _embed_texts,
    _env,
    _index_entry,
    _notas_de_reglas,
    build_adjacency,
    build_corpus,
    build_index,
    buscar_nota,
    cmd_reindex,
    compute_graph,
    embed_backend,
    guardar_nota,
    is_stale,
    links_in,
    load_notes,
    note_links,
    note_placeholder,
    parse_note,
    rank_notes,
    resumen_salud,
    slugify,
    source_files,
    today,
    vector_scores,
)
from soou_core import config  # noqa: E402
from soou_core.config import (  # noqa: E402,F401  (re-export)
    _CFG_CACHE,
    _DEFAULT_AGENTS,
    _DEFAULT_PROFILES,
    _DEFAULT_ROUTING,
    _DEFAULT_VERTICALS,
    _budget_allow,
    _budget_consume,
    _budget_left_str,
    _budget_today,
    _model_for,
    _reasoning_for,
    load_config,
)


def __getattr__(nombre):
    """`soou.WIKI`, `soou.ROOT`... se leen de soou_core.rutas (PEP 562). Para
    CAMBIAR una ruta, asignala en `soou.rutas` (o usa rutas.configurar)."""
    if nombre in rutas.NOMBRES:
        return getattr(rutas, nombre)
    raise AttributeError(f"module 'soou' has no attribute {nombre!r}")
ASSETS = CODE_DIR if (CODE_DIR / "ui").exists() else CODE_DIR / "soou_data"


# ---------------------------------------------------------------------------
# Utilidades de frontmatter (mini parser YAML, solo pares clave: valor planos)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Índice de retrieval cacheado (F1/F2): los hooks recall/context corren en CADA
# prompt y antes releían + tokenizaban TODAS las notas cada vez. El índice
# vive en soou.db (SQLite embebido) con lo derivado de cada nota —tokens, enlaces,
# placeholder, meta, body— indexado por (mtime, size). En cada carga sólo se
# stat-ea el árbol (barato) y se re-parsea/re-tokeniza SOLO lo que cambió.
# El ranking es idéntico: sólo se elimina el trabajo repetido.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Config declarativa (Eje 2: escalar por PERFIL de despliegue · Eje 6: agentes).
# soou.config.json define el perfil activo (single/team/enterprise) → capacidades,
# y la lista de agentes del harness (agnóstico de proveedor). Si falta, defaults.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Budget enforcement — la VÁLVULA del Cost Router. budget_per_day = máximo de
# RUNS de agente por día (1 run = 1 invocación de agente; proxy medible del costo).
# None = ILIMITADO (perfil single por defecto → no se aplica nada, retrocompatible).
# Ledger en ROOT/.soou-budget.json: {'YYYY-MM-DD': conteo}. Todo tolerante a errores.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Las REGLAS como parte del cerebro.
#
# schema.yaml define como se mantiene el cerebro y agentes-cerebro.md como debe usarlo un
# agente, pero los dos viven FUERA de wiki/ y el indice solo recorre wiki/. O sea
# que las reglas nunca aparecian en un brain_recall: un agente las respetaba solo
# si se le ocurria abrir el archivo.
#
# Se exponen como notas sinteticas de solo lectura en vez de moverlas, porque el
# destilador y los clientes leen esos archivos donde estan. No se guardan en
# soou.db: se arman al vuelo, asi no hay copia que se desincronice.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------

def cmd_ingest(args):
    src = Path(args.file).expanduser()
    if not src.exists():
        sys.exit(f"[error] no existe la fuente: {src}")
    if args.area not in indice.AREAS:
        sys.exit(f"[error] area invalida: {args.area} (usa: {', '.join(indice.AREAS)})")

    dest_dir = rutas.SOURCES / args.area
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    if dest.exists():
        dest = dest_dir / f"{src.stem}-{indice.today()}{src.suffix}"
    shutil.copy2(src, dest)
    rel_src = dest.relative_to(rutas.ROOT).as_posix()

    title = args.title or src.stem
    name = indice.slugify(title)
    note_dir = rutas.WIKI / args.area
    note_dir.mkdir(parents=True, exist_ok=True)
    note_path = note_dir / f"{name}.md"
    if note_path.exists():
        print(f"[aviso] ya existe la nota {note_path.name}; solo se registro la fuente.")
    else:
        note_path.write_text(
            "---\n"
            f"name: {name}\n"
            f"area: {args.area}\n"
            "type: source\n"
            f"sources: [{rel_src}]\n"
            "pinned: false\n"
            f"updated: {indice.today()}\n"
            "---\n\n"
            f"# {title}\n\n"
            f"{indice.PLACEHOLDER_TAG}: fuente registrada, pendiente de sintetizar por el LLM.\n\n"
            f"Fuente cruda: `{rel_src}`\n\n"
            "Enlaza a conceptos relacionados con [[nombre-de-nota]].\n",
            encoding="utf-8",
        )
        print(f"[ok] nota placeholder creada: {note_path.relative_to(rutas.ROOT).as_posix()}")
    print(f"[ok] fuente archivada (inmutable): {rel_src}")
    print("\nSiguiente paso: pedile a Claude Code que 'ingiera' esta fuente en la wiki.")


# ---------------------------------------------------------------------------
# query
# ---------------------------------------------------------------------------

def cmd_query(args):
    notes = indice.load_notes()
    if not getattr(args, "privadas", False):   # un agente tambien puede llamar al CLI
        notes = indice.para_agentes(notes)
    fused, backend = indice.rank_notes(" ".join(args.text), notes)
    if not fused:
        print("Sin coincidencias.")
        return
    print(f"(retrieval hibrido BM25 + {backend}, fusion RRF)\n")
    for score, n in fused[: args.limit]:
        rel = n["path"].relative_to(rutas.ROOT).as_posix()
        ls = indice.note_links(n)
        print(f"[{n['area']}] {n['name']}  (rrf {score:.4f})  {rel}")
        snippet = re.sub(r"\s+", " ", n["body"]).strip()[:200]
        print(f"    {snippet}")
        if ls:
            print(f"    aristas -> {', '.join(ls)}")
        print()


# ---------------------------------------------------------------------------
# lint
# ---------------------------------------------------------------------------

def cmd_lint(args):
    """Chequeo de salud del cerebro. Los datos salen de resumen_salud(), el mismo
    que consumen la UI y el MCP: antes esta funcion repetia toda esa logica y
    habia que arreglar cada caso dos veces."""
    s = indice.resumen_salud()

    def section(titulo, items, fmt=str):
        print(f"\n{titulo}: {len(items)}")
        for it in items:
            print(f"  - {fmt(it)}")

    print(f"== lint: {s['total']} notas ==")
    section("Enlaces rotos", s["broken"],
            lambda x: f"{x['from']} -> [[{x['to']}]] (no existe)")
    section("Obsoletas (la fuente cambio)", s["stale"])
    section("Conceptos huerfanos (nadie los enlaza)", s["orphans"])
    section("Placeholders pendientes de sintetizar", s["placeholders"])
    section("Contradicciones registradas (revisar)", s["contradictions"])
    section("Posibles duplicados", s["duplicates"], lambda x: f"{x[0]} ~ {x[1]}")
    section("Vigencia (reemplazos rotos o vigentes en conflicto)", s["vigencia"],
            lambda x: f"{x['nota']}: {x['problema']}")

    problemas = (len(s["broken"]) + len(s["placeholders"]) + len(s["stale"])
                 + len(s["vigencia"]))
    print(f"\nSalud: {'OK' if problemas == 0 else str(problemas) + ' pendientes'}")


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Salud y escritura de notas. Las comparten la UI, el CLI y el MCP: hasta aca la
# logica de salud vivia solo dentro de la clase de la UI, y escribir una nota
# solo se podia desde la UI, asi que un agente no tenia como corregir una nota
# mal destilada: solo podia encolar material nuevo y esperar.
# ---------------------------------------------------------------------------


def cmd_stats(args):
    notes, nodes, edges = indice.compute_graph()
    by_type = {}
    for n in notes:
        t = n["meta"].get("type", "?")
        by_type[t] = by_type.get(t, 0) + 1
    concepts = sum(v for t, v in by_type.items() if t in indice.CONCEPT_TYPES)
    docs = by_type.get("doc", 0)
    undistilled = sum(1 for n in notes if n["meta"].get("type") == "doc"
                      and n["meta"].get("distilled") != "yes")
    distilled_docs = docs - undistilled
    projects = by_type.get("project", 0)
    stale = sum(1 for n in notes if indice.is_stale(n))

    print(f"Notas totales   : {len(notes)}")
    for t in sorted(by_type):
        print(f"  {t:11}: {by_type[t]}")
    print(f"Aristas         : {len(edges)}")
    # metrica HONESTA: sintesis real = notas-concepto destiladas a mano,
    # sobre el material que aun es import crudo (doc).
    base = concepts + undistilled
    cov = (concepts / base * 100) if base else 0
    print(f"Conceptos (dest): {concepts}")
    print(f"Docs destilados : {distilled_docs}  ·  sin destilar: {undistilled}")
    print(f"Cobertura sintesis: {cov:.0f}%  ({concepts} concept / {base} sin cubrir)")
    if stale:
        print(f"⚠️ Obsoletas    : {stale}  (fuente cambio; corre 'lint')")
    print(f"Retrieval       : BM25 + {indice.embed_backend() or 'tfidf'} (fusion RRF)")


# ---------------------------------------------------------------------------
# context — emite el contexto del cerebro como JSON para el hook SessionStart
# ---------------------------------------------------------------------------


def detect_project(cwd: str, notes: list) -> dict | None:
    """Proyecto de la carpeta de trabajo: el mismo criterio que el paquete de
    contexto (soou_core/contexto.py), por componentes de ruta y no por
    subcadena (api ya no coincide dentro de api-movil)."""
    proys = contexto.proyecto_de(cwd, notes)
    return proys[0] if proys else None


def cmd_context(args):
    # subsesion de destilacion o agente bajo evaluacion: sin contexto del cerebro
    if os.environ.get("BRAIN_DISTILL") or rutas.en_evaluacion():
        return
    notes, nodes, edges = indice.compute_graph()
    hook = sesiones.read_hook_stdin()
    cwd = hook.get("cwd") or ""
    active = detect_project(cwd, notes)

    lines = [f"🧠 Cerebro (LLM Wiki): {len(notes)} notas, {len(edges)} aristas. "
             f"CLI: soou (query·recall·sync·lint·eval)."]

    if active:
        # Foco: el proyecto donde estas + los conceptos de su vertical
        vert = next((l[5:] for l in indice.note_links(active) if l.startswith("vert-")), None)
        lines.append(f"\n▶ Proyecto activo: {active['name'].replace('proj-','')}"
                     + (f"  ·  vertical: {vert}" if vert else ""))
        by_name = {n["name"]: n for n in notes}
        related = []
        for n in notes:
            if n["meta"].get("type") in indice.CONCEPT_TYPES:
                ls = indice.note_links(n)
                if (vert and f"vert-{vert}" in ls) or active["name"] in ls \
                        or (vert and n["name"].startswith(vert)):
                    related.append(n["name"])
        if related:
            lines.append("  conceptos relevantes: " + ", ".join(sorted(set(related))[:12]))
    else:
        # Panorama: proyectos por vertical
        by_vert = {}
        for n in notes:
            if n["meta"].get("type") == "project":
                v = next((l[5:] for l in indice.note_links(n) if l.startswith("vert-")), "otros")
                by_vert.setdefault(v, []).append(n["name"].replace("proj-", ""))
        for v in sorted(by_vert):
            lines.append(f"  · {v}: {', '.join(sorted(by_vert[v]))}")

    stale = sum(1 for n in notes if indice.is_stale(n))
    if stale:
        lines.append(f"\n⚠️ {stale} nota(s) con fuente cambiada — corre 'python soou.py lint'.")

    out = {"hookSpecificOutput": {"hookEventName": "SessionStart",
                                  "additionalContext": "\n".join(lines)}}
    print(json.dumps(out, ensure_ascii=False))


def cmd_router_log(args):
    logf = rutas.ROOT / "router-log.jsonl"
    if not logf.exists():
        print("Sin decisiones registradas todavía.")
        return
    rows = [json.loads(l) for l in logf.read_text(encoding="utf-8").splitlines() if l.strip()]
    dist = {}
    for r in rows:
        dist[r["tier"]] = dist.get(r["tier"], 0) + 1
    print(f"== router-log: {len(rows)} decisiones ==")
    for t in sorted(dist):
        print(f"  {t}: {dist[t]}")
    print("\nÚltimas 8:")
    for r in rows[-8:]:
        print(f"  [{r['tier']}] {r['prompt'][:70]}")


# ---------------------------------------------------------------------------
# eval — calidad del retrieval con consultas de respuesta conocida. Sin esto, decidir
# si hacen falta embeddings es opinion: aca se mide hit@1, recall@k y MRR, separando
# consultas lexicas (comparten palabras con la nota) de parafrasis (no las comparten).
# ---------------------------------------------------------------------------


def cmd_eval(args):
    notes = indice.load_notes()
    archivo = Path(args.archivo) if getattr(args, "archivo", None) else rutas.EVAL_DIR / "consultas.json"
    if getattr(args, "generar", 0):
        archivo.parent.mkdir(parents=True, exist_ok=True)
        previas = []
        if archivo.exists():
            previas = [c for c in json.loads(archivo.read_text(encoding="utf-8"))
                       .get("consultas", []) if c.get("tipo") != "auto"]
        nuevas = router.generar_consultas(notes, args.generar)
        archivo.write_text(json.dumps({"_nota": "auto = generadas; agrega las tuyas con "
                                       "tipo 'parafrasis' o 'manual'.",
                                       "consultas": previas + nuevas},
                                      ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[eval] {len(nuevas)} consulta(s) auto + {len(previas)} tuyas -> "
              f"{archivo.relative_to(rutas.ROOT).as_posix() if rutas.ROOT in archivo.parents else archivo}")
    if not archivo.exists():
        print("[eval] no hay consultas. Genera un set con: soou.py eval --generar 40")
        return
    consultas = json.loads(archivo.read_text(encoding="utf-8")).get("consultas", [])
    r = router.evaluar_retrieval(consultas, notes, args.k)
    k = r["k"]
    print(f"== eval retrieval · {r['global']['n']} consultas · {len(notes)} notas ==")
    print(f"{'conjunto':<12} {'n':>4} {'hit@1':>7} {f'recall@{k}':>9} {'mrr@10':>7}")
    for nombre, a in [("global", r["global"])] + list(r["por_tipo"].items()):
        print(f"{nombre:<12} {a['n']:>4} {a['hit@1']:>7.2f} {a[f'recall@{k}']:>9.2f} {a['mrr@10']:>7.2f}")
    fallos = [f for f in r["filas"] if not f["rango"] or f["rango"] > k]
    if fallos and getattr(args, "detalle", False):
        print(f"\nFuera del top {k}:")
        for f in fallos:
            print(f"  · [{f['tipo']}] {f['q'][:70]}  -> esperaba {', '.join(f['relevantes'])}")
    if getattr(args, "json", False):
        print(json.dumps({x: r[x] for x in ("k", "global", "por_tipo")}, ensure_ascii=False))


def cmd_classify(args):
    notes = indice.load_notes()
    prompt = " ".join(args.text)
    fused, _ = indice.rank_notes(prompt, notes)
    tier, reason, strat = router.classify_task(prompt, notes, fused)
    level, flow = router.flow_for(tier, reason)
    print(f"Tarea   : {prompt}")
    print(f"Nivel   : {level}   ({tier})")
    print(f"Flujo   : {flow}")
    print(f"Señales : {reason}")
    print(f"Detalle : {strat}")


# ---------------------------------------------------------------------------
# Harness ejecutor: AgentAdapter (agnóstico) + Runner (nivel -> agente + fallback)
# Ver [[soou-harness]] y [[soou-harness-ref]]. MVP: fallback ordenado + inyección
# de contexto del cerebro por nivel. Pendiente: worktree aislado, validators, budget.
# ---------------------------------------------------------------------------

from soou_core.agentes import (  # noqa: F401  (re-export)
    AgentAdapter,
    ClaudeAdapter,
    CodexAdapter,
    GenericAdapter,
    RECIPE_REGISTRY,
    RecipeAdapter,
    _launch,
)


# ---------------------------------------------------------------------------
# REGISTRO DE RECETAS: cómo se ejecuta headless cada CLI agéntica de código conocida
# (verificado contra docs oficiales/GitHub, 2026-09-04). Cada receta define bins, la
# invocación no-interactiva con ESCRITURA autónoma ("write") y de solo-lectura/plan
# ("read"), su model_flag opcional, y cómo se pasa el prompt. `{prompt}` = la tarea.
# Best-effort por versión: valida con `soou.py agents --probe` y ajusta en la config.
# Fuentes verificadas: aider.chat/docs/scripting (--message/--yes/--model) ·
# google-gemini/gemini-cli headless (-p, -m; --approval-mode yolo) · cursor.com/docs/cli
# (-p --force) · opencode.ai/docs/cli (run --auto -m) · goose-docs.ai headless (run -t) ·
# ampcode.com (-x) · charmbracelet/crush (run --yolo) · QwenLM/qwen-code · github.blog
# Copilot CLI (-p --allow-all-tools) · docs.openhands.dev (--headless -t) ·
# docs.factory.ai/droid-exec (exec --auto <low|medium|high> -m) · docs.aws.amazon.com Amazon Q
# (q chat --no-interactive --trust-all-tools) · docs.continue.dev headless (cn -p --allow "*").
# Otros → GenericAdapter (command+args) o receta propia en soou.config.json.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Marca de la terminal. Se dibuja en los momentos de arranque (init, ui), nunca
# en el servidor MCP: ahi stdout es del protocolo y stderr lo captura el cliente.
#
# Dos resguardos que importan mas que el dibujo:
#   · los caracteres de bloque revientan con UnicodeEncodeError en una consola
#     de Windows con codepage heredado (cp1252), asi que hay respaldo en ASCII;
#   · solo se dibuja si stdout es un terminal, para no ensuciar un pipe ni la
#     salida de la tarea programada.
# ---------------------------------------------------------------------------


def cmd_recall(args):
    """UserPromptSubmit: inyecta notas relevantes, con entity gate.
    Nombre/dato -> nota(s) exacta(s). Tema -> semillas + vecinos del grafo."""
    # subsesion de destilacion o agente bajo evaluacion: sin recall
    if os.environ.get("BRAIN_DISTILL") or rutas.en_evaluacion():
        return
    if not config.load_config().get("brain_context", True):   # envio apagado
        return
    todas, _, _ = indice.compute_graph()
    notes = indice.para_agentes(todas)      # lo inyectado llega al modelo del cliente
    texto_cli = " ".join(getattr(args, "text", []) or "").strip()
    # Con texto por argumento es uso manual: no se espera JSON del hook por stdin
    # (antes se quedaba colgado si stdin era un pipe abierto).
    hook = {} if texto_cli else sesiones.read_hook_stdin()
    prompt = (hook.get("prompt") or texto_cli).strip()
    if len(prompt) < 4:
        return
    # el mismo paquete que run y el MCP (soou_core/contexto.py); en el hook, sin
    # resumen del proyecto: llega en cada mensaje y el SessionStart ya lo da.
    paq = contexto.armar_paquete(prompt, todas, cwd=hook.get("cwd") or os.getcwd(),
                                 recorte=130, presupuesto=1200, resumen_proyecto=False)
    if not paq.piezas:
        return
    lines = contexto.como_hook(paq)

    # router advisory: sugerir orquestacion solo en tareas amplias/profundas
    tier, reason, strat = router.classify_task(prompt, notes, paq.ranking)
    router.log_classification(prompt, tier, reason)
    if tier in ("T2", "T3") or "RIESGO" in strat:
        lines.append(f"⚙️ Orquestación sugerida [{tier}]: {strat} ({reason})")

    out = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                  "additionalContext": "\n".join(lines)}}
    print(json.dumps(out, ensure_ascii=False))


# ---------------------------------------------------------------------------
# graph (HTML autonomo, force-directed en vanilla JS/canvas)
# ---------------------------------------------------------------------------

def cmd_graph(args):
    notes, nodes, edges = indice.compute_graph()
    data = json.dumps({"nodes": nodes, "edges": edges})
    by_area = {a: sum(1 for n in nodes if n["area"] == a) for a in indice.AREAS}
    ph = sum(1 for x in nodes if x["placeholder"])
    synth = len(nodes) - ph
    pct = (synth / len(nodes) * 100) if nodes else 0
    gen = dt.datetime.now().strftime("%Y-%m-%d %H:%M")

    html_doc = GRAPH_TEMPLATE.format(
        data=data,
        total=len(nodes),
        edges=len(edges),
        synth=synth,
        ph=ph,
        pct=f"{pct:.0f}",
        code=by_area["code"],
        personal=by_area["personal"],
        work=by_area["work"],
        gen=gen,
    )
    out = rutas.ROOT / "graph.html"
    out.write_text(html_doc, encoding="utf-8")
    print(f"[ok] grafo generado: {out}")
    if args.open:
        webbrowser.open(out.as_uri())


from soou_core.plantillas import (  # noqa: F401  (re-export)
    GRAPH_TEMPLATE,
)


# ---------------------------------------------------------------------------
# Retrieval hibrido: BM25 + vectorial (embeddings o TF-IDF) fusionados con RRF
# ---------------------------------------------------------------------------

import math

from soou_core.texto import (  # noqa: F401  (re-export)
    STOPWORDS,
    TOKENIZER_VERSION,
    _STOP_PLEGADAS,
    _cosine,
    _df,
    _keyphrases,
    _plegar,
    _raiz,
    _ranks,
    _sentences,
    _superficies,
    _textrank,
    _tfidf_vec,
    bm25_scores,
    tfidf_cosine,
    tokenize,
)


# ---------------------------------------------------------------------------
# scan — importa proyectos existentes y sus .md como notas enlazadas
# ---------------------------------------------------------------------------


def write_generated(path: Path, content: str):
    """Escribe solo si la nota no existe o fue generada por scan (respeta pinned/humanas)."""
    if path.exists():
        meta = indice.parse_note(path)["meta"]
        if meta.get("generated") != "scan" or meta.get("pinned") == "true" \
                or meta.get("distilled") == "yes":   # no pisar docs ya destilados
            return False
    path.write_text(content, encoding="utf-8")
    return True


def _motivo_para_no_escanear(root: Path, force: bool):
    """Motivo por el que NO conviene escanear esta raiz, o None si es segura.

    soou suele vivir dentro de la carpeta personal, y la raiz por defecto era su
    carpeta padre: escanear ahi importaba Documentos, Escritorio y carpetas
    sincronizadas al cerebro sin que nadie lo pidiera."""
    if force:
        return None
    if root == Path.home().resolve():
        return ("es tu carpeta personal. Pasa --root con una carpeta de proyectos, "
                "o --force si de verdad queres escanearla entera")
    if root == Path(root.anchor):
        return "es la raiz del disco. Elegi una carpeta de proyectos con --root"
    return None


def cmd_scan(args):
    root = Path(args.root).expanduser().resolve()
    if not root.exists():
        sys.exit(f"[error] no existe root: {root}")
    motivo = _motivo_para_no_escanear(root, getattr(args, "force", False))
    if motivo:
        print(f"[scan] omitido: {root} {motivo}.")
        return

    projects = []
    for d in sorted(root.iterdir()):
        if not d.is_dir() or d.name in router.SKIP_DIRS or d.name.startswith("."):
            continue
        if indice.slugify(d.name) in set(config.load_config()["exclude_projects"]):
            continue
        mds = [p for p in d.rglob("*.md")
               if not any(s in p.parts for s in router.SKIP_DIRS)]
        if not (d / ".git").exists() and not mds:
            continue
        projects.append((d, mds))

    proj_slugs = {indice.slugify(d.name): d.name for d, _ in projects}
    created = imported = 0

    # nota por vertical
    verts = {router.classify_vertical(d.name) for d, _ in projects}
    for v in sorted(verts):
        note = rutas.WIKI / "code" / f"vert-{v}.md"
        note.parent.mkdir(parents=True, exist_ok=True)
        if write_generated(note,
            f"---\nname: vert-{v}\narea: code\ntype: vertical\n"
            f"sources: []\npinned: false\ngenerated: scan\nupdated: {indice.today()}\n---\n\n"
            f"# Vertical: {v}\n\nAgrupa las plataformas y workers del dominio '{v}'.\n"):
            created += 1

    for d, mds in projects:
        pslug = indice.slugify(d.name)
        vert = router.classify_vertical(d.name)
        area = router.classify_area(d.name)
        rel_root = d.relative_to(root).as_posix()

        # arista frontend<->worker del mismo vertical
        pair_links = ""
        for other_slug, other_name in proj_slugs.items():
            if other_slug == pslug:
                continue
            if router.classify_vertical(other_name) == vert and vert not in ("core", "otros"):
                kind_self = "plataforma" in d.name.lower()
                kind_other = "plataforma" in other_name.lower()
                if kind_self != kind_other:
                    pair_links += f" [[proj-{other_slug}]]"

        note = rutas.WIKI / area / f"proj-{pslug}.md"
        note.parent.mkdir(parents=True, exist_ok=True)
        if write_generated(note,
            f"---\nname: proj-{pslug}\narea: {area}\ntype: project\n"
            f"sources: [{rel_root}]\npinned: false\ngenerated: scan\nupdated: {indice.today()}\n---\n\n"
            f"# {d.name}\n\nProyecto del vertical [[vert-{vert}]].{pair_links}\n\n"
            f"Ruta: `{rel_root}`  ·  {len(mds)} documentos markdown.\n"):
            created += 1

        # nota por cada .md del repo
        for md in mds:
            try:
                body = md.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            m = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
            title = m.group(1).strip() if m else md.stem
            relmd = md.relative_to(d).as_posix()
            mslug = indice.slugify(f"{pslug}-{md.stem}")
            # detectar menciones cruzadas a otros proyectos
            cross = ""
            low = body.lower()
            for other_slug, other_name in proj_slugs.items():
                if other_slug != pslug and other_name.lower() in low:
                    cross += f" [[proj-{other_slug}]]"
            snippet = re.sub(r"\s+", " ", re.sub(r"[#>*`\[\]()]", "", body)).strip()[:220]
            try:
                mtime = int(md.stat().st_mtime)
            except Exception:
                mtime = 0
            note = rutas.WIKI / area / f"{mslug}.md"
            if write_generated(note,
                f"---\nname: {mslug}\narea: {area}\ntype: doc\n"
                f"sources: [{(d.relative_to(root)/relmd).as_posix()}]\n"
                f"src_mtime: {mtime}\n"
                f"pinned: false\ngenerated: scan\nupdated: {indice.today()}\n---\n\n"
                f"# {title}\n\nDoc de [[proj-{pslug}]] ({relmd}).{cross}\n\n{snippet}\n"):
                imported += 1

    print(f"[ok] proyectos: {len(projects)}  ·  notas proyecto/vertical: {created}  ·  docs importados: {imported}")
    print("Corre  python soou.py stats  y  graph --open  para ver el resultado.")


# ---------------------------------------------------------------------------
# capture / sessions — captura la transcripcion y la deja lista para destilar
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Saneado de L0. Los clientes inyectan en el turno del usuario bloques que NO son
# la conversacion (recordatorios del sistema, CLAUDE.md, memoria, contexto del
# entorno). Guardarlos ensucia el destilado, repite kilobytes en cada sesion y
# puede filtrar contexto privado. Se quitan ANTES de escribir, junto con secretos.
# ---------------------------------------------------------------------------

from soou_core.saneado import (  # noqa: F401  (re-export)
    SESSION_MAX_CHARS,
    SESSION_MSG_MAX,
    _RUIDO_BLOQUES,
    _SECRETOS,
    _limpiar_texto,
    _linea_sesion,
)


# ---------------------------------------------------------------------------
# capture-codex — PULL de las sesiones de Codex (que NO tiene hooks). Lee sus
# rollouts .jsonl, extrae la conversacion (response_item role user/assistant) y
# la deja en sessions/raw como L0 pendiente, para que el pipeline de destilacion
# la trate igual que las de Claude Code. Es el Tramo 1 del harness ([[brain-harness]]).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# PULL universal de sesiones: registro (agente → función de import). Extensible:
# sumar una IA que persista transcripciones = agregar su fuente aquí. Los agentes
# con PUSH (hooks, p.ej. Claude Code) capturan solos y no van en el pull. Y la
# captura desde el Runner (_capture_run) cubre CUALQUIER agente orquestado por soou
# sin conocer su formato → cobertura universal combinando ambas vías.
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Politica de captura: QUE sesiones entran al cerebro.
#
# Los cosechadores leen carpetas GLOBALES de la maquina (~/.codex/sessions y
# ~/.claude/projects), no algo por proyecto. Sin filtro entra cualquier cosa:
# en esta maquina ya habia una transcripcion abierta desde C:\Windows\system32.
#
# Se filtra por el directorio de trabajo REAL que cada transcripcion registra,
# no por el nombre de su carpeta: Claude aplana la ruta del proyecto y ahi un
# guion separador no se distingue de uno que era parte del nombre.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Cosechador de Claude Code. Antes solo existia el de Codex, asi que una sesion
# de Claude entraba al cerebro UNICAMENTE si el hook SessionEnd estaba puesto:
# el sync programado no la veia. Con esto la captura deja de depender del hook
# y del cliente.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# distill — destilacion AUTONOMA: Claude headless convierte sesiones L0 crudas
# en notas-concepto enlazadas, sin humano en el loop. Modo conservador+auditable:
# aditivo (nunca borra), respeta pinned, y deja un log de cada corrida.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# distill-lite (NIVEL A): destilacion DETERMINISTA, SIN IA. Resumen extractivo
# (TextRank), keyphrases (TF-IDF) y auto-enlazado contra el grafo existente.
# Corre siempre, gratis e instantaneo -> el cerebro queda buscable sin depender
# de ningun proveedor. La sintesis conceptual (NIVEL B, IA) es un paso opcional
# ENCIMA. Reusa el motor de retrieval (tokenize/_df/_tfidf_vec/rank_notes).
# Ver [[soou-harness]] / [[llm-wiki]].
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# doctor — rutina de mantenimiento: re-escanea, lint y reporta que falta
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# compact — la contraparte de la destilacion. Hasta aca el pipeline solo CRECIA:
# el Nivel A crea una nota por sesion para siempre, el Nivel B nunca retira lo
# que el A dejo, el lint detecta duplicados pero nadie actua, y los crudos L0 no
# expiran nunca. Esto es lo unico del sistema que quita material.
#
# Por eso es deliberadamente conservador:
#   · ARCHIVA, no borra: todo se mueve a archivo/ con un manifiesto, fuera del
#     indice pero recuperable copiandolo de vuelta.
#   · SIMULA por defecto: hace falta --apply para tocar el disco.
#   · Nunca toca una nota con pinned: true (correccion humana, dice schema.yaml).
#   · Solo retira lo que esta PROBADAMENTE absorbido; lo dudoso lo reporta para
#     que lo mires vos, no lo resuelve solo.
# ---------------------------------------------------------------------------


def cmd_doctor(args):
    print("== brain doctor ==\n")
    print("0) Automatizacion:")
    for nombre, ok, detalle, _ in instalacion._chequear_instalacion():
        print("   " + ("[si]" if ok else "[--]") + " " + nombre + ": " + detalle)
    print()
    destino = getattr(args, "scan", None)
    if destino:
        print(f"1) Escaneando proyectos en {destino} ...")
        cmd_scan(argparse.Namespace(root=destino, force=getattr(args, "force", False)))
    else:
        print("1) Escaneo de proyectos: omitido "
              "(pasa --scan CARPETA para importar proyectos al cerebro).")
    print("\n2) Estado:")
    cmd_stats(argparse.Namespace())
    print("\n3) Salud (lint):")
    cmd_lint(argparse.Namespace())
    notes, _, _ = indice.compute_graph()
    docs = [n["name"] for n in notes if n["meta"].get("type") == "doc"
            and n["meta"].get("distilled") != "yes"]
    print(f"\n4) Sugerencia: hay {len(docs)} docs sin destilar. Pedile a Claude Code "
          f"'sintetiza el vertical X' para convertirlos en notas-concepto enlazadas.")
    print("   Cuando termines, corre 'python soou.py reindex' (o cierra la sesion).")


# ---------------------------------------------------------------------------
# ui — app de escritorio (pywebview). Import lazy: el core sigue cero-deps.
# ---------------------------------------------------------------------------


def cmd_profile(args):
    """Muestra (o cambia) el perfil de despliegue activo y sus capacidades (Eje 2)."""
    if getattr(args, "set", None):
        try:
            data = json.loads(rutas.CONFIG_PATH.read_text(encoding="utf-8")) if rutas.CONFIG_PATH.exists() else {}
        except Exception:
            data = {}
        if args.set not in config._DEFAULT_PROFILES and args.set not in (data.get("profiles") or {}):
            print(f"[error] perfil desconocido: {args.set}  (opciones: {', '.join(config._DEFAULT_PROFILES)})")
            return
        data["profile"] = args.set
        rutas.CONFIG_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[ok] perfil activo → {args.set}")
    cfg = config.load_config()
    print(f"perfil activo : {cfg['profile']}   (disponibles: {', '.join(cfg['profiles'])})")
    print("capacidades   :")
    for k, v in cfg["caps"].items():
        print(f"  · {k}: {v}")
    print("agentes       :")
    for ad in registro.build_adapters():
        print(f"  · {ad.name}: {'disponible' if ad.available() else 'sin CLI en PATH'}")


def cmd_budget(args):
    """Muestra la válvula del Cost Router: cap diario, runs de hoy y restantes."""
    cfg = config.load_config()
    cap = cfg["caps"].get("budget_per_day")
    used = config._budget_today()
    print(f"perfil activo   : {cfg['profile']}")
    print(f"budget_per_day  : {'ilimitado' if cap is None else cap}")
    print(f"runs de hoy     : {used}")
    if cap is None:
        print("restantes       : ilimitado")
    else:
        print(f"restantes       : {max(cap - used, 0)}  ({config._budget_left_str()})")


# ---------------------------------------------------------------------------
# init — portabilidad: prepara soou en CUALQUIER máquina. Idempotente y seguro
# (nunca pisa wiki/ ni config existentes). Rutas detectadas en runtime, no absolutas.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="LLM Wiki personal - cerebro sintetizado")
    ap.add_argument("--version", action="version",
                    version=f"soou {__version__} (datos: {rutas.ROOT})")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("ingest", help="registra una fuente cruda y crea placeholder")
    p.add_argument("file")
    p.add_argument("--area", required=True, choices=indice.AREAS)
    p.add_argument("--title", default=None)
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("query", help="busca en la wiki por texto y aristas")
    p.add_argument("text", nargs="+")
    p.add_argument("--limit", type=int, default=8)
    p.add_argument("--privadas", action="store_true",
                   help="incluye las notas privadas (solo para ti, no para agentes)")
    p.set_defaults(func=cmd_query)

    p = sub.add_parser("lint", help="chequea salud de la wiki")
    p.set_defaults(func=cmd_lint)

    p = sub.add_parser("provenance", help="agrega procedencia a las notas generadas "
                       "antes de que existiera (vista previa; --apply escribe)")
    p.add_argument("--apply", action="store_true", help="escribe los cambios")
    p.set_defaults(func=destilado.cmd_provenance)

    p = sub.add_parser("stats", help="resumen en terminal")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("graph", help="genera grafo interactivo HTML")
    p.add_argument("--open", action="store_true", help="abrir en el navegador")
    p.set_defaults(func=cmd_graph)

    p = sub.add_parser("scan", help="importa proyectos existentes y sus .md")
    p.add_argument("--root", default=str(rutas.ROOT.parent),
                   help="carpeta con los proyectos (def: la que contiene soou)")
    p.add_argument("--force", action="store_true",
                   help="escanear aunque la raiz sea tu carpeta personal")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("context", help="sin argumentos: contexto de inicio (JSON para hook); "
                                       "con una tarea: qué notas vería un agente")
    p.add_argument("tarea", nargs="*")
    p.add_argument("--explain", action="store_true",
                   help="con una tarea: por qué entró cada nota y por qué quedaron fuera las demás")
    p.add_argument("--cwd", help="carpeta del proyecto (def: la actual)")
    p.add_argument("--presupuesto", type=int, help="caracteres de contenido (def: context_budget)")
    p.set_defaults(func=lambda a: contexto.cmd_context(a) if a.tarea else cmd_context(a))

    p = sub.add_parser("recall", help="notas relevantes a una consulta (JSON para hook)")
    p.add_argument("text", nargs="*")
    p.set_defaults(func=cmd_recall)

    p = sub.add_parser("classify", help="puntúa la complejidad y sugiere orquestación")
    p.add_argument("text", nargs="+")
    p.set_defaults(func=cmd_classify)

    p = sub.add_parser("run", help="orquesta una tarea: clasifica nivel + elige agente + ejecuta")
    p.add_argument("task", nargs="*")
    p.add_argument("--dry", action="store_true", help="muestra el plan sin ejecutar el agente")
    p.add_argument("--plan", action="store_true", help="descompone una tarea compleja en subtareas (Planner)")
    p.add_argument("--write", action="store_true", help="permite que el agente escriba archivos")
    p.add_argument("--no-brain", action="store_true",
                   help="no envia contexto del cerebro al agente")
    p.add_argument("--resume", metavar="WORKTREE", help="reanuda un plan cortado desde la ruta de su worktree")
    p.set_defaults(func=harness.cmd_run)

    p = sub.add_parser("trace", help="intentos de un run: agente, estado, motivo y verificaciones")
    p.add_argument("run_id", nargs="?", help="id del run (sin id: los ultimos)")
    p.add_argument("-n", type=int, default=10, help="cuantos runs listar sin id")
    p.set_defaults(func=ejecutor.cmd_trace)

    p = sub.add_parser("profile", help="perfil de despliegue activo + capacidades + agentes")
    p.add_argument("--set", help="cambia el perfil activo (single/team/enterprise)")
    p.set_defaults(func=cmd_profile)

    p = sub.add_parser("budget", help="válvula del Cost Router: cap diario de runs, usados y restantes")
    p.set_defaults(func=cmd_budget)

    p = sub.add_parser("agents", help="preflight/autodetección de agentes del harness")
    p.add_argument("--probe", action="store_true", help="lanza cada agente de verdad (--version) para validar el launch")
    p.add_argument("--detect", action="store_true", help="autodescubre qué IAs agénticas conocidas están instaladas")
    p.add_argument("--conformance", metavar="NOMBRE",
                   help="prueba de conformidad de un agente (lo lanza hasta 4 veces)")
    p.add_argument("--sin-timeout", action="store_true",
                   help="con --conformance: omite el caso de timeout")
    p.set_defaults(func=conformidad.cmd_agents)

    p = sub.add_parser("router-log", help="resumen de decisiones del router (calibración)")
    p.set_defaults(func=cmd_router_log)

    p = sub.add_parser("eval", help="mide la calidad del retrieval (hit@1, recall@k, MRR)")
    p.add_argument("--archivo", help="consultas JSON (def: eval/consultas.json)")
    p.add_argument("--generar", type=int, default=0, metavar="N",
                   help="genera N consultas automaticas desde tus notas")
    p.add_argument("-k", type=int, default=5, help="profundidad del recall (def: 5)")
    p.add_argument("--detalle", action="store_true", help="lista las consultas falladas")
    p.add_argument("--json", action="store_true", help="ademas emite las metricas en JSON")
    p.add_argument("--tareas", action="store_true",
                   help="mide el valor de la memoria sobre el corpus inventado (con y sin memoria)")
    p.add_argument("--agente", action="append", metavar="NOMBRE",
                   help="con --tareas: agente real (repetible); sin esto usa el agente eco")
    p.add_argument("--guardar", action="store_true",
                   help="con --tareas: guarda el resultado en benchmarks/tareas/")
    p.add_argument("--si", action="store_true", help="con --agente: no pide confirmacion")
    p.set_defaults(func=lambda a: evaluacion.cmd_eval_tareas(a) if a.tareas else cmd_eval(a))

    p = sub.add_parser("uninstall",
                       help="quita la integracion con el sistema (no toca tu conocimiento)")
    p.add_argument("--dry-run", action="store_true", help="muestra que haria, sin tocar nada")
    p.add_argument("--cache", action="store_true",
                   help="ademas borra artefactos derivados (indice, locks, logs)")
    p.set_defaults(func=instalacion.cmd_uninstall)

    p = sub.add_parser("status", help="verifica que la automatizacion este instalada y viva")
    p.add_argument("--repair", action="store_true", help="reinstala las piezas rotas")
    p.add_argument("--interval", type=int, default=5,
                   help="minutos entre syncs al reparar el trigger")
    p.set_defaults(func=instalacion.cmd_status)

    p = sub.add_parser("compact",
                       help="retira del cerebro lo ya absorbido (simula salvo --apply)")
    p.add_argument("--apply", action="store_true", help="aplica de verdad; por defecto simula")
    p.add_argument("--dias", type=int, default=compactar.COMPACT_RAW_DIAS,
                   help="antiguedad minima para archivar un crudo ya sintetizado")
    p.add_argument("--recuperar", action="store_true",
                   help="devuelve a la cola las sesiones destiladas que perdieron su nota")
    p.set_defaults(func=compactar.cmd_compact)

    p = sub.add_parser("doctor", help="mantenimiento: stats + lint + reporte")
    p.add_argument("--scan", metavar="CARPETA",
                   help="ademas importa proyectos desde esa carpeta")
    p.add_argument("--force", action="store_true",
                   help="permite escanear aunque sea tu carpeta personal")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("capture", help="captura la transcripcion de sesion (hook SessionEnd)")
    p.set_defaults(func=sesiones.cmd_capture)

    p = sub.add_parser("sessions", help="lista sesiones capturadas y su estado de destilado")
    p.add_argument("--limpiar", action="store_true",
                   help="sanea los L0 existentes (ruido inyectado, secretos, tope de tamaño)")
    p.add_argument("--apply", action="store_true",
                   help="con --limpiar: escribe (antes guarda respaldo en archivo/)")
    p.add_argument("--importar", metavar="ID_O_RUTA",
                   help="importa UNA sesion descartada (por id o ruta del .jsonl)")
    p.add_argument("--proyecto", metavar="CARPETA",
                   help="con --importar: la carpeta real del proyecto al que pertenece")
    p.set_defaults(func=sesiones.cmd_sessions)

    p = sub.add_parser("init", help="prepara soou en esta máquina (workspace + sync automático); portable")
    # hooks y MCP ahora son el DEFAULT: un init a secas dejaba al usuario sin
    # captura en tiempo real y sin el cerebro expuesto a su agente.
    p.add_argument("--no-hooks", action="store_true",
                   help="no instalar los hooks de Claude Code")
    p.add_argument("--no-mcp", action="store_true",
                   help="no registrar el servidor MCP en Codex/Claude")
    p.add_argument("--hooks", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--mcp", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--shortcut", action="store_true", help="crea un acceso directo a la UI en el escritorio (Windows)")
    p.add_argument("--no-trigger", action="store_true", help="no instalar el trigger de sync automático")
    p.add_argument("--interval", type=int, default=5, help="minutos entre syncs (default 5)")
    p.add_argument("--preview", action="store_true",
                   help="muestra lo que haria, sin cambiar nada")
    p.add_argument("--minimal", action="store_true",
                   help="solo workspace e indice: sin sync automatico, hooks ni MCP")
    p.add_argument("--yes", "-y", action="store_true",
                   help="no pide confirmacion (scripts)")
    p.set_defaults(func=instalacion.cmd_init)

    p = sub.add_parser("sync", help="sincronización universal (cualquier IA): captura + Nivel A; --deep añade Nivel B")
    p.add_argument("--deep", action="store_true", help="además Nivel B (síntesis IA con cualquier agente de escritura)")
    p.add_argument("--limit", type=int, default=0, help="tope de sesiones para el Nivel B")
    p.set_defaults(func=ejecutor.cmd_sync)

    p = sub.add_parser("distill", help="destila las sesiones pendientes (Nivel A sin IA + Nivel B con cualquier agente)")
    p.add_argument("--limit", type=int, default=0, help="destilar solo las primeras N pendientes")
    p.add_argument("--lite", action="store_true", help="solo Nivel A: destilado deterministico sin IA (gratis)")
    p.add_argument("--source", choices=["codex", "other"], help="limita la destilacion a una fuente")
    p.add_argument("--force", action="store_true", help="re-destila TODO: resetea marcas 'distilled' antes de correr (Nivel A y B)")
    p.set_defaults(func=ejecutor.cmd_distill)

    p = sub.add_parser("persona", help="sintetiza la nota-persona L3 del usuario desde el cerebro")
    p.set_defaults(func=ejecutor.cmd_persona)

    p = sub.add_parser("capture-codex", help="importa (pull) las sesiones de Codex como L0 pendiente")
    p.set_defaults(func=sesiones.cmd_capture_codex)

    p = sub.add_parser("reindex", help="reconstruye el índice soou.db desde los .md (sin Docker)")
    p.add_argument("--full", action="store_true", help="borra y reconstruye todo el índice")
    p.set_defaults(func=indice.cmd_reindex)

    p = sub.add_parser("ui", help="abre la app de escritorio (pywebview)")
    p.add_argument("--browser", action="store_true",
                   help="forzar el modo navegador aunque pywebview este instalado")
    p.add_argument("--port", type=int, default=0,
                   help="puerto del servidor local (0 = uno libre)")
    p.set_defaults(func=ui.cmd_ui)

    p = sub.add_parser("session-end", help="hook SessionEnd: captura + dispara destilacion en background")
    p.set_defaults(func=destilado.cmd_session_end)

    p = sub.add_parser("distill-mark", help="marca docs como destilados (distilled: yes)")
    p.add_argument("names", nargs="+")
    p.set_defaults(func=sesiones.cmd_distill_mark)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
