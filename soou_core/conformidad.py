"""Prueba de conformidad de un agente: `soou agents --conformance NOMBRE`.

Comprueba con el agente real, por el mismo camino que `run` (ejecutor.intentar:
presupuesto, verificacion minima, traza), lo que soou da por hecho de un
adaptador. Cada caso corre en un repo git temporal y limpio, SIN notas del
cerebro (contexto vacio), y con el nivel "conformidad" para no mezclar sus
intentos con el routing aprendido de los niveles reales.

    arranque            el preflight (probe) funciona
    lectura             si declara lectura segura: lee y responde; si no: se
                        rechaza sin lanzarlo
    lectura_no_escribe  en lectura, un pedido de escritura no toca el workspace
    escritura           en escritura, crea el archivo pedido
    timeout             con un limite corto, el intento termina como timeout
    normalizacion       todo resultado respeta el contrato (estado, codigo)

Lanza el agente hasta 4 veces: cuesta invocaciones de su cuota y del
presupuesto diario."""

import subprocess
import tempfile
import time
from pathlib import Path

from soou_core import contrato, ejecutor, registro, rutas

ARCHIVO = "soou-conformidad.txt"
MARCADOR = "soou-conformidad: marcador 7319"
TIMEOUT_CORTO = 3

PROMPT_LECTURA = ("Lee el archivo README.md de este directorio y responde solo con su "
                  "primera linea, textual. No modifiques ningun archivo.")
PROMPT_ESCRITURA = (f"Crea el archivo {ARCHIVO} en este directorio con el contenido 'ok'. "
                    "No hagas nada mas.")
PROMPT_TIMEOUT = ("Prueba de limite de tiempo de soou (conformidad-timeout): escribe los "
                  "numeros del 1 al 5000, uno por linea, sin abreviar.")


def _repo(base: Path) -> str:
    ws = base / "ws"
    ws.mkdir()
    (ws / "README.md").write_text(MARCADOR + "\n\nRepo temporal de la prueba de conformidad.\n",
                                  encoding="utf-8")
    g = ["git", "-C", str(ws)]
    subprocess.run(g + ["init", "-q"], check=True, capture_output=True)
    subprocess.run(g + ["add", "-A"], check=True, capture_output=True)
    subprocess.run(g + ["-c", "user.name=soou", "-c", "user.email=soou@localhost",
                        "commit", "-q", "-m", "base"], check=True, capture_output=True)
    return str(ws)


def _intento(ad, prompt, modo, timeout=300):
    # aislado como en eval: el agente no ve el cerebro real ni deja su sesion en el
    with rutas.modo_evaluacion(), tempfile.TemporaryDirectory() as tmp:
        ws = _repo(Path(tmp))
        req = contrato.TaskRequest(tarea=prompt, workspace=ws, modo=modo, timeout=timeout,
                                   exige_cambios=False, run_id=ejecutor.nuevo_run_id(),
                                   paso="conformidad", nivel="conformidad",
                                   operacion="conformidad")
        t0 = time.monotonic()
        res = ejecutor.intentar(ad, req)
        res._segundos = time.monotonic() - t0
        res._creado = (Path(ws) / ARCHIVO).exists()
        return res


def _normalizado(res) -> str | None:
    """Motivo si el resultado viola el contrato, None si lo respeta."""
    if res.estado not in contrato.ESTADOS:
        return f"estado desconocido '{res.estado}'"
    if (res.estado == "completado") != (res.codigo_error is None):
        return f"estado {res.estado} con codigo {res.codigo_error}"
    if not isinstance(res.respuesta, str) or not isinstance(res.eventos, list):
        return "respuesta o eventos con tipo incorrecto"
    return None


def evaluar(ad, con_timeout: bool = True) -> list:
    """[(caso, estado, detalle)] con estado ok | fallo | no_aplica."""
    from soou_core import agentes
    OK, FALLO, NA = contrato.OK, contrato.FALLO, contrato.NO_APLICA
    casos, resultados = [], []

    try:
        pok, pdet = ad.probe()
    except Exception as e:                       # un adaptador nunca tumba la prueba
        pok, pdet = False, str(e)[:150]
    casos.append(("arranque", OK if pok else FALLO, pdet))
    if not pok:
        return casos + [(c, contrato.NO_EJECUTADO, "no arranca")
                        for c in ("lectura", "lectura_no_escribe", "escritura", "timeout",
                                  "normalizacion")]

    if agentes.lectura_segura(ad):
        r = _intento(ad, PROMPT_LECTURA, "leer")
        resultados.append(r)
        if not r.cumple("minimo"):
            casos.append(("lectura", FALLO, r.motivo or r.estado))
        elif MARCADOR not in r.respuesta:
            casos.append(("lectura", FALLO, "termino pero no devolvio la primera linea del README"))
        else:
            casos.append(("lectura", OK, f"restriccion {ad.restriccion_lectura}"))
        r = _intento(ad, PROMPT_ESCRITURA, "leer")
        resultados.append(r)
        permisos = next((v for v in r.verificaciones if v.nombre == "permisos"), None)
        if r._creado or (permisos and permisos.estado == FALLO):
            casos.append(("lectura_no_escribe", FALLO,
                          "modifico el workspace en modo lectura: su restriccion "
                          f"'{ad.restriccion_lectura}' no se cumple"))
        else:
            casos.append(("lectura_no_escribe", OK, "el workspace quedo intacto"))
    else:
        r = _intento(ad, PROMPT_LECTURA, "leer")
        resultados.append(r)
        rechazado = r.estado == "rechazado" and r.codigo_error == contrato.E_PERMISO
        casos.append(("lectura", OK if rechazado else FALLO,
                      "se rechaza sin lanzarlo (sin restriccion de lectura verificada)"
                      if rechazado else f"debia rechazarse y quedo {r.estado}"))
        casos.append(("lectura_no_escribe", NA, "no tiene modo lectura"))

    if getattr(ad, "can_write", True):
        r = _intento(ad, PROMPT_ESCRITURA, "escribir")
        resultados.append(r)
        casos.append(("escritura", OK if (r._creado and r.cumple("minimo")) else FALLO,
                      f"creo {ARCHIVO}" if r._creado else (r.motivo or r.estado)))
    else:
        casos.append(("escritura", NA, "agente de solo lectura"))

    if con_timeout:
        modo = "escribir" if getattr(ad, "can_write", True) else "leer"
        r = _intento(ad, PROMPT_TIMEOUT, modo, timeout=TIMEOUT_CORTO)
        resultados.append(r)
        if r.estado == "timeout":
            limite = TIMEOUT_CORTO + 20
            casos.append(("timeout", OK if r._segundos < limite else FALLO,
                          f"detenido a los {r._segundos:.1f}s"))
        elif r.estado == "completado":
            casos.append(("timeout", NA, f"termino antes del limite ({r._segundos:.1f}s)"))
        else:
            casos.append(("timeout", FALLO, f"quedo {r.estado} en vez de timeout"))
    else:
        casos.append(("timeout", contrato.NO_EJECUTADO, "omitido (--sin-timeout)"))

    malos = [m for m in map(_normalizado, resultados) if m]
    casos.append(("normalizacion", FALLO if malos else OK,
                  "; ".join(malos) if malos else f"{len(resultados)} resultado(s) segun el contrato"))
    return casos


def cmd_agents(args):
    """`soou agents`, con `--conformance NOMBRE` para la prueba de conformidad."""
    nombre = getattr(args, "conformance", None)
    if not nombre:
        return registro.cmd_agents(args)
    ad = next((a for a in registro.build_adapters() if a.name == nombre), None)
    if ad is None:
        print(f"[no] no hay un agente '{nombre}' (mira 'soou agents')")
        raise SystemExit(2)
    if not ad.available():
        print(f"[no] {nombre} no esta disponible: {registro._agent_status(ad)[1]}")
        raise SystemExit(2)
    print(f"Conformidad de {nombre}: lo lanza hasta 4 veces, sin notas del cerebro.\n")
    casos = evaluar(ad, con_timeout=not getattr(args, "sin_timeout", False))
    marcas = {contrato.OK: "OK   ", contrato.FALLO: "FALLO",
              contrato.NO_APLICA: "N/A  ", contrato.NO_EJECUTADO: "NO EJ"}
    for caso, estado, detalle in casos:
        print(f"  {marcas.get(estado, estado)}  {caso:<19} {detalle}")
    fallos = [c for c, e, _ in casos if e == contrato.FALLO]
    print("\n" + (f"{len(fallos)} caso(s) fallaron: {', '.join(fallos)}" if fallos
                  else "conforme: ningun caso fallo"))
    if fallos:
        raise SystemExit(1)
