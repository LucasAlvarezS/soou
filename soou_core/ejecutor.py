"""Ejecutor unico: TODA llamada a un agente pasa por aqui.

Antes habia tres bucles de intento (escritura en `run`, pasos del plan y modo
dual), cada uno con su copia de presupuesto, modelo, traza, criterio de utilidad
y limpieza. Ahora:

    candidatos()          -> que agentes pueden cumplir el modo, en que orden
    intentar()            -> un intento: reserva, checkpoint, ejecuta, verifica,
                             traza y, si fallo, vuelve exactamente al checkpoint
    ejecutar_con_fallback -> intentos en orden hasta el primero que cumple

Capa alta, debajo de harness. El diario de ejecucion (trazas, intentos fallidos)
vive en runs/; a la memoria solo llega lo que el harness decide capturar."""

import datetime as dt
import json
import os
import re
import secrets
import subprocess
import tempfile
import time
from pathlib import Path
from soou_core import (agentes, compactar, config, contrato, destilado, registro, rutas,
                       sesiones)


# --------------------------------------------------------------- trazas e historial

def _traza(evento: dict):
    """Una linea por intento en runs/trazas.jsonl. Atributos con nombres de las
    convenciones OpenTelemetry GenAI (gen_ai.*), que siguen en Development: por
    eso se versiona el esquema. Acotado por _rotar_logs (compact)."""
    try:
        f = sesiones._runs_dir() / "trazas.jsonl"
        f.parent.mkdir(parents=True, exist_ok=True)
        fila = {"schema": "soou.trace/2", "ts": dt.datetime.now().isoformat(timespec="seconds"),
                **evento}
        linea = json.dumps(fila, ensure_ascii=False) + "\n"
        with f.open("a", encoding="utf-8") as out:      # una linea entera por write
            out.write(linea)
    except Exception:
        pass


TRAZA_VIDA_MEDIA_DIAS = 14      # peso de un resultado: se divide a la mitad cada 14 dias
TRAZA_MIN_MUESTRAS = 3.0        # bajo esto no hay evidencia suficiente para reordenar


def _historial_agentes(nivel: str, ahora: float | None = None) -> dict:
    """{agente: (exitos_ponderados, total_ponderado)} para un nivel, con decaimiento.
    Un rechazo (el agente no podia cumplir el modo) no cuenta como fallo suyo."""
    f = sesiones._runs_dir() / "trazas.jsonl"
    if not f.exists():
        return {}
    ahora = ahora or time.time()
    stats = {}
    try:
        lineas = f.read_text(encoding="utf-8").splitlines()[-compactar.LOG_MAX_LINEAS:]
    except OSError:
        return {}
    for l in lineas:
        try:
            r = json.loads(l)
        except Exception:
            continue
        if r.get("gen_ai.operation.name") != "execute_step" or r.get("soou.level") != nivel:
            continue
        if r.get("soou.estado") in ("rechazado", "cancelado"):
            continue
        agente = r.get("gen_ai.agent.name")
        try:
            edad = (ahora - dt.datetime.fromisoformat(r["ts"]).timestamp()) / 86400
        except Exception:
            continue
        peso = 0.5 ** (max(edad, 0) / TRAZA_VIDA_MEDIA_DIAS)
        ok, tot = stats.get(agente, (0.0, 0.0))
        stats[agente] = (ok + (peso if r.get("soou.status") == "ok" else 0.0), tot + peso)
    return stats


def _ordenar_por_historial(agentes_: list, nivel: str) -> list:
    """Learned routing conservador: respeta el orden configurado, pero manda al
    final a un agente que, con evidencia suficiente, falla mas de lo que acierta
    en este nivel. Estable: sin historial no cambia nada."""
    hist = _historial_agentes(nivel)

    def malo(a):
        ok, tot = hist.get(a.name, (0.0, 0.0))
        return tot >= TRAZA_MIN_MUESTRAS and ok / tot < 0.5
    return sorted(agentes_, key=malo)


# --------------------------------------------------------------- utilidad de un resultado

# Un agente puede terminar con codigo 0 y aun asi NO haber hecho nada: lo vimos en
# uso real (Codex sin terminal: "No pude aplicar el cambio... helper_unknown_error")
# y el harness lo daba por exito, sin probar el fallback.
_NO_PUDO = re.compile(
    r"\b(no pude|no fue posible|no se pudo (completar|aplicar|ejecutar|leer|editar|modificar)|"
    r"no se modificaron archivos|no puedo (leer|editar|acceder|ejecutar)|"
    r"i (couldn'?t|could not|was unable to|cannot)|unable to (complete|apply|run|access|read|edit)|"
    r"not able to (complete|access|run)|helper_unknown_error|permission denied)\b", re.I)


def _estado_git(cwd) -> str:
    """Huella del arbol: HEAD + cambios sin commitear, incluidos los ignorados
    (sin el estado del plan). Es una senal, no una garantia: no ve nada fuera
    del worktree."""
    if not cwd:
        return ""
    try:
        head = subprocess.run(["git", "-C", str(cwd), "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=20).stdout.strip()
        st = subprocess.run(["git", "-C", str(cwd), "status", "--porcelain", "--ignored",
                             "--", ".", ":(exclude).soou-plan.json"],
                            capture_output=True, text=True, timeout=20).stdout
        # un archivo que ya figuraba modificado y se vuelve a modificar no cambia
        # la linea de status: el hash del contenido del arbol si cambia.
        arbol = _arbol_de_trabajo(cwd) or ""
        return head + "\n" + st + "\n" + arbol
    except Exception:
        return ""


def _resultado_util(res: dict, escritura: bool, cwd=None, antes: str = "") -> tuple:
    """(util, motivo) con la politica minima, para una salida de adaptador.
    `escritura` = se exigian cambios (firma historica); los permisos de lectura
    los comprueba `intentar`, que sabe el modo real."""
    r = verificar_minimo(contrato.desde_legado(res), escritura, cwd, antes, True)
    return r.cumple("minimo"), r.motivo


def verificar_minimo(res, requiere_cambios: bool, cwd, antes: str, escritura: bool):
    """Agrega al resultado las verificaciones del minimo, que vale en TODOS los
    niveles: el proceso termino bien, hay respuesta o cambios, el agente no
    declara que no pudo, en lectura no se toco el workspace y, si se exigian
    cambios, los hay."""
    V = contrato.Verificacion
    cambio = bool(cwd) and _estado_git(cwd) != antes
    vs = [V("proceso", contrato.OK if res.proceso_ok else contrato.FALLO,
            "" if res.proceso_ok else
            ("el agente termino con error" if res.estado == "fallo" else res.estado)
            + (": " + " ".join(res.respuesta.split())[:160] if res.respuesta else ""))]
    if res.proceso_ok:
        vs.append(V("respuesta", contrato.OK if (res.respuesta.strip() or cambio)
                    else contrato.FALLO, "" if (res.respuesta.strip() or cambio)
                    else "el agente no devolvio respuesta ni cambios"))
        no_pudo = (not cambio) and bool(_NO_PUDO.search(res.respuesta))
        vs.append(V("no_pudo", contrato.FALLO if no_pudo else contrato.OK,
                    "el agente reporta que no pudo completar la tarea" if no_pudo else ""))
        if not escritura and cwd:
            vs.append(V("permisos", contrato.FALLO if cambio else contrato.OK,
                        "modifico el workspace en modo lectura" if cambio else ""))
        else:
            vs.append(V("permisos", contrato.OK,
                        "escritura: solo se comprueba dentro del worktree" if escritura else
                        "lectura sin workspace"))
        if requiere_cambios:
            vs.append(V("cambios", contrato.OK if cambio else contrato.FALLO,
                        "" if cambio else "no dejo cambios en el worktree"))
        else:
            vs.append(V("cambios", contrato.NO_APLICA))
    res.verificaciones = vs + [v for v in res.verificaciones
                               if v.nombre not in {x.nombre for x in vs}]
    if res.proceso_ok:
        if requiere_cambios and not cambio:
            res.estado, res.codigo_error = "sin_cambios", contrato.E_SIN_CAMBIOS
        elif not res.cumple("minimo"):
            # termino "bien" pero no cumple: el estado tiene que decirlo, si no
            # queda "completado" con un codigo de error (contradictorio)
            permisos = res.verificacion("permisos")
            res.estado = "fallo"
            if res.codigo_error is None:
                res.codigo_error = (contrato.E_PERMISO if permisos and
                                    permisos.estado == contrato.FALLO else contrato.E_NO_PUDO)
    return res


# --------------------------------------------------------------- checkpoint por intento

def _git(cwd, *args, env=None, timeout=60):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                          timeout=timeout, env=env)


def _arbol_de_trabajo(cwd):
    """Hash (tree) del contenido actual del worktree, incluidos los archivos sin
    seguimiento, SIN tocar el indice real: usa un indice temporal."""
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(tmp) / "index"))
        if _git(cwd, "read-tree", "HEAD", env=env).returncode != 0:
            return None
        _git(cwd, "add", "-A", "--", ".", ":(exclude).soou-plan.json", env=env)
        r = _git(cwd, "write-tree", env=env)
        return r.stdout.strip() if r.returncode == 0 else None


def punto_de_control(cwd) -> dict | None:
    """Foto exacta del worktree antes de un intento: HEAD y contenido (con lo no
    commiteado y lo no seguido). Incluye el trabajo de pasos previos y cualquier
    cambio del usuario: volver aqui no los pierde."""
    if not cwd:
        return None
    try:
        head = _git(cwd, "rev-parse", "HEAD").stdout.strip()
        arbol = _arbol_de_trabajo(cwd)
        return {"head": head, "arbol": arbol} if head and arbol else None
    except Exception:
        return None


def volver_a(cwd, punto) -> bool:
    """Deja el worktree exactamente como en el punto de control: deshace commits
    del intento, restaura archivos borrados o cambiados y quita los archivos
    nuevos no ignorados. No usa `git clean` sobre lo que ya habia."""
    if not cwd or not punto:
        return False
    try:
        _git(cwd, "reset", "-q", punto["head"])              # HEAD y indice al punto
        _git(cwd, "checkout", punto["arbol"], "--", ".")      # contenido del punto
        en_punto = set(_git(cwd, "ls-tree", "-r", "--name-only", punto["arbol"])
                       .stdout.splitlines())
        nuevos = _git(cwd, "ls-files", "--others", "--exclude-standard").stdout.splitlines()
        for rel in nuevos:
            if rel not in en_punto and rel != ".soou-plan.json":
                try:
                    (Path(cwd) / rel).unlink()
                except OSError:
                    pass
        _git(cwd, "reset", "-q")                              # indice = HEAD
        return _arbol_de_trabajo(cwd) == punto["arbol"]
    except Exception:
        return False


# --------------------------------------------------------------- seleccion e intentos

def nuevo_run_id() -> str:
    return "run-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)


def candidatos(nivel: str, modo: str, cfg: dict | None = None) -> list:
    """Agentes disponibles que pueden cumplir el modo, en orden: routing del
    nivel (eje costo) y luego historial aprendido."""
    cfg = cfg or config.load_config()
    avail = [a for a in registro.build_adapters() if a.available()]
    if modo == "escribir":       # los solo-API no aplican cambios en disco
        avail = [a for a in avail if getattr(a, "can_write", True)]
    else:                        # lectura: solo agentes cuya restriccion es real
        avail = [a for a in avail if agentes.lectura_segura(a)]
    order = (cfg.get("routing") or {}).get(nivel, [])
    if order:
        avail.sort(key=lambda a: order.index(a.name) if a.name in order else len(order))
    return _ordenar_por_historial(avail, nivel)


def _sin_presupuesto(ad, intento):
    res = contrato.TaskResult(estado="rechazado", agente=ad.name,
                              codigo_error=contrato.E_PRESUPUESTO, intento=intento,
                              respuesta="limite diario de invocaciones alcanzado ("
                              + config._budget_left_str() + ")")
    res.verificaciones = [contrato.Verificacion("proceso", contrato.FALLO, res.respuesta)]
    return res


def intentar(ad, req, intento: int = 0, reservado: bool = False):
    """Un intento completo con un agente. Devuelve un TaskResult verificado.
    Reserva el presupuesto ANTES de lanzar (salvo que el llamador ya lo hizo)."""
    if not reservado and not config._budget_reserve(1):
        return _sin_presupuesto(ad, intento)
    modelo = req.modelo or config._model_for(ad.name, req.nivel)
    reasoning = req.reasoning or config._reasoning_for(ad.name, req.nivel)
    ws = req.workspace
    punto = punto_de_control(ws) if ws and not req.conservar_parcial else None
    antes = _estado_git(ws)
    t0 = time.time()
    try:
        salida = ad.run(req.prompt(), allow_write=req.escritura, timeout=req.timeout,
                        cwd=ws, model=modelo, reasoning=reasoning)
    except Exception as e:                 # un adaptador nunca debe tumbar el run
        salida = agentes.fallo_lanzamiento(ad.name, e)
    res = contrato.desde_legado(salida, agente=ad.name,
                                duracion_ms=int((time.time() - t0) * 1000))
    res.intento = intento
    verificar_minimo(res, req.requiere_cambios, ws, antes, req.escritura)
    util = res.cumple("minimo")
    restaurado = None
    if not util and ws and punto:
        restaurado = volver_a(ws, punto)
    _traza({"gen_ai.operation.name": "execute_step", "gen_ai.agent.name": ad.name,
            "gen_ai.request.model": modelo, "soou.level": req.nivel,
            "soou.run_id": req.run_id, "soou.step": req.paso, "soou.attempt": intento,
            "soou.mode": req.operacion or ("run-write" if req.escritura else "run-read"),
            "soou.status": "ok" if util else "error", "soou.estado": res.estado,
            "soou.error_code": res.codigo_error, "soou.reason": res.motivo,
            "soou.checks": res.resumen_verificacion(),
            "soou.restored": restaurado, "duration_ms": res.duracion_ms})
    res.modelo, res.reasoning = modelo, reasoning
    return res


def ejecutar_con_fallback(req, lista, al_empezar=None, al_fallar=None):
    """Intenta con cada agente en orden hasta el primero que cumple el minimo.
    Devuelve (resultado_util | None, intentos). Se detiene sin presupuesto."""
    intentos = []
    for n, ad in enumerate(lista):
        if not config._budget_reserve(1):      # sin cupo: ni se anuncia ni se lanza
            res = _sin_presupuesto(ad, n)
            intentos.append(res)
            if al_fallar:
                al_fallar(ad, res)
            break
        if al_empezar:
            al_empezar(ad, req)
        res = intentar(ad, req, intento=n, reservado=True)
        intentos.append(res)
        if res.cumple("minimo"):
            return res, intentos
        if al_fallar:
            al_fallar(ad, res)
    return None, intentos


# --------------------------------------------------------------- destilado coordinado

class InvocadorDestilado:
    """Las llamadas a agentes del destilado (sync --deep, persona) pasan por aqui:
    comparten presupuesto, trazas y verificacion minima con el harness. El modulo
    de destilado (capa de memoria) no importa el ejecutor: recibe este objeto y
    solo ve resultados.

    El destilado edita el wiki real en la raiz de datos y su trabajo es ADITIVO:
    un intento fallido puede haber dejado notas utiles, asi que no hay checkpoint
    ni vuelta atras (conservar_parcial)."""

    nivel = "complex"             # persona: sintesis unica, con el modelo frontier
    nivel_destilado = "trivial"   # distill: corre solo y seguido -> lo mas barato

    def __init__(self, run_id: str | None = None):
        self.run_id = run_id or nuevo_run_id()

    def _nivel(self, operacion: str) -> str:
        return self.nivel_destilado if operacion == "distill" else self.nivel

    def candidatos(self, operacion: str = "distill") -> list:
        return candidatos(self._nivel(operacion), "escribir")

    def _req(self, prompt: str, operacion: str, timeout: int, agente=None):
        """`agente` solo importa al destilar: su modelo se fija aqui
        (`models.<agente>.distill`, si falta el de `trivial`) y `intentar` lo
        respeta, asi el Nivel B nunca hereda el modelo de otro nivel."""
        modelo = reasoning = None
        if operacion == "distill" and agente is not None:
            modelo = config.modelo_destilado(agente.name)
            reasoning = config.reasoning_destilado(agente.name)
        return contrato.TaskRequest(tarea=prompt, workspace=str(rutas.ROOT), modo="escribir",
                                    exige_cambios=False, timeout=timeout, run_id=self.run_id,
                                    paso=operacion, nivel=self._nivel(operacion),
                                    operacion=operacion, conservar_parcial=True,
                                    modelo=modelo, reasoning=reasoning)

    def invocar(self, agente, prompt: str, intento: int = 0, operacion: str = "distill",
                timeout: int = 1200, al_empezar=None):
        """Un intento con `agente`; el llamador decide el fallback (el lote de
        sesiones se recalcula entre intentos). Reserva el cupo ANTES de anunciar."""
        if not config._budget_reserve(1):
            return _sin_presupuesto(agente, intento)
        if al_empezar:
            al_empezar(agente)
        return intentar(agente, self._req(prompt, operacion, timeout, agente),
                        intento=intento, reservado=True)

    def ejecutar(self, prompt: str, operacion: str, timeout: int = 1200,
                 al_empezar=None, al_fallar=None):
        """Prompt fijo con fallback: (resultado_util | None, intentos)."""
        return ejecutar_con_fallback(self._req(prompt, operacion, timeout),
                                     self.candidatos(operacion), al_empezar, al_fallar)


def _con_invocador(fn):
    def cmd(args):
        args.invocador = InvocadorDestilado()
        return fn(args)
    cmd.__name__, cmd.__doc__ = fn.__name__, fn.__doc__
    return cmd


cmd_distill = _con_invocador(destilado.cmd_distill)
cmd_sync = _con_invocador(destilado.cmd_sync)
cmd_persona = _con_invocador(destilado.cmd_persona)


# --------------------------------------------------------------- procesos en segundo plano

# Unico lanzador desacoplado (UI, MCP, harness y el hook de destilado). Vive en
# agentes, la capa base que maneja procesos, para que la capa de memoria lo use.
lanzar_desacoplado = agentes.lanzar_desacoplado


# --------------------------------------------------------------- soou trace

def leer_trazas(run_id: str | None = None) -> list:
    f = sesiones._runs_dir() / "trazas.jsonl"
    if not f.exists():
        return []
    filas = []
    for l in f.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(l)
        except Exception:
            continue
        if run_id is None or r.get("soou.run_id") == run_id:
            filas.append(r)
    return filas


def cmd_trace(args):
    """Sin id: los ultimos runs. Con id: cada intento y por que se paso al siguiente."""
    rid = (getattr(args, "run_id", None) or "").strip() or None
    filas = leer_trazas(rid)
    if rid is None:
        runs = {}
        for r in filas:
            if r.get("soou.run_id"):
                runs.setdefault(r["soou.run_id"], []).append(r)
        if not runs:
            print("Sin runs con id en runs/trazas.jsonl todavia.")
            return
        for k in list(runs)[-(getattr(args, "n", 10) or 10):]:
            pasos = [x for x in runs[k] if x.get("gen_ai.operation.name") == "execute_step"]
            ok = any(x.get("soou.status") == "ok" for x in pasos)
            print(f"{k}  {runs[k][0].get('ts', '')}  {len(pasos)} intento(s)  "
                  f"{'ok' if ok else 'sin exito'}")
        return
    if not filas:
        print(f"No hay trazas para {rid}.")
        return
    print(f"== {rid} ==")
    for r in filas:
        op = r.get("gen_ai.operation.name")
        if op == "execute_step":
            paso = f"paso {r['soou.step']} · " if r.get("soou.step") else ""
            print(f"  {r.get('ts', '')}  {paso}intento {r.get('soou.attempt', 0)}  "
                  f"{r.get('gen_ai.agent.name')}"
                  + (f" ({r['gen_ai.request.model']})" if r.get("gen_ai.request.model") else "")
                  + f"  {r.get('soou.estado', r.get('soou.status'))}"
                  + (f" [{r['soou.error_code']}]" if r.get("soou.error_code") else "")
                  + f"  {r.get('duration_ms', 0) / 1000:.1f}s")
            if r.get("soou.reason"):
                print(f"      motivo: {r['soou.reason']}")
            if r.get("soou.checks"):
                print(f"      verificaciones: {r['soou.checks']}")
            if r.get("soou.restored") is not None:
                print(f"      vuelta al checkpoint: {'ok' if r['soou.restored'] else 'INCOMPLETA'}")
        elif op == "validate":
            print(f"  {r.get('ts', '')}  validacion: {r.get('soou.status')}")
