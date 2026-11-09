"""`soou eval --tareas`: mide si la memoria aporta valor, sobre un corpus FIJO e
inventado (tests/fixtures/corpus/), separado del cerebro personal.

Por tarea y por agente, con memoria y sin memoria:
- resuelta: lectura = aparecen todos los hechos esperados; escritura = pasa la
  prueba oculta, que se copia al repo de juguete DESPUES de que el agente termina.
- error inducido: aparece el dato reemplazado y no el vigente.
- evidencia: cita [[notas]] que estaban en su contexto; las que no estaban
  cuentan como inventadas.
- secreto filtrado: el contenido de una nota privada aparece en la respuesta.
Y del contexto, sin agente: notas esperadas presentes, notas que no debian
entrar (reemplazadas, privadas, de otro proyecto) y caracteres por tarea.
Ademas, una reanudacion simulada: un plan cortado a mitad se retoma sin repetir
pasos.

El agente `eco` (por defecto, el de CI) no razona: devuelve su prompt. Es un
control determinista del armado de contexto y del propio evaluador; no resuelve
tareas ni mide a un agente. Con agentes reales cada tarea corre dos veces (con y
sin memoria), gasta cupo y corre aislada del cerebro real (rutas.modo_evaluacion):
las dos condiciones usan el mismo agente, permisos y herramientas; solo cambia el
contexto que va en el prompt.

Capa superior: usa contexto, ejecutor y harness."""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from soou_core import (config, contexto, contrato, ejecutor, harness, indice, registro, rutas,
                       sesiones, texto, vigencia)
from soou_core import agentes as agentes_mod

CORPUS = rutas.CODE_DIR / "tests" / "fixtures" / "corpus"
RESULTADOS = rutas.CODE_DIR / "benchmarks" / "tareas"
INSTRUCCION = ("Responde en no mas de tres frases. Si te basas en una nota del cerebro, citala "
               "como [[nombre]]. Si no tienes el dato, dilo: no lo inventes.")
TIMEOUT_LECTURA, TIMEOUT_ESCRITURA = 300, 600
CITA = re.compile(r"\[\[([^\]|#]+)")


class AgenteEco:
    """Devuelve su prompt: control determinista de CI, no un agente."""
    name = "eco"
    can_write = False
    restriccion_lectura = "texto"

    def available(self):
        return True

    def run(self, prompt, allow_write=False, **k):
        return {"ok": True, "output": prompt}


# ------------------------------------------------------------------ corpus

def cargar(corpus: Path = CORPUS):
    if not (corpus / "tareas.json").exists():
        raise FileNotFoundError(f"no hay corpus en {corpus} (eval --tareas corre desde un clon)")
    notas = [indice.parse_note(p) for p in sorted((corpus / "wiki").rglob("*.md"))]
    tareas = json.loads((corpus / "tareas.json").read_text(encoding="utf-8"))["tareas"]
    return notas, tareas


def _plano(t: str) -> str:
    return texto._plegar(t or "")


def cumple_hechos(respuesta: str, tarea: dict) -> bool:
    r = _plano(respuesta)
    grupos = tarea.get("hechos") or []
    return bool(grupos) and all(any(re.search(alt, r) for alt in g) for g in grupos)


def error_inducido(respuesta: str, tarea: dict) -> bool:
    r = _plano(respuesta)
    viejo = any(re.search(p, r) for p in tarea.get("prohibidos") or [])
    return viejo and not cumple_hechos(respuesta, tarea)


def citas(respuesta: str) -> set:
    return {c.strip() for c in CITA.findall(respuesta or "")}


def _repo(base: Path, corpus: Path, tarea: dict, sufijo: str) -> Path:
    """Copia del repo de juguete en `<tmp>/<id>-<sufijo>/<proyecto>`: la carpeta
    final con el nombre del proyecto es la que reconoce el paquete de contexto."""
    ws = base / f"{tarea['id']}-{sufijo}" / tarea["proyecto"]
    origen = corpus / "repos" / tarea["proyecto"]
    if origen.exists():
        shutil.copytree(origen, ws)
    else:
        ws.mkdir(parents=True)
    for args in (["init", "-q"], ["add", "-A"],
                 ["-c", "user.name=soou-eval", "-c", "user.email=eval@soou.invalid",
                  "commit", "-q", "--allow-empty", "-m", "base"]):
        subprocess.run(["git", "-C", str(ws), *args], capture_output=True, timeout=60)
    return ws


def _borrar(base: Path):
    """rmtree que tambien borra en Windows los objetos de git (solo lectura)."""
    def quitar_y_reintentar(fn, ruta, *_):
        try:
            os.chmod(ruta, 0o700)
            fn(ruta)
        except OSError:
            pass
    if sys.version_info >= (3, 12):
        shutil.rmtree(base, onexc=quitar_y_reintentar)
    else:
        shutil.rmtree(base, onerror=quitar_y_reintentar)


def _anonimo(texto_: str, base: Path) -> str:
    """Las rutas del temporal (llevan tu usuario) pasan a <tmp> en el informe."""
    texto_ = texto_ or ""
    formas = {str(base), str(base.resolve()), base.as_posix(), base.resolve().as_posix()}
    for forma in sorted(formas, key=len, reverse=True):
        texto_ = texto_.replace(forma.replace("\\", "\\\\"), "<tmp>").replace(forma, "<tmp>")
    return texto_


def _cambios(ws: Path) -> str:
    """El diff que dejo el agente, antes de copiar la prueba: si la prueba falla,
    queda la evidencia para separar fallo del agente de error del evaluador."""
    g = ["git", "-C", str(ws)]
    subprocess.run(g + ["add", "-A"], capture_output=True, timeout=60)
    r = subprocess.run(g + ["diff", "--cached", "--no-color"], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=60)
    return (r.stdout or "")[:4000]


# ------------------------------------------------------------------ aislamiento

INSTRUCCIONES = ("AGENTS.md", "CLAUDE.md", "CLAUDE.local.md")


def _instrucciones_heredadas(base: Path) -> list:
    """Archivos de instrucciones que un agente cargaria por su cuenta: los de las
    carpetas sobre el workspace y los globales del usuario."""
    casa = Path.home()
    candidatos = [d / n for d in [base, *base.parents] for n in INSTRUCCIONES]
    candidatos += [casa / ".claude" / "CLAUDE.md",
                   Path(os.environ.get("CODEX_HOME") or casa / ".codex") / "AGENTS.md"]
    vistos = set()
    for c in candidatos:
        if c.is_file():
            try:
                vistos.add("~/" + c.resolve().relative_to(casa.resolve()).as_posix())
            except ValueError:
                vistos.add(c.as_posix())
    return sorted(vistos)


def marcadores(notas: list, tareas: list) -> list:
    """Lo que solo existe en el corpus: los secretos y los nombres de nota de sus
    proyectos inventados (no los genericos, como `convencion-logs`)."""
    proyectos = {t["proyecto"] for t in tareas}
    nombres = {n["name"] for n in notas
               if any(n["name"].startswith(p + "-") or n["name"] == "proj-" + p for p in proyectos)}
    return sorted({t["secreto"] for t in tareas if t.get("secreto")} | nombres)


def corpus_en_cerebro_real(marcas: list, raices=None) -> list:
    """Archivos del cerebro real que contienen algo del corpus. Si un agente los
    lee por su cuenta (archivos, hooks), la condicion sin memoria deja de serlo."""
    raices = raices if raices is not None else [rutas.WIKI, rutas.SESSIONS]
    marcas = [m.lower() for m in marcas]
    propio = (CORPUS / "wiki").resolve()
    hallados = []
    for raiz in raices:
        if not raiz or not Path(raiz).exists() or Path(raiz).resolve() == propio:
            continue
        raiz = Path(raiz)
        for f in raiz.rglob("*.md"):
            try:
                t = f.read_text(encoding="utf-8", errors="ignore").lower()
            except OSError:
                continue
            if any(m in t for m in marcas):
                hallados.append(f.relative_to(raiz.parent).as_posix())
    return sorted(hallados)


def revisar_aislamiento(notas: list, tareas: list, base: Path) -> dict:
    return {"variable": rutas.EVAL_ENV,
            "instrucciones_heredadas": _instrucciones_heredadas(base),
            "corpus_en_cerebro_real": corpus_en_cerebro_real(marcadores(notas, tareas))[:30]}


def version_de(ad) -> str:
    if isinstance(ad, AgenteEco):
        return "eco"
    try:
        r = agentes_mod._launch([ad.resolve() or ad._bin(), "--version"], timeout=30)
        return ((r.stdout or r.stderr or "").strip().splitlines() or ["?"])[0][:80]
    except Exception as e:
        return f"desconocida ({type(e).__name__})"


# ------------------------------------------------------------------ contexto

def medir_contexto(tarea: dict, paq) -> dict:
    nombres = [p.nombre for p in paq.piezas]
    mapa = vigencia.reemplazos([p.nota for p in paq.piezas])
    return {
        "esperadas": len(tarea.get("esperadas") or []),
        "esperadas_presentes": sum(1 for e in tarea.get("esperadas") or [] if e in nombres),
        "coladas": [f for f in tarea.get("fuera") or [] if f in nombres],
        "reemplazadas": [n for n in nombres if n in mapa],
        "privadas": [p.nombre for p in paq.piezas if indice.es_privada(p.nota)],
        "sin_comprobar_marcadas": sum(1 for p in paq.piezas if p.texto.startswith("[")),
        "caracteres": paq.usados,
        "notas": nombres,
    }


# ------------------------------------------------------------------ una corrida

def _ejecutar(ad, req):
    if isinstance(ad, AgenteEco):      # sin presupuesto ni trazas: no es una invocacion
        return contrato.desde_legado(ad.run(req.prompt()), agente=ad.name)
    return ejecutor.intentar(ad, req)


def _prueba(ws: Path, corpus: Path, tarea: dict) -> tuple:
    destino = ws / ("_prueba_oculta_" + tarea["prueba"])
    shutil.copy(corpus / "pruebas" / tarea["prueba"], destino)
    try:
        r = subprocess.run([sys.executable, "-m", "unittest", "-q", destino.stem], cwd=ws,
                           capture_output=True, text=True, timeout=120,
                           env=harness._entorno_validador())
        return r.returncode == 0, (r.stderr or r.stdout)[-300:]
    except subprocess.TimeoutExpired:
        return False, "la prueba oculta excedio 120 s"


def correr(ad, tarea: dict, notas: list, corpus: Path, base: Path, memoria: bool,
           run_id: str = "") -> dict:
    ws = _repo(base, corpus, tarea, f"{ad.name}-{'con' if memoria else 'sin'}")
    paq = contexto.armar_paquete(tarea["tarea"], notas, cwd=ws, presupuesto=contexto.PRESUPUESTO)
    ctx = contexto.como_prompt(paq) if memoria else ""
    fila = {"id": tarea["id"], "tipo": tarea["tipo"], "memoria": memoria,
            "caracteres_contexto": len(ctx)}
    escritura = tarea["tipo"] == "escritura"
    if escritura and not getattr(ad, "can_write", True):
        return {**fila, "estado": "no_ejecutado", "resuelta": None,
                "detalle": f"{ad.name} no escribe en disco"}
    req = contrato.TaskRequest(
        tarea=tarea["tarea"] + ("" if escritura else "\n\n" + INSTRUCCION), contexto=ctx,
        workspace=str(ws), modo="escribir" if escritura else "leer", exige_cambios=escritura,
        timeout=TIMEOUT_ESCRITURA if escritura else TIMEOUT_LECTURA, run_id=run_id,
        paso=f"{tarea['id']}/{'con' if memoria else 'sin'}", nivel="medium", operacion="eval")
    t0 = time.time()
    res = _ejecutar(ad, req)
    resp = res.respuesta or ""
    fila.update({"estado": res.estado, "codigo_error": res.codigo_error,
                 "duracion_ms": res.duracion_ms or int((time.time() - t0) * 1000),
                 "respuesta": _anonimo(resp[:800], base)})
    if escritura:
        fila["cambios"] = _anonimo(_cambios(ws), base)
        fila["resuelta"], detalle = _prueba(ws, corpus, tarea)
        fila["detalle"] = _anonimo(detalle, base)
    elif tarea["tipo"] == "fuga":
        fila["resuelta"] = None
    else:
        fila["resuelta"] = cumple_hechos(resp, tarea)
        fila["error_inducido"] = error_inducido(resp, tarea)
    if tarea.get("secreto"):
        fila["secreto_filtrado"] = _plano(tarea["secreto"]) in _plano(resp)
    if not escritura:
        # valida: una nota que recibio; inventada: un nombre que no aparece en
        # ninguna parte de lo que recibio (ni como nota ni como enlace)
        dadas = {p.nombre for p in paq.piezas} if memoria else set()
        vistas = dadas | citas(req.prompt())
        cit = citas(resp) - ({"nombre"} if isinstance(ad, AgenteEco) else set())
        fila["citas_validas"] = sorted(cit & dadas)
        fila["citas_inventadas"] = sorted(cit - vistas)
    return fila


# ------------------------------------------------------------------ reanudacion

def medir_reanudacion(base: Path) -> dict:
    """Plan de 3 pasos con un agente guionado (sin procesos ni cupo real): el
    presupuesto se corta en el paso 2; `run --resume` lo retoma. Debe terminar
    3/3, sin repetir pasos y con el mismo run_id. Todo vive en `base`."""
    repo = base / "reanudacion"
    repo.mkdir(parents=True)
    for args in (["init", "-q"], ["-c", "user.name=soou-eval", "-c", "user.email=eval@soou.invalid",
                                  "commit", "-q", "--allow-empty", "-m", "base"]):
        subprocess.run(["git", "-C", str(repo), *args], capture_output=True, timeout=60)
    hechos = []

    class Guion:
        name, can_write, restriccion_lectura = "guion", True, "texto"

        def available(self):
            return True

        def run(self, prompt, allow_write=False, cwd=None, **k):
            m = re.search(r"\((\d+)/\d+\)", prompt)
            paso = m.group(1) if m else "?"
            hechos.append(paso)
            with open(Path(cwd) / f"paso-{paso}.txt", "a", encoding="utf-8") as f:
                f.write("hecho\n")
            return {"ok": True, "output": f"paso {paso} hecho"}

    cupo = {"cortar": True, "n": 0}

    def reservar(n=1):
        cupo["n"] += 1
        return not (cupo["cortar"] and cupo["n"] == 2)

    cfg = {"routing": {}, "brain_context": False, "caps": {}, "verticals": {}}
    previo = os.getcwd()
    with ExitStack() as pila:
        for obj, nombre, kw in [
            (registro, "build_adapters", {"return_value": [Guion()]}),
            (config, "load_config", {"return_value": cfg}),
            (config, "_budget_reserve", {"side_effect": reservar}),
            (config, "_budget_left_str", {"return_value": "0"}),
            (config, "_model_for", {"return_value": None}),
            (config, "_reasoning_for", {"return_value": None}),
            (sesiones, "_runs_dir", {"return_value": base / "runs"}),
            (indice, "load_notes", {"return_value": []}),
        ]:
            pila.enter_context(mock.patch.object(obj, nombre, **kw))
        salida = pila.enter_context(open(os.devnull, "w", encoding="utf-8"))
        pila.enter_context(mock.patch("sys.stdout", salida))
        try:
            os.chdir(repo)
            harness._run_plan_execute("plan de prueba", ["paso uno", "paso dos", "paso tres"],
                                      [], cfg, 0)
        finally:
            os.chdir(previo)
        wts = sorted((base / "runs").glob("*--soou-run-*"))
        if not wts:
            return {"ok": False, "detalle": "no se creo el worktree"}
        wt = wts[-1]
        antes = json.loads((wt / ".soou-plan.json").read_text(encoding="utf-8"))
        cupo["cortar"] = False
        harness.cmd_run(mock.Mock(resume=str(wt)))
        despues = json.loads((wt / ".soou-plan.json").read_text(encoding="utf-8"))
    repetidos = [f.name for f in wt.glob("paso-*.txt")
                 if f.read_text(encoding="utf-8").count("hecho") != 1]
    ok = (antes["done"] == 1 and despues["done"] == 3 and hechos == ["1", "2", "3"]
          and not repetidos and antes.get("run_id") == despues.get("run_id")
          and all(s["status"] == "done" for s in despues["steps"]))
    return {"ok": ok, "cortado_en": antes["done"] + 1, "terminado": f"{despues['done']}/3",
            "invocaciones": hechos, "pasos_repetidos": repetidos,
            "mismo_run_id": antes.get("run_id") == despues.get("run_id")}


# ------------------------------------------------------------------ agregado

def _tasa(filas, memoria, clave="resuelta"):
    vals = [f[clave] for f in filas if f["memoria"] == memoria and f.get(clave) is not None]
    return round(sum(1 for v in vals if v) / len(vals), 2) if vals else None


def resumen(filas: list, ctx: list) -> dict:
    return {
        "resueltas_con_memoria": _tasa(filas, True),
        "resueltas_sin_memoria": _tasa(filas, False),
        "errores_inducidos_con_memoria": sum(1 for f in filas if f["memoria"] and f.get("error_inducido")),
        "errores_inducidos_sin_memoria": sum(1 for f in filas if not f["memoria"] and f.get("error_inducido")),
        "respuestas_con_evidencia": _tasa([{**f, "ev": bool(f.get("citas_validas"))}
                                           for f in filas if "citas_validas" in f], True, "ev"),
        "citas_inventadas": sum(len(f.get("citas_inventadas") or []) for f in filas),
        "secretos_filtrados": sum(1 for f in filas if f.get("secreto_filtrado")),
        "caracteres_contexto_medio": round(sum(c["caracteres"] for c in ctx) / len(ctx)) if ctx else 0,
        "no_ejecutadas": sum(1 for f in filas if f.get("estado") == "no_ejecutado"),
    }


def evaluar(agentes_=None, corpus: Path = CORPUS, reanudacion: bool = True,
            al_progresar=None, al_guardar=None) -> dict:
    """`al_guardar(informe)` se llama tras CADA corrida y al final: lo ya corrido
    queda escrito aunque el proceso muera despues."""
    notas, tareas = cargar(corpus)
    agentes_ = agentes_ or [AgenteEco()]
    reales = [a for a in agentes_ if not isinstance(a, AgenteEco)]
    base = Path(tempfile.mkdtemp(prefix="soou-eval-"))
    with rutas.modo_evaluacion():      # sin MCP, hooks ni sesiones de soou para el agente
        try:
            ctx = []
            for t in tareas:
                paq = contexto.armar_paquete(t["tarea"], notas, cwd=base / "ctx" / t["proyecto"],
                                             presupuesto=contexto.PRESUPUESTO)
                ctx.append({"id": t["id"], **medir_contexto(t, paq)})
            esperadas = sum(c["esperadas"] for c in ctx)
            informe = {
                "version": 1, "corpus": {"notas": len(notas), "tareas": len(tareas)},
                "fecha": vigencia.ahora(),
                "contexto": {
                    "esperadas_presentes": round(sum(c["esperadas_presentes"] for c in ctx)
                                                 / esperadas, 2) if esperadas else None,
                    "coladas": sum(len(c["coladas"]) for c in ctx),
                    "reemplazadas": sum(len(c["reemplazadas"]) for c in ctx),
                    "privadas": sum(len(c["privadas"]) for c in ctx),
                    "caracteres_medio": round(sum(c["caracteres"] for c in ctx) / len(ctx)),
                    "por_tarea": ctx},
                "agentes": {},
            }
            if reales:                   # leer el cerebro real solo si corre un agente real
                informe["aislamiento"] = revisar_aislamiento(notas, tareas, base)
            run_id = ejecutor.nuevo_run_id()
            for ad in agentes_:
                filas = []
                entrada = informe["agentes"][ad.name] = {
                    "version": version_de(ad), "completo": False, "resumen": None, "filas": filas}
                for t in tareas:
                    for memoria in (True, False):
                        try:
                            fila = correr(ad, t, notas, corpus, base, memoria, run_id)
                        except Exception as e:       # una tarea rota no tira la corrida entera
                            fila = {"id": t["id"], "tipo": t["tipo"], "memoria": memoria,
                                    "estado": "error_eval", "resuelta": None,
                                    "detalle": _anonimo(f"{type(e).__name__}: {e}", base)[:300]}
                        filas.append(fila)
                        entrada["resumen"] = resumen(filas, ctx)
                        if al_progresar:
                            al_progresar(ad.name, fila)
                        if al_guardar:          # lo ya corrido no se pierde si el proceso muere
                            al_guardar(informe)
                entrada["completo"] = True
            if reanudacion:
                informe["reanudacion"] = medir_reanudacion(base)
            if al_guardar:
                al_guardar(informe)
            return informe
        finally:
            _borrar(base)


# ------------------------------------------------------------------ CLI

def _fmt(v):
    return "-" if v is None else (f"{v:.2f}" if isinstance(v, float) else str(v))


def imprimir(inf: dict):
    c = inf["contexto"]
    print(f"== eval tareas · {inf['corpus']['tareas']} tareas · {inf['corpus']['notas']} notas "
          "(corpus inventado) ==")
    print(f"contexto: esperadas presentes {_fmt(c['esperadas_presentes'])} · que no debian entrar "
          f"{c['coladas']} · reemplazadas {c['reemplazadas']} · privadas {c['privadas']} · "
          f"{c['caracteres_medio']} caracteres por tarea")
    for nombre, a in inf["agentes"].items():
        r = a["resumen"]
        etiqueta = ("  (control determinista: devuelve su prompt; no mide a un agente)"
                    if nombre == "eco" else f"  ({a.get('version', '?')})")
        print(f"\n[{nombre}]" + etiqueta + ("" if a.get("completo", True) else "  INCOMPLETO"))
        print(f"  resueltas        con memoria {_fmt(r['resueltas_con_memoria'])} · "
              f"sin memoria {_fmt(r['resueltas_sin_memoria'])}")
        print(f"  error inducido   con {r['errores_inducidos_con_memoria']} · "
              f"sin {r['errores_inducidos_sin_memoria']}")
        print(f"  evidencia        {_fmt(r['respuestas_con_evidencia'])} de las respuestas con "
              f"memoria cita una nota recibida · citas inventadas {r['citas_inventadas']}")
        print(f"  secretos         {r['secretos_filtrados']} filtrado(s)"
              + (f" · {r['no_ejecutadas']} tarea(s) no ejecutadas" if r["no_ejecutadas"] else ""))
    if "aislamiento" in inf:
        z = inf["aislamiento"]
        print(f"\naislamiento: sin hooks, MCP de soou ni sesiones guardadas ({z['variable']}=1)"
              f" · instrucciones heredadas: {', '.join(z['instrucciones_heredadas']) or 'ninguna'}")
        if z["corpus_en_cerebro_real"]:
            print(f"  [!] {len(z['corpus_en_cerebro_real'])} archivo(s) de tu cerebro contienen "
                  "datos del corpus; si un agente los lee por su cuenta, 'sin memoria' deja de serlo:")
            for f in z["corpus_en_cerebro_real"][:5]:
                print("      " + f)
    if "reanudacion" in inf:
        z = inf["reanudacion"]
        print(f"\nreanudacion: {'OK' if z['ok'] else 'FALLO'} · cortado en el paso "
              f"{z.get('cortado_en', '?')}, terminado {z.get('terminado', '?')}, "
              f"pasos repetidos {len(z.get('pasos_repetidos') or [])}")


def cmd_eval_tareas(args):
    nombres = list(dict.fromkeys(getattr(args, "agente", None) or []))   # sin repetidos
    agentes_ = []
    if nombres:
        disponibles = {a.name: a for a in registro.build_adapters() if a.available()}
        faltan = [n for n in nombres if n not in disponibles]
        if faltan:
            print(f"[no] agente(s) no disponibles: {', '.join(faltan)}")
            raise SystemExit(2)
        agentes_ = [disponibles[n] for n in nombres]
        _, tareas = cargar()
        total = 2 * len(tareas) * len(agentes_)
        print(f"[eval] {total} corridas reales ({len(tareas)} tareas x con/sin memoria x "
              f"{len(agentes_)} agente(s)); gastan cupo de tus agentes.")
        if not getattr(args, "si", False):
            if not sys.stdin.isatty() or input("¿Seguir? [s/N] ").strip().lower() not in ("s", "si", "sí"):
                print("[eval] cancelado (usa --si para no preguntar).")
                return

    def progreso(nombre, f):
        if nombre != "eco":
            marca = {True: "OK", False: "no", None: "-"}[f.get("resuelta")]
            print(f"  {nombre:<12} {f['id']:<26} {'con' if f['memoria'] else 'sin'} memoria: "
                  f"{marca} ({f.get('estado')}, {round((f.get('duracion_ms') or 0) / 1000)} s)",
                  flush=True)

    def guardar(inf):
        RESULTADOS.mkdir(parents=True, exist_ok=True)
        # el nombre sale de los agentes PEDIDOS: el guardado parcial y el final son el mismo archivo
        dest = RESULTADOS / (f"{inf['fecha'][:10]}-" + "-".join(nombres or ["eco"]) + ".json")
        dest.write_bytes((json.dumps(inf, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        return dest

    con_guardar = getattr(args, "guardar", False)
    # con --guardar, lo ya corrido se escribe tras cada corrida: no se pierde si algo falla luego
    inf = evaluar(agentes_ or None, al_progresar=progreso,
                  al_guardar=guardar if con_guardar else None)
    imprimir(inf)
    if con_guardar:
        dest = guardar(inf)
        print(f"\n[ok] resultado guardado en {dest.relative_to(rutas.CODE_DIR).as_posix()}")
    if getattr(args, "json", False):
        print(json.dumps(inf, ensure_ascii=False))
