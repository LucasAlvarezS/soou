"""Modulo harness de soou (extraido de soou.py).

soou.py lo re-exporta; dentro de soou_core se usa como `harness.nombre`."""

from pathlib import Path
import concurrent.futures
import datetime as dt
import difflib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from soou_core import (agentes, compactar, config, contexto, contrato, ejecutor, indice, registro,
                       router, rutas, saneado, sesiones, vigencia)


def _brain_context(prompt: str, cwd=None) -> str:
    """Contexto del cerebro para el prompt de un agente: el paquete unico de
    soou_core/contexto.py (resumen del proyecto, notas pertinentes, sin
    privadas ni reemplazadas, dentro del presupuesto)."""
    paq = contexto.armar_paquete(prompt, cwd=cwd or os.getcwd(),
                                 presupuesto=contexto.presupuesto())
    return contexto.como_prompt(paq)


def _parse_json_list(text: str) -> list[str]:
    """Extrae el primer array JSON de strings presente en un texto."""
    match = re.search(r"\[.*?\]", text, re.DOTALL)
    if not match:
        return []
    try:
        value = json.loads(match.group(0))
    except (json.JSONDecodeError, TypeError):
        return []
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    return []


def _plan_task(task: str, notes) -> list[str]:
    """Descompone una tarea compleja en subtareas ordenadas sin ejecutarlas."""
    # pasa por el ejecutor como toda llamada a un agente: presupuesto, traza,
    # verificacion minima y fallback; en lectura, solo agentes con lectura segura.
    lista = ejecutor.candidatos("complex", "leer")
    if not lista:
        return [task]
    req = contrato.TaskRequest(
        tarea=("Descompone la tarea en 2 a 5 subtareas ORDENADAS, concretas y accionables.\n"
               "Devuelve SOLO un array JSON de strings, sin texto extra.\n\n"
               f"Tarea: {task}"),
        contexto=_brain_context(task), modo="leer", nivel="complex",
        run_id=ejecutor.nuevo_run_id(), paso="plan", operacion="plan", timeout=600)
    res, _ = ejecutor.ejecutar_con_fallback(req, lista)
    subs = _parse_json_list(res.respuesta) if res else []
    return subs[:5] if subs else [task]


def _git_root(path="."):
    try:
        r = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


def _make_worktree(root):
    """Crea un git worktree en una rama nueva. Devuelve (path, branch, error)."""
    branch = "soou/run-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    base = sesiones._runs_dir()
    base.mkdir(parents=True, exist_ok=True)
    wt = base / (Path(root).name + "--" + branch.replace("/", "-"))
    r = subprocess.run(["git", "-C", root, "worktree", "add", "-b", branch, str(wt)],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        return None, None, r.stderr.strip()
    return str(wt), branch, None


def _run_validators(cwd, base=None):
    """Validadores detectables sobre el worktree. Devuelve [(nombre, ok, resumen)]
    con ok = True (aprobado), False (fallo) o None (no se pudo ejecutar)."""
    checks = []
    try:
        # con checkpoints por paso el trabajo ya esta commiteado: se compara contra
        # la base del run (si se conoce) ademas de lo que quede sin commitear.
        rango = [base + "..HEAD"] if base else []
        d = subprocess.run(["git", "-C", cwd, "diff", "--stat", *rango],
                           capture_output=True, text=True, timeout=20)
        w = subprocess.run(["git", "-C", cwd, "diff", "--stat"],
                           capture_output=True, text=True, timeout=20) if base else None
        resumen = (d.stdout.strip() + ("\n" + w.stdout.strip() if w and w.stdout.strip() else ""))
        checks.append(("git diff", True, (resumen.strip() or "sin cambios")[:400]))
    except Exception:
        pass
    p = Path(cwd)
    if any(p.glob("*.py")) or (p / "pyproject.toml").exists() or (p / "setup.py").exists():
        try:
            c = subprocess.run([sys.executable, "-m", "compileall", "-q", "-x",
                                r"(node_modules|\.git|\.venv|venv)", "."], cwd=cwd,
                               capture_output=True, text=True, timeout=120)
            checks.append(("py compile", c.returncode == 0,
                           ((c.stdout + c.stderr).strip() or "ok")[:300]))
        except Exception as e:
            checks.append(("py compile", False, str(e)[:200]))
    for nombre, argv, motivo in _detectar_validadores(cwd):
        if argv is None:                       # detectado pero no ejecutable aqui
            checks.append((nombre, None, "no ejecutado: " + motivo))   # None != aprobado
            continue
        try:
            r = subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                               timeout=VALIDATOR_TIMEOUT, encoding="utf-8", errors="replace",
                               env=_entorno_validador())
            salida = (r.stdout + "\n" + r.stderr).strip().splitlines()
            checks.append((nombre, r.returncode == 0,
                           (" | ".join(l.strip() for l in salida[-4:] if l.strip()) or "ok")[:400]))
        except subprocess.TimeoutExpired:
            checks.append((nombre, False, f"timeout ({VALIDATOR_TIMEOUT}s)"))
        except Exception as e:
            checks.append((nombre, False, str(e)[:200]))
    return checks


VALIDATOR_TIMEOUT = 900


def _entorno_validador() -> dict:
    """Entorno de los tests del proyecto: CI=1 (sin prompts ni watchers) y SIN las
    variables internas de soou, que cambian el comportamiento del codigo bajo prueba
    (BRAIN_DISTILL desactiva la captura y hacia fallar en falso a sus tests)."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("BRAIN_DISTILL", "SOOU_DEPTH", "SOOU_HOME")}
    env["CI"] = "1"
    return env


def _detectar_validadores(cwd) -> list:
    """Tests/lint REALES del proyecto: [(nombre, argv | None, motivo)].

    Orden: lo que declare el repo en .soou.json {"validators": ["cmd", ...]} manda;
    si no hay, se autodetecta (npm, pytest/unittest, cargo, go, make). Un comando
    que no se puede correr aca (sin node_modules, sin la herramienta) se reporta
    como omitido en vez de fallar: el worktree no trae dependencias instaladas."""
    p = Path(cwd)
    decl = p / ".soou.json"
    if decl.exists():
        try:
            cmds = json.loads(decl.read_text(encoding="utf-8")).get("validators") or []
            return [(c, shlex.split(c, posix=os.name != "nt"), "") for c in cmds if c.strip()]
        except Exception as e:
            return [(".soou.json", None, f"ilegible ({e})")]
    out = []
    pkg = p / "package.json"
    if pkg.exists():
        try:
            scripts = json.loads(pkg.read_text(encoding="utf-8")).get("scripts") or {}
        except Exception:
            scripts = {}
        npm = shutil.which("npm")
        for s in ("lint", "typecheck", "test"):
            cmd = scripts.get(s, "")
            if not cmd or "no test specified" in cmd:
                continue
            if not npm:
                out.append((f"npm {s}", None, "npm no esta en el PATH"))
            elif not (p / "node_modules").exists():
                out.append((f"npm {s}", None, "sin node_modules en el worktree (npm ci)"))
            else:
                out.append((f"npm {s}", [npm, "run", s, "--silent"], ""))
    tests_dir = next((d for d in ("tests", "test") if (p / d).is_dir()), None)
    py_tests = tests_dir and any((p / tests_dir).rglob("test_*.py"))
    if py_tests or (p / "pytest.ini").exists():
        try:
            import importlib.util as _ilu
            con_pytest = _ilu.find_spec("pytest") is not None
        except Exception:
            con_pytest = False
        if con_pytest:
            out.append(("pytest", [sys.executable, "-m", "pytest", "-q", "-x"], ""))
        elif tests_dir:
            out.append(("unittest", [sys.executable, "-m", "unittest", "discover",
                                     "-s", tests_dir, "-p", "test_*.py"], ""))
    if (p / "Cargo.toml").exists():
        c = shutil.which("cargo")
        out.append(("cargo test", [c, "test", "--quiet"] if c else None, "cargo no esta en el PATH"))
    if (p / "go.mod").exists():
        g = shutil.which("go")
        out.append(("go test", [g, "test", "./..."] if g else None, "go no esta en el PATH"))
    mk = p / "Makefile"
    if not out and mk.exists() and re.search(r"^test:", mk.read_text(errors="ignore"), re.M):
        m = shutil.which("make")
        out.append(("make test", [m, "test"] if m else None, "make no esta en el PATH"))
    return out


def _capture_run(task: str, agent: str, output: str, verificado: str | None = None,
                 run_id: str = "", veredicto: str | None = None):
    """CAPTURA UNIVERSAL desde el Runner: guarda el resultado de una tarea orquestada
    (por CUALQUIER IA) como CANDIDATO a memoria, para que Nivel A/B lo destilen.

    Solo llega aqui un resultado que cumplio el minimo; los intentos fallidos se
    quedan en el diario de ejecucion (runs/trazas.jsonl). La procedencia (run y
    agente) y la verificacion real viajan en la cabecera: "el agente dijo que
    termino" y "los validadores lo comprobaron" no significan lo mismo."""
    if os.environ.get("BRAIN_DISTILL"):
        return
    out = (output or "").strip()
    if len(out) < 200:                 # nada sustancioso que destilar
        return
    if run_id and rutas.SESSIONS.exists() and \
            next(rutas.SESSIONS.rglob(f"session-{run_id}.md"), None) is not None:
        return                         # ya capturado (p.ej. un plan reanudado)
    now = dt.datetime.now()
    dest_dir = rutas.SESSIONS / now.strftime("%Y/%m/%d")
    dest_dir.mkdir(parents=True, exist_ok=True)
    nombre = run_id or f"run-{now.strftime('%Y%m%d-%H%M%S')}"
    dest = dest_dir / f"session-{nombre}.md"
    # sin validar = respuesta del agente que nadie comprobo (p.ej. una pregunta de
    # lectura): en uso real una respuesta asi resulto inventada.
    validado = verificado or "NO (respuesta del agente sin comprobar; tratar como no verificada)"
    header = (f"<!-- distilled: no -->\n# Sesion run {now.strftime('%Y-%m-%d %H:%M')}\n"
              f"Fuente: soou run ({agent})\n"
              + (f"Run: {run_id}\n" if run_id else "")
              + f"Verificado: {validado}\n"
              # legible por el destilado (vigencia.de_cabecera): solo una prueba
              # funcional aprobada es tests_passed
              + f"Verificacion: {vigencia.de_veredicto(veredicto)}\n\n")
    body = f"**user:** {saneado._limpiar_texto(task)[:2000]}\n\n**assistant:** {saneado._limpiar_texto(out)[:4000]}"
    dest.write_text(header + body, encoding="utf-8")
    print(f"[run] capturado al cerebro: {dest.relative_to(rutas.ROOT).as_posix()}")


_FUNCIONAL = re.compile(r"\b(tests?|pytest|unittest|check|spec|e2e)\b", re.I)


def _clase_validador(nombre: str) -> str:
    """basica: el diff y que compile; estatica: lint y tipos; funcional: pruebas.
    Solo una prueba funcional aprobada demuestra que la tarea funciona."""
    if nombre in ("git diff", "py compile"):
        return "basica"
    if nombre in ("npm lint", "npm typecheck"):
        return "estatica"
    return "funcional" if _FUNCIONAL.search(nombre) else "estatica"


def _verificacion_de_validadores(resultados) -> tuple:
    """(veredicto, texto). veredicto: "funcional" (al menos una prueba funcional
    aprobada y ningun fallo), "fallo" (algo fallo) o "sin_verificacion_funcional"
    (solo comprobaciones basicas/estaticas, o nada se pudo ejecutar). Cada
    comprobacion aprobada se registra como tal; lo no ejecutado, como no ejecutado."""
    aprob = [n for n, ok, _ in resultados if ok is True]
    malos = [n for n, ok, _ in resultados if ok is False]
    sin_correr = [n for n, ok, _ in resultados if ok is None]
    partes = []
    if aprob:
        partes.append("aprobadas: " + ", ".join(aprob))
    if sin_correr:
        partes.append("no ejecutadas: " + ", ".join(sin_correr))
    detalle = "; ".join(partes)
    if malos:
        return "fallo", "no (fallaron: " + ", ".join(malos) + (("; " + detalle) if detalle else "") + ")"
    if any(_clase_validador(n) == "funcional" for n in aprob):
        return "funcional", "si (" + detalle + ")"
    if not resultados:
        return "sin_verificacion_funcional", "no (no hay validadores para este repositorio)"
    return "sin_verificacion_funcional", "parcial: sin verificacion funcional (" + detalle + ")"


def _imprimir_validadores(resultados):
    for name, vok, brief in resultados:
        marca = {True: "OK   ", False: "FALLO", None: "NO EJ"}[vok]
        print(f"  {marca} {name} [{_clase_validador(name)}]: {brief}")


_AVISO_VEREDICTO = {
    "funcional": "validación funcional OK",
    "fallo": "OJO: validación con fallos",
    "sin_verificacion_funcional": "sin verificación funcional: ninguna prueba comprobó "
                                  "que funcione, revísalo a mano",
}


def _checkpoint(wt_path, mensaje: str) -> bool:
    """Commit en la rama del run tras un paso exitoso: reanudar parte del ultimo
    verde y el diff de cada paso queda revisable. Nunca toca la rama principal."""
    try:
        subprocess.run(["git", "-C", wt_path, "add", "-A", "--", ".", ":(exclude).soou-plan.json"],
                       capture_output=True, timeout=60)
        r = subprocess.run(["git", "-C", wt_path, "commit", "-q", "-m", mensaje],
                           capture_output=True, text=True, timeout=60)
        return r.returncode == 0
    except Exception:
        return False


def _write_plan_state(wt_path, state):
    """Persiste el progreso reanudable del Planner sin interrumpir la ejecucion."""
    try:
        (Path(wt_path) / ".soou-plan.json").write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _run_plan_execute(prompt, subs, notes, cfg, depth, wt=None, start=0):
    """Ejecuta el plan (fan-out multiagente): corre cada subtarea en UN worktree
    COMPARTIDO, cada una con el agente que le toca por cost-routing (clasifica la
    subtarea → nivel → orden de preferencia). NO hace auto-merge: deja el trabajo en
    la rama del worktree y sugiere aceptar/descartar. Devuelve None."""
    if wt is None:
        root = _git_root(".")
        if not root:
            print("[plan] el cwd no es repo git; aborto.")
            return
        wt_path, branch, err = _make_worktree(root)
        if not wt_path:
            print(f"[plan] no se pudo crear worktree ({err}); aborto.")
            return
    else:
        root, wt_path, branch = wt

    if wt is None:
        steps = [{"sub": sub, "agent": None, "model": None,
                  "status": "pending"} for sub in subs]
        base = subprocess.run(["git", "-C", wt_path, "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip() or None
        state = {"prompt": prompt, "subs": subs, "done": start,
                 "branch": branch, "root": root, "wt_path": wt_path,
                 "base": base, "steps": steps}
        _write_plan_state(wt_path, state)
    else:
        state = {"prompt": prompt, "subs": subs, "done": start,
                 "branch": branch, "root": root, "wt_path": wt_path}
        try:
            existing = json.loads(
                (Path(wt_path) / ".soou-plan.json").read_text(encoding="utf-8"))
            steps = existing.get("steps")
            if not isinstance(steps, list) or len(steps) != len(subs):
                steps = [{"sub": sub, "agent": None, "model": None,
                          "status": "done" if i < start else "pending"}
                         for i, sub in enumerate(subs)]
            state.update(existing)
            state.update({"prompt": prompt, "subs": subs, "done": start,
                          "branch": branch, "root": root, "wt_path": wt_path,
                          "steps": steps})
        except Exception:
            state["steps"] = [{"sub": sub, "agent": None, "model": None,
                                "status": "done" if i < start else "pending"}
                               for i, sub in enumerate(subs)]
        _write_plan_state(wt_path, state)
    print(f"[plan] ejecutando {len(subs)} subtareas en worktree aparte (rama {branch})")

    run_id = state.get("run_id") or ejecutor.nuevo_run_id()
    state["run_id"] = run_id
    _write_plan_state(wt_path, state)
    ok_all = True
    done = start
    for idx in range(start, len(subs)):
        sub = subs[idx]
        i = idx + 1
        # cost-routing por SUBTAREA: la reclasifico para elegir el agente correcto
        fused, _ = indice.rank_notes(sub, notes)
        tier, reason, strat = router.classify_task(sub, notes, fused)
        sublevel, _ = router.flow_for(tier, reason)
        lista = ejecutor.candidatos(sublevel, "escribir", cfg)
        if not lista:
            print("[plan] sin agente de escritura.")
            ok_all = False
            break
        ctx = _brain_context(sub) if cfg.get("brain_context", True) else ""
        # una subtarea puede ser de lectura ("revisar X"): no se le exigen cambios,
        # pero el minimo (respuesta, sin "no pude") sigue valiendo.
        req = contrato.TaskRequest(tarea=f"Subtarea ({i}/{len(subs)}): {sub}", contexto=ctx,
                                   workspace=wt_path, modo="escribir", exige_cambios=False,
                                   run_id=run_id, paso=f"{i}/{len(subs)}", nivel=sublevel)
        state["steps"][idx]["status"] = "running"

        def empezar(ad, r, idx=idx, i=i, sub=sub, sublevel=sublevel):
            modelo = config._model_for(ad.name, sublevel)
            reasoning = config._reasoning_for(ad.name, sublevel)
            state["steps"][idx]["agent"] = ad.name
            state["steps"][idx]["model"] = modelo
            _write_plan_state(wt_path, state)
            print(f"[plan] {i}/{len(subs)} -> {ad.name}{_etiquetas(modelo, reasoning)}: {sub}")

        def fallar(ad, r):
            detalle = " ".join(r.respuesta.split())[:200]
            print(f"[plan]    {ad.name} no sirvio: {r.motivo} ({detalle}); pruebo el siguiente")

        res, intentos = ejecutor.ejecutar_con_fallback(req, lista, empezar, fallar)
        if res is None:
            motivo = intentos[-1].motivo if intentos else "sin intentos"
            if intentos and intentos[-1].codigo_error == contrato.E_PRESUPUESTO:
                print("[budget] limite diario alcanzado (" + config._budget_left_str()
                      + "); se detiene. Reintenta manana o sube budget_per_day.")
                if len(intentos) == 1:       # ni se intento: el paso sigue pendiente
                    state["steps"][idx]["status"] = "pending"
                    _write_plan_state(wt_path, state)
                    ok_all = False
                    break
            print(f"[plan] subtarea {i} fallo con todos los agentes ({motivo}).")
            state["steps"][idx]["status"] = "failed"
            _write_plan_state(wt_path, state)
            ok_all = False
            break
        if _checkpoint(wt_path, f"soou paso {i}/{len(subs)}: {sub[:60]}"):
            state["checkpoints"] = state.get("checkpoints", 0) + 1
        state["steps"][idx]["status"] = "done"
        done += 1
        state["done"] = done
        _write_plan_state(wt_path, state)
        extract = " ".join(res.respuesta.split())[:400]
        if extract:
            print(f"[plan]    {extract}")

    print("\n[validación]")
    resultados = list(_run_validators(wt_path, state.get("base")))
    _imprimir_validadores(resultados)
    veredicto, texto = _verificacion_de_validadores(resultados)
    state["validacion"] = {"funcional": "ok"}.get(veredicto, veredicto)
    _write_plan_state(wt_path, state)
    ejecutor._traza({"gen_ai.operation.name": "validate", "soou.branch": branch,
                     "soou.run_id": run_id, "soou.status": state["validacion"],
                     "soou.reason": texto})
    if veredicto == "fallo":
        print("  ⚠️ la validacion FALLO: revisa la rama antes de aceptar el merge.")
    elif veredicto == "sin_verificacion_funcional":
        print("  ⚠️ " + _AVISO_VEREDICTO[veredicto] + ".")

    if ok_all:
        subprocess.run(["git", "-C", wt_path, "add", "-A", "--", ".", ":(exclude).soou-plan.json"],
                       capture_output=True)
        cm = subprocess.run(["git", "-C", wt_path, "commit", "-q", "-m",
                             f"soou plan: {prompt[:70]}"], capture_output=True, text=True)
        if cm.returncode == 0 or state.get("checkpoints"):
            print(f"\n[plan] {len(subs)} subtareas completas en rama {branch}")
            print(f"  aceptar  : git -C \"{root}\" merge {branch} && git -C \"{root}\" worktree remove \"{wt_path}\"")
            print(f"  descartar: git -C \"{root}\" worktree remove --force \"{wt_path}\" && git -C \"{root}\" branch -D {branch}")
        else:
            print("\n[plan] las subtareas no dejaron cambios que commitear (nada que mergear).")
            print(f"  descartar: git -C \"{root}\" worktree remove --force \"{wt_path}\" && git -C \"{root}\" branch -D {branch}")
    else:
        print(f"\n[plan] {done}/{len(subs)} subtareas completadas; worktree conservado para inspección.")
        print(f"  descartar: git -C \"{root}\" worktree remove --force \"{wt_path}\" && git -C \"{root}\" branch -D {branch}")
    return


def _etiquetas(modelo, reasoning) -> str:
    return ((f" (modelo {modelo})" if modelo else "")
            + (f" (effort {reasoning})" if reasoning is not None else ""))


def cmd_run(args):
    if getattr(args, "resume", None):
        wt_path = args.resume
        state_path = Path(wt_path) / ".soou-plan.json"
        if not state_path.exists():
            print("[resume] no hay estado de plan en " + wt_path)
            return
        state = json.loads(state_path.read_text(encoding="utf-8"))
        pending = state["subs"][state["done"]:]
        if not pending:
            print("[resume] plan ya completo (" + str(state["done"]) + "/"
                  + str(len(state["subs"])) + ").")
            return
        print("[resume] reanudando " + str(len(pending))
              + " subtareas pendientes (de " + str(len(state["subs"]))
              + ") en rama " + state["branch"])
        _run_plan_execute(
            state["prompt"], state["subs"], indice.load_notes(), config.load_config(),
            int(os.environ.get("SOOU_DEPTH", "0")),
            wt=(state["root"], state["wt_path"], state["branch"]),
            start=state["done"])
        return

    if not args.task:
        print("[run] falta una tarea (o usa --resume WORKTREE).")
        return

    prompt = " ".join(args.task)
    notes = indice.load_notes()
    fused, _ = indice.rank_notes(prompt, notes)
    tier, reason, strat = router.classify_task(prompt, notes, fused)
    level, flow = router.flow_for(tier, reason)
    cfg = config.load_config()
    use_brain = (level != "trivial" and cfg.get("brain_context", True)
                 and not getattr(args, "no_brain", False))
    allow_write = getattr(args, "write", False)   # seguridad: escribir solo si se pide explícito
    modo = "escribir" if allow_write else "leer"
    caps = cfg["caps"]
    avail = ejecutor.candidatos(level, modo, cfg)
    # dual-agent: nivel ambiguo/alto-riesgo + perfil con parallel_agents + ≥2 agentes.
    # Verificación cruzada (lectura/plan); no se combina con escritura por seguridad.
    dual = (level == "ambiguous/high-risk" and caps.get("parallel_agents")
            and len(avail) >= 2 and not allow_write)

    print(f"[run] perfil: {cfg['profile']}  ·  nivel: {level}  ·  flujo: {flow}")
    pref = avail[0].name if avail else "NINGUNO"
    pref_model = config._model_for(pref, level) if avail else None
    pref_label = f" (modelo {pref_model})" if pref_model else ""
    print(f"[run] routing (eje costo): preferido → {pref}{pref_label}  ·  fallback → {[a.name for a in avail[1:]] or '—'}")
    if getattr(args, "plan", False):
        # --plan EXPLÍCITO: el usuario lo pidió → descomponer siempre (el gate por nivel
        # vive en el modo AUTOMÁTICO, brain_orchestrate). Si sale 1 subtarea, era simple.
        # SOOU_DEPTH acota la recursión: los agentes hijos no re-planifican al infinito.
        depth = int(os.environ.get("SOOU_DEPTH", "0"))
        if depth >= 2:
            print("[plan] limite de profundidad; ejecuto plano sin descomponer")
        else:
            subs = _plan_task(prompt, notes)
            if len(subs) <= 1:
                print("[plan] tarea atómica; no se descompone (1 subtarea).")
            else:
                print(f"[plan] descomposicion en {len(subs)} subtareas:")
                for i, subtask in enumerate(subs, 1):
                    print(f"  {i}. {subtask}")
            if not getattr(args, "write", False):
                return   # solo-descomposición
            os.environ["SOOU_DEPTH"] = str(depth + 1)   # los hijos heredan profundidad+1
            _run_plan_execute(prompt, subs, notes, cfg, depth)
            return
    if getattr(args, "dry", False):
        print(f"[dry] inyectaría contexto del cerebro: {use_brain} · escritura: {allow_write}")
        if dual:
            print(f"[dry] DUAL-AGENT: {avail[0].name} + {avail[1].name} en paralelo + verificación cruzada")
        if allow_write:
            root = _git_root(".")
            print(f"[dry] escritura en git worktree aparte (rama soou/run-*) de: {root or 'NO es repo git → se abortaría'}")
        if use_brain:
            print("[dry] contexto:\n" + (_brain_context(prompt) or "  (sin coincidencias)"))
        return
    if not avail:
        print("[error] no hay agentes disponibles que puedan " + ("escribir." if allow_write
              else "leer con una restriccion verificada (mira 'soou agents')."))
        return

    ctx = _brain_context(prompt) if use_brain else ""
    run_id = ejecutor.nuevo_run_id()
    print(f"[run] id {run_id}  ·  detalle: soou trace {run_id}")
    req = contrato.TaskRequest(tarea=prompt, contexto=ctx, modo=modo, run_id=run_id,
                               nivel=level)

    if dual:
        # 2 agentes resuelven la MISMA tarea → se contrastan (patrón dual-planner).
        print(f"[run] DUAL-AGENT ({avail[0].name} + {avail[1].name}) · verificación cruzada\n")
        pares = list(enumerate(avail[:2]))
        for _, ad in pares:
            print(f"[run] → {ad.name}{_etiquetas(config._model_for(ad.name, level), config._reasoning_for(ad.name, level))}…")
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda p: ejecutor.intentar(p[1], req, intento=p[0]), pares))
        for r in results:
            print(f"\n───── {r.agente} ({'ok' if r.cumple('minimo') else 'falló: ' + r.motivo}) ─────")
            print(r.respuesta[:1800])
        oks = [r for r in results if r.cumple("minimo")]
        print("\n[verificación] " + (
            f"{len(oks)}/2 agentes respondieron ({', '.join(r.agente for r in oks)}); "
            "contrasta ambas respuestas antes de decidir." if oks
            else "ningún agente completó."))
        if len(oks) == 2:
            normalized = [re.sub(r"\s+", " ", r.respuesta.lower()).strip() for r in oks]
            words = [set(re.findall(r"\w+", text)) for text in normalized]
            smaller, larger = sorted(words, key=len)
            contained = bool(smaller) and len(smaller & larger) / len(smaller) > 0.6
            similar = difflib.SequenceMatcher(None, *normalized).ratio() > 0.6
            print("[juez] alto acuerdo entre agentes" if (contained or similar)
                  else "[juez] discrepan — revisa ambas")
        for r in oks:                          # captura universal (ambas respuestas)
            _capture_run(prompt, r.agente, r.respuesta, run_id=f"{run_id}-{r.agente}")
        return

    # escritura en un git worktree aparte: el repo no se toca mid-run
    wt = None
    if allow_write:
        root = _git_root(".")
        if not root:
            print("[aviso] el cwd no es repo git; sin worktree aparte → aborto la escritura.")
            return
        wt_path, branch, err = _make_worktree(root)
        if not wt_path:
            print(f"[aviso] no se pudo crear worktree ({err}); aborto por seguridad.")
            return
        req.workspace, wt = wt_path, (root, wt_path, branch)
        print(f"[run] worktree aparte: rama {branch}")

    def empezar(ad, r):
        print(f"[run] → {ad.name}{_etiquetas(config._model_for(ad.name, level), config._reasoning_for(ad.name, level))} (write={allow_write})…")

    def fallar(ad, r):
        if r.codigo_error == contrato.E_PRESUPUESTO:
            print("[budget] limite diario alcanzado (" + config._budget_left_str()
                  + "); se detiene. Reintenta manana o sube budget_per_day.")
            return
        detalle = " ".join(r.respuesta.split())[:200] or "(sin salida)"
        print(f"[aviso] {ad.name} no sirvio: {r.motivo} ({detalle}); fallback al siguiente…")

    res, _intentos = ejecutor.ejecutar_con_fallback(req, avail, empezar, fallar)
    ok = res is not None
    if ok:
        print(res.respuesta[:2000])

    if not wt:
        # una respuesta de solo lectura no pasa por validadores: queda marcada asi
        # en el cerebro para que el destilado no la trate como hecho comprobado.
        if ok:
            _capture_run(prompt, res.agente, res.respuesta, run_id=run_id)
        else:
            print("[error] todos los agentes fallaron.")
        return

    root, wt_path, branch = wt
    if not ok:
        subprocess.run(["git", "-C", root, "worktree", "remove", "--force", wt_path], capture_output=True)
        subprocess.run(["git", "-C", root, "branch", "-D", branch], capture_output=True)
        print("[run] worktree descartado (el agente no completó).")
        print("[error] todos los agentes fallaron.")
        return
    print("\n[validación]")
    resultados = list(_run_validators(wt_path))
    _imprimir_validadores(resultados)
    veredicto, verificado = _verificacion_de_validadores(resultados)
    ejecutor._traza({"gen_ai.operation.name": "validate", "soou.branch": branch,
                     "soou.run_id": run_id, "soou.status": veredicto, "soou.reason": verificado})
    _capture_run(prompt, res.agente, res.respuesta, verificado=verificado, run_id=run_id,
                 veredicto=veredicto)
    # COMMIT en la rama del worktree: si no, la rama queda == base y el
    # `git merge` sugerido no traería NADA (el trabajo quedaría varado).
    subprocess.run(["git", "-C", wt_path, "add", "-A", "--", ".", ":(exclude).soou-plan.json"],
                   capture_output=True)
    cm = subprocess.run(["git", "-C", wt_path, "commit", "-q", "-m",
                         f"soou run: {prompt[:70]}"], capture_output=True, text=True)
    if cm.returncode != 0:
        print("\n[worktree] el agente no dejó cambios que commitear (nada que mergear).")
        subprocess.run(["git", "-C", root, "worktree", "remove", "--force", wt_path], capture_output=True)
        subprocess.run(["git", "-C", root, "branch", "-D", branch], capture_output=True)
        return
    print(f"\n[worktree] commit en rama '{branch}' — {_AVISO_VEREDICTO[veredicto]}.")
    print(f"  aceptar  : git -C \"{root}\" merge {branch} && git -C \"{root}\" worktree remove \"{wt_path}\"")
    print(f"  descartar: git -C \"{root}\" worktree remove --force \"{wt_path}\" && git -C \"{root}\" branch -D {branch}")


def _launch_plan_detached(task):
    """Lanza un plan con escritura como proceso independiente en segundo plano."""
    return ejecutor.lanzar_desacoplado(["run", "--plan", "--write", task])
