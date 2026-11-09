"""Modulo destilado de soou (extraido de soou.py).

soou.py lo re-exporta; dentro de soou_core se usa como `destilado.nombre`."""

from pathlib import Path
import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
from soou_core import agentes, indice, rutas, sesiones, texto, vigencia


def build_distill_prompt(pend: list[Path], notes: list) -> str:
    names = ", ".join(sorted(n["name"] for n in notes))
    rels = "\n".join(f"  - {p.relative_to(rutas.ROOT).as_posix()}" for p in pend)
    return f"""Eres el destilador autonomo de un "cerebro" LLM-Wiki (patron Karpathy).
Tu trabajo: convertir transcripciones de sesiones crudas en notas-concepto
enlazadas, siguiendo estrictamente las reglas de `schema.yaml` (leelo primero).

REGLAS (modo conservador, no negociable):
- Una nota = UN concepto. Frontmatter completo (name, area, type, sources, pinned, updated).
- PROCEDENCIA Y VIGENCIA en cada nota que crees o cambies:
  `source_session: <ruta de la sesion>` (y `source_run:` si su cabecera trae `Run:`),
  `recorded_at: <fecha ISO>` (solo al crearla) y `verification:` copiada de la
  cabecera `Verificacion:` de la sesion; si no la trae, `sin_verificar`. NUNCA
  escribas `tests_passed` ni `revisado` si la sesion no lo dice: lo que un agente
  afirmo es `afirmado_por_agente`.
- Una decision nueva que reemplaza a otra NO la borra: en la nueva pon
  `supersedes: <vieja>` y `status: vigente`; en la vieja, `status: reemplazada` y
  `superseded_by: <nueva>` (si la vieja esta pinned, no la toques: registra la
  contradiccion aparte).
- Enlaza con [[nombre]] a notas existentes; crea aristas ricas.
- Extrae SOLO conocimiento consolidado y reutilizable (decisiones, hechos, arquitectura).
  Ignora pasos triviales (comandos sueltos, "corre el test", debugging intermedio).
- Prefiere ACTUALIZAR una nota existente antes que crear una duplicada.
- NUNCA toques una nota con `pinned: true`. NUNCA borres nada.
- Ante fuentes que se contradicen: registrar-no-inferir (nota-contradiccion aparte, enlazada).
- Las notas nuevas van en wiki/code|personal|work/ segun corresponda.

NOTAS EXISTENTES (para enlazar y no duplicar):
{names}

SESIONES PENDIENTES DE DESTILAR (leelas con Read, estan bajo {rutas.SESSIONS.relative_to(rutas.ROOT).as_posix()}/):
{rels}

PASOS:
1. Lee schema.yaml y cada sesion pendiente.
2. Crea/actualiza las notas-concepto que correspondan (Write/Edit).
3. Al terminar cada sesion, marca su archivo cambiando `<!-- distilled: no -->`
   por `<!-- distilled: yes -->`.
4. Termina con un resumen: que notas creaste/actualizaste y que sesiones cerraste.
"""


def _auto_links(text: str, notes: list, limit: int = 8) -> list:
    low = " " + re.sub(r"\s+", " ", text.lower()) + " "
    hits = []
    for n in notes:
        if n["meta"].get("type") == "session":
            continue
        cand = n["name"].replace("-", " ").lower()
        if len(cand) >= 5 and cand in low:
            hits.append((len(cand), n["name"]))
    hits.sort(reverse=True)
    out = []
    for _, name in hits:
        if name not in out:
            out.append(name)
        if len(out) >= limit:
            break
    return out


def procedencia_de_sesion(path: Path) -> dict:
    """source_session (relativa a los datos), source_run y verification de una
    sesion cruda, segun su cabecera (vigencia.de_cabecera)."""
    try:
        rel = path.resolve().relative_to(rutas.ROOT.resolve()).as_posix()
    except (ValueError, OSError):
        rel = path.name
    try:
        cabecera = path.read_text(encoding="utf-8", errors="ignore")[:600]
    except OSError:
        cabecera = ""
    return {"source_session": rel, **vigencia.de_cabecera(cabecera)}


def distill_lite_one(path: Path, notes: list, corpus: list):
    s = sesiones._parse_session_raw(path)
    material = "\n".join(s["assistant"]) or "\n".join(s["user_asks"])
    if not material.strip():
        sesiones._mark_raw(path, "lite"); return None
    summary = texto._textrank(texto._sentences(material), 5)
    stoks = texto.tokenize(material)
    formas = texto._superficies(material)
    tags = [formas.get(t, t) for t in texto._keyphrases(stoks, corpus, 8)]
    links = _auto_links(material, notes, 8)
    area = s.get("area") if s.get("area") in indice.AREAS else None
    if area is None:            # sin area declarada: se infiere del grafo, como antes
        area = "work"
        try:
            fused, _ = indice.rank_notes(" ".join(tags) or material[:400], notes)
            if fused:
                area_counts = {}
                for _, note in fused[:5]:
                    candidate = note["meta"].get("area")
                    if candidate in indice.AREAS:
                        area_counts[candidate] = area_counts.get(candidate, 0) + 1
                if area_counts:
                    area = max(area_counts, key=area_counts.get)
        except Exception:
            pass
    if area not in indice.AREAS:
        area = "work"
    title = (s["user_asks"][0][:70] if s["user_asks"]
             else (tags[0] if tags else "sesion"))
    name = f"sesion-{s['source']}-{s['sid']}"
    existing = next((n for n in notes
                     if n["name"] == name
                     and n["meta"].get("origin") == "distill-lite"), None)
    dest = existing["path"] if existing else rutas.WIKI / area / f"{name}.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    proc = procedencia_de_sesion(path)
    previo = existing["meta"].get("recorded_at") if existing else None
    proc["recorded_at"] = previo or vigencia.ahora()     # cuando se registro, no se pisa
    out = ["---", f"name: {name}", f"area: {area}", "type: session",
           "sources: []", "origin: distill-lite", "pinned: false",
           f"updated: {indice.today()}"]
    out += [f"{k}: {proc[k]}" for k in ("source_session", "source_run", "recorded_at",
                                         "verification") if proc.get(k)]
    out += ["---", "",
           f"# {title}", "",
           f"Sesion **{s['source']}** · {s['date']} · {s['n_msgs']} mensajes "
           "(destilado deterministico, sin IA).", ""]
    if summary:
        out.append("## Resumen"); out += [f"- {x}" for x in summary]; out.append("")
    if tags:
        out.append("## Temas"); out.append(" · ".join(f"`{t}`" for t in tags)); out.append("")
    if links:
        out.append("## Relacionado"); out.append(" ".join(f"[[{l}]]" for l in links)); out.append("")
    dest.write_text("\n".join(out), encoding="utf-8")
    sesiones._mark_raw(path, "lite")
    return name


def migrar_procedencia(aplicar: bool = False) -> list:
    """Migracion OPCIONAL: agrega source_session, verification y recorded_at a
    las notas que genero el Nivel A antes de que existieran esos campos. Solo
    toca notas generadas (origin: distill-lite) sin source_session y nunca una
    pinned. Sin `aplicar` solo informa. Devuelve [(nota, campos)]."""
    if not rutas.SESSIONS or not rutas.SESSIONS.exists():
        return []
    por_clave = {}
    for f in rutas.SESSIONS.rglob("session-*.md"):
        clave = f"sesion-{sesiones._source_of_session(f.name)}-{sesiones._sid_of_session(f.name)}"
        por_clave.setdefault(clave, f)
    cambios = []
    for n in indice.build_index():
        m = n["meta"]
        generada = m.get("origin") == "distill-lite"
        if not generada or m.get("source_session") or str(m.get("pinned", "")).lower() == "true":
            continue
        sesion = por_clave.get(n["name"])
        if sesion is None:
            continue
        proc = procedencia_de_sesion(sesion)
        proc["recorded_at"] = m.get("updated") or vigencia.ahora()
        campos = {k: proc[k] for k in ("source_session", "source_run", "recorded_at",
                                       "verification") if proc.get(k) and not m.get(k)}
        if not campos:
            continue
        cambios.append((n["name"], campos))
        if aplicar:
            raw = n["path"].read_text(encoding="utf-8")
            fin = raw.find("\n---", 3)
            if not raw.startswith("---") or fin == -1:
                continue
            extra = "".join(f"\n{k}: {v}" for k, v in campos.items())
            n["path"].write_text(raw[:fin] + extra + raw[fin:], encoding="utf-8")
    if aplicar and cambios:
        indice.build_index()
    return cambios


def cmd_provenance(args):
    """`soou provenance [--apply]`: la migracion opcional de procedencia."""
    aplicar = getattr(args, "apply", False)
    cambios = migrar_procedencia(aplicar)
    if not cambios:
        print("[ok] ninguna nota generada sin procedencia.")
        return
    for nombre, campos in cambios[:10]:
        print(f"  {nombre}: " + ", ".join(f"{k}={v}" for k, v in campos.items()))
    if len(cambios) > 10:
        print(f"  ... y {len(cambios) - 10} mas")
    print(f"[{'ok' if aplicar else 'vista previa'}] {len(cambios)} nota(s) "
          + ("actualizadas." if aplicar else "se actualizarian; corre con --apply para escribir."))


def _write_last_sync(made):
    data = {
        "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
        "notes_created": len(made),
        "names": made,
        "agent": "distill-lite",
    }
    (rutas.ROOT / "last-sync.json").write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def run_lite(source: str | None = None) -> list:
    """NIVEL A sobre todo lo pendiente. Devuelve nombres de notas creadas."""
    pend = sesiones.lite_pending(source)
    if not pend:
        return []
    notes = indice.load_notes()
    corpus = indice.build_corpus(notes)
    made = []
    for p in pend:
        try:
            name = distill_lite_one(p, notes, corpus)
            if name:
                made.append(name)
        except Exception as e:
            print(f"[aviso] distill-lite fallo en {p.name}: {e}")
    if made:
        _write_last_sync(made)
    return made


def _launch_distill_detached():
    """Lanza `soou.py distill` como proceso independiente que sobrevive al hook
    (no bloquea el cierre de sesion). No hace nada si ya estamos destilando o si
    no hay pendientes."""
    if os.environ.get("BRAIN_DISTILL") or not sesiones.pending_sessions():
        return
    agentes.lanzar_desacoplado(["distill"], cwd=rutas.ROOT)


def cmd_session_end(args):
    """Hook SessionEnd: captura la transcripcion (L0) y dispara la destilacion
    autonoma en background para la(s) sesion(es) recien cerrada(s)."""
    if rutas.en_evaluacion():       # un agente evaluado no deja sesion en el cerebro
        return
    sesiones.cmd_capture(args)
    _launch_distill_detached()


def cmd_sync(args):
    """Sincronización UNIVERSAL (sea cual sea la IA): trae lo pendiente de todas las
    fuentes de sesión conocidas + lo capturado del Runner, y destila Nivel A (sin IA).
    Con --deep, además Nivel B (síntesis con cualquier agente de escritura). Es el verbo
    que corre la tarea programada; no depende de ningún proveedor en particular."""
    cmd_distill(argparse.Namespace(lite=not getattr(args, "deep", False),
                                   limit=getattr(args, "limit", 0),
                                   invocador=getattr(args, "invocador", None)))
    _escribir_heartbeat()   # latido: permite saber si la automatizacion sigue viva


def cmd_distill(args):
    if os.environ.get("BRAIN_DISTILL"):
        return   # nunca destilar dentro de una destilacion
    if getattr(args, "force", False):
        n = sesiones._reset_marks(getattr(args, "source", None))
        print(f"[distill] --force: {n} sesion(es) reseteadas a 'distilled: no' "
              "(se re-destilan Nivel A y B).")
    if getattr(args, "lite", False):
        # NIVEL A solo: determinista, sin IA. Lock ligero propio (.distill-lite.lock,
        # distinto del .distill.lock de la destilacion completa): la tarea programada
        # corre 'distill --lite' cada 5 min y podria solaparse con una corrida manual.
        lite_lock = rutas.ROOT / ".distill-lite.lock"
        try:
            if lite_lock.exists() and (time.time() - lite_lock.stat().st_mtime) < 300:
                print("[distill-lite] otra corrida --lite en curso; se omite.")
                return
            lite_lock.write_text(str(os.getpid()))
        except OSError:
            pass
        try:
            try:
                sesiones._pull_all_sessions(refresh=True)
            except Exception as e:
                print(f"[aviso] pull Codex: {e}")
            source = getattr(args, "source", None)
            made = run_lite(source)
            if made:
                print(f"[distill-lite] {len(made)} nota(s) de sesion (sin IA): "
                      + ", ".join(made[:6]) + ("…" if len(made) > 6 else ""))
                indice.build_index()
            else:
                print("[distill-lite] sin sesiones nuevas.")
        finally:
            try:
                lite_lock.unlink(missing_ok=True)
            except OSError:
                pass
        return
    # lock entre instancias: el hook SessionEnd puede lanzar dos distill casi a la
    # vez -> destilarían las MISMAS sesiones (notas duplicadas + doble cuota). Si otro
    # corre (lock fresco < 30min), se omite; el próximo turno retoma lo pendiente.
    lock = rutas.ROOT / ".distill.lock"
    try:
        if lock.exists() and (time.time() - lock.stat().st_mtime) < 1800:
            print("[distill] otra destilacion en curso; se omite (evita duplicar).")
            return
        lock.write_text(str(os.getpid()))
    except OSError:
        pass
    try:
        _distill_run(args)
    finally:
        try:
            lock.unlink(missing_ok=True)
        except OSError:
            pass


def _distill_run(args):
    try:
        sesiones._pull_all_sessions(refresh=True)   # PULL: Codex no tiene hooks -> lo barremos aqui
    except Exception as e:
        print(f"[aviso] no se pudo importar sesiones de Codex: {e}")
    # NIVEL A (siempre, gratis): deja cada sesion buscable ya, sin IA.
    source = getattr(args, "source", None)
    made = run_lite(source)
    if made:
        print(f"[distill] NIVEL A (sin IA): {len(made)} nota(s) de sesion creadas.")
    pend = sesiones.pending_sessions(source)
    if not pend:
        print("[distill] no hay sesiones pendientes de sintesis IA (Nivel B).")
        indice.build_index()
        return
    limit = getattr(args, "limit", 0) or 0
    if limit:
        pend = pend[:limit]

    # NIVEL B: las llamadas a agentes las coordina el ejecutor (capa superior), que
    # pasa aqui como `invocador`: elige agentes de escritura por routing 'complex' e
    # historial, reserva presupuesto, verifica y traza cada intento. Este modulo solo
    # arma el lote, interpreta el resultado y decide el fallback. Corre en cwd=ROOT
    # (edita el wiki real, aditivo/conservador, sin worktree). Sin invocador o sin
    # agente de escritura -> queda solo el Nivel A.
    invocador = getattr(args, "invocador", None)
    writers = invocador.candidatos() if invocador else []
    if not writers:
        print("[distill] sin agente de escritura para el Nivel B (síntesis IA); queda el Nivel A."
              if invocador else
              "[distill] el Nivel B se lanza desde la CLI (soou distill/sync --deep); queda el Nivel A.")
        print("[distill] reindexando (soou.db)...")
        indice.cmd_reindex(argparse.Namespace(full=False))
        return
    rutas.DISTILL_DIR.mkdir(parents=True, exist_ok=True)
    succeeded = False
    for index, agent in enumerate(writers):
        # Un escritor anterior puede completar parte del lote antes de fallar.
        # Recalcular evita que el fallback repita sesiones ya marcadas.
        current = sesiones.pending_sessions(source)
        if limit:
            current = current[:limit]
        if not current:
            succeeded = True
            break
        prompt = build_distill_prompt(current, indice.para_agentes(indice.load_notes()))
        n_lote = len(current)
        # El ejecutor reserva el cupo, anuncia y aplica el Cost Router (nivel 'complex').
        res = invocador.invocar(
            agent, prompt, intento=index, operacion="distill",
            al_empezar=lambda ad: print(f"[distill] Nivel B: destilando {n_lote} sesion(es) "
                                        f"con {ad.name} (conservador, aditivo)..."))
        if res.codigo_error == "E_PRESUPUESTO":
            print(f"[aviso] Nivel B detenido: {res.respuesta}. Queda el Nivel A.")
            break   # sin cupo: ningun otro agente correria
        out = res.respuesta or ""
        ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        safe_agent = re.sub(r"[^A-Za-z0-9_-]", "-", agent.name)
        logf = rutas.DISTILL_DIR / f"distill-{ts}-{safe_agent}.txt"
        logf.write_text(out, encoding="utf-8")
        rel_log = logf.relative_to(rutas.ROOT).as_posix()

        if res.cumple("minimo"):
            succeeded = True
            print(f"[ok] destilacion completada con {agent.name}. Log auditable: {rel_log}")
            if out:
                print("  resumen: " + " ".join(out.split())[:500])
            break

        hint = ""
        if any(w in out.lower() for w in ("limit", "reset", "usage", "quota", "cuota")):
            hint = f"  (límite de uso de {agent.name})"
        print(f"[aviso] la destilacion con {agent.name} termino con error"
              f" ({res.motivo or res.estado}).{hint}")
        print(f"        el trabajo parcial es ADITIVO y se conserva. Log: {rel_log}")
        if index + 1 < len(writers):
            print(f"[distill] probando fallback: {writers[index + 1].name}...")

    if not succeeded:
        print("[aviso] ningun agente de escritura pudo completar el lote.")

    done = len(sesiones.pending_sessions(source))
    print(f"[distill] sesiones pendientes ahora: {done} (antes {len(pend)}).")
    # reindexar SIEMPRE: el trabajo es aditivo (nuevas notas), hay que dejar
    # soou.db al día aunque la corrida terminara en error (corte por cuota).
    print("[distill] reindexando (soou.db)...")
    indice.cmd_reindex(argparse.Namespace(full=False))


def cmd_persona(args):
    """Sintetiza la nota-persona L3 del usuario desde las notas del cerebro."""
    notes = indice.para_agentes(indice.load_notes())   # la sintesis la hace un modelo
    source_types = {"concept", "decision", "vertical"}
    selected = [n for n in notes
                if n["meta"].get("type") in source_types
                or n["meta"].get("pinned") == "true"][:40]
    material = "\n".join(
        f"- [[{n['name']}]] — {' '.join(n['body'].split())[:200]}"
        for n in selected
    )
    prompt = (
        "Sintetiza una nota PERSONA (capa L3) del usuario/proyecto a partir de "
        "estas notas del cerebro. Captura: (1) quien es y en que trabaja "
        "(dominios, verticales, proyectos), (2) decisiones y convenciones "
        "recurrentes, (3) principios/estilo de trabajo. Concisa (~30-50 lineas), "
        "en 3ra persona, en espanol. Escribe o REEMPLAZA el archivo "
        "wiki/personal/soou-persona.md con frontmatter:\n"
        "name: soou-persona\narea: personal\ntype: persona\nsources: []\n"
        f"pinned: true\nupdated: {dt.date.today().isoformat()}\n"
        "Enlaza con [[wikilinks]] a las notas mas relevantes. No toques ninguna "
        "otra nota.\n\n"
        f"Material del cerebro ({len(selected)} notas):\n{material}"
    )
    invocador = getattr(args, "invocador", None)
    if invocador is None or not invocador.candidatos():
        print("[persona] sin agente de escritura disponible; no se genero la nota.")
        return
    res, intentos = invocador.ejecutar(
        prompt, "persona",
        al_empezar=lambda ad, _req: print(f"[persona] sintetizando con {ad.name}..."),
        al_fallar=lambda ad, r: print(f"[aviso] persona con {ad.name}: {r.motivo or r.estado}"))
    if res is None:
        print("[aviso] ningun agente pudo sintetizar la persona.")
    else:
        print(f"[ok] sintesis de persona con {res.agente}.")
        if res.respuesta:
            print("  resumen: " + " ".join(res.respuesta.split())[:500])
    print("[persona] reindexando (soou.db)...")
    indice.cmd_reindex(argparse.Namespace(full=False))


def _escribir_heartbeat(notas_creadas: int = 0) -> None:
    """Latido de cada sync. Sin esto no habia forma de saber si la automatizacion
    seguia viva: last-sync.json solo se escribe cuando la destilacion produjo algo,
    asi que un sync sano pero sin novedades era indistinguible de uno muerto."""
    try:
        rutas.HEARTBEAT_PATH.write_text(json.dumps({
            "ts": dt.datetime.now().isoformat(timespec="seconds"),
            "interprete": sys.executable,
            "notas_creadas": notas_creadas,
        }, indent=2), encoding="utf-8")
    except OSError:
        pass
