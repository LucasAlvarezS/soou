"""Ejecutor unico: contrato, minimo de verificacion, checkpoint por intento,
fallback, timeout con procesos hijos y presupuesto con reserva.

Lo que NO debe pasar: que "no ejecutado" cuente como aprobado, que un fallback
borre trabajo previo o del usuario, que un timeout deje procesos escribiendo, o
que dos procesos se pasen del cupo leyendo el mismo saldo."""

import contextlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
from soou_core import agentes, config, contrato, ejecutor, rutas, sesiones  # noqa: E402

FALSO = str(Path(__file__).resolve().parent / "fixtures" / "agente_falso.py")


def _git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True,
                          check=True).stdout


def _repo(base: Path) -> Path:
    repo = base / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("original", encoding="utf-8")
    (repo / "b.txt").write_text("se va a borrar", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


class _Agente:
    """Adaptador de prueba: hace lo que le pase `accion(cwd)` y devuelve `salida`."""
    can_write = True
    restriccion_lectura = "aprobacion_cli"

    def __init__(self, nombre, salida="listo", accion=None, ok=True):
        self.name, self.salida, self.accion, self.ok = nombre, salida, accion, ok
        self.llamadas = 0

    def available(self):
        return True

    def run(self, prompt, allow_write=False, timeout=1200, cwd=None, model=None, reasoning=None):
        self.llamadas += 1
        if self.accion:
            self.accion(Path(cwd) if cwd else None)
        return {"ok": self.ok, "output": self.salida}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.pila = contextlib.ExitStack()
        self.pila.enter_context(patch.object(sesiones, "_runs_dir", return_value=self.tmp / "runs"))
        self.pila.enter_context(patch.object(config, "_budget_reserve", return_value=True))
        self.pila.enter_context(patch.object(config, "_model_for", return_value=None))
        self.pila.enter_context(patch.object(config, "_reasoning_for", return_value=None))

    def tearDown(self):
        self.pila.close()


class ContratoTests(unittest.TestCase):
    def test_verificado_se_deriva_y_no_ejecutado_no_aprueba(self):
        V = contrato.Verificacion
        r = contrato.TaskResult(estado="completado", politica="validadores", verificaciones=[
            V(n, contrato.OK) for n in contrato.MINIMO])
        self.assertTrue(r.cumple("minimo"))
        self.assertFalse(r.verificado, "falta la verificacion de validadores")
        r.verificaciones.append(V("validadores", contrato.NO_EJECUTADO))
        self.assertFalse(r.verificado, "no ejecutado nunca cuenta como aprobado")
        r.verificaciones[-1] = V("validadores", contrato.OK)
        self.assertTrue(r.verificado)

    def test_las_politicas_nunca_quitan_el_minimo(self):
        for nombre, exigidas in contrato.POLITICAS.items():
            self.assertTrue(set(contrato.MINIMO) <= set(exigidas), nombre)

    def test_salida_historica_se_traduce(self):
        r = contrato.desde_legado({"ok": False, "estado": "timeout", "codigo_error": "E_TIMEOUT",
                                   "output": "x"}, agente="a")
        self.assertEqual((r.estado, r.codigo_error, r.uso_reportado), ("timeout", "E_TIMEOUT", None))


class MinimoTests(Base):
    def test_lectura_sin_respuesta_no_sirve(self):
        req = contrato.TaskRequest(tarea="t")
        r = ejecutor.intentar(_Agente("a", salida="   "), req)
        self.assertFalse(r.cumple("minimo"))
        self.assertIn("respuesta", r.motivo)

    def test_lectura_que_escribe_viola_permisos_y_se_deshace(self):
        repo = _repo(self.tmp)
        req = contrato.TaskRequest(tarea="t", workspace=str(repo), modo="leer")
        ag = _Agente("a", accion=lambda cwd: (cwd / "intruso.txt").write_text("x"))
        r = ejecutor.intentar(ag, req)
        self.assertEqual(r.verificacion("permisos").estado, contrato.FALLO)
        self.assertFalse((repo / "intruso.txt").exists())

    def test_escritura_sin_cambios_queda_sin_cambios(self):
        repo = _repo(self.tmp)
        req = contrato.TaskRequest(tarea="t", workspace=str(repo), modo="escribir")
        r = ejecutor.intentar(_Agente("a", salida="hecho"), req)
        self.assertEqual((r.estado, r.codigo_error), ("sin_cambios", contrato.E_SIN_CAMBIOS))
        self.assertFalse(r.cumple("minimo"))

    def test_un_adaptador_que_explota_es_un_fallo_no_una_excepcion(self):
        class Roto(_Agente):
            def run(self, *a, **k):
                raise RuntimeError("boom")
        r = ejecutor.intentar(Roto("roto"), contrato.TaskRequest(tarea="t"))
        self.assertEqual(r.estado, "fallo")
        self.assertEqual(r.codigo_error, contrato.E_LANZAMIENTO)


class CheckpointTests(Base):
    def test_fallback_no_borra_trabajo_previo_ni_del_usuario(self):
        repo = _repo(self.tmp)
        (repo / "paso1.txt").write_text("de un paso anterior", encoding="utf-8")
        _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "paso 1")
        (repo / "a.txt").write_text("cambio del usuario sin commitear", encoding="utf-8")
        (repo / "nota-usuario.txt").write_text("archivo nuevo del usuario", encoding="utf-8")

        def destrozo(cwd):
            (cwd / "a.txt").write_text("pisado por el agente", encoding="utf-8")
            (cwd / "b.txt").unlink()
            (cwd / "basura.txt").write_text("x", encoding="utf-8")
            _git(cwd, "add", "-A"); _git(cwd, "commit", "-q", "-m", "commit del agente")

        malo = _Agente("malo", salida="No pude: helper_unknown_error", accion=lambda c: None)
        destructor = _Agente("destructor", salida="no pude terminar", ok=False, accion=destrozo)
        bueno = _Agente("bueno", accion=lambda c: (c / "resultado.txt").write_text("ok"))
        head_antes = _git(repo, "rev-parse", "HEAD").strip()

        req = contrato.TaskRequest(tarea="t", workspace=str(repo), modo="escribir")
        res, intentos = ejecutor.ejecutar_con_fallback(req, [malo, destructor, bueno])

        self.assertEqual(res.agente, "bueno")
        self.assertEqual([i.agente for i in intentos], ["malo", "destructor", "bueno"])
        self.assertEqual(_git(repo, "rev-parse", "HEAD").strip(), head_antes,
                         "el commit del agente fallido se deshizo")
        self.assertEqual((repo / "a.txt").read_text(encoding="utf-8"),
                         "cambio del usuario sin commitear")
        self.assertTrue((repo / "nota-usuario.txt").exists())
        self.assertTrue((repo / "b.txt").exists(), "lo borrado por el agente vuelve")
        self.assertTrue((repo / "paso1.txt").exists())
        self.assertFalse((repo / "basura.txt").exists())
        self.assertTrue((repo / "resultado.txt").exists())

    def test_la_traza_registra_run_intento_y_vuelta(self):
        repo = _repo(self.tmp)
        req = contrato.TaskRequest(tarea="t", workspace=str(repo), modo="escribir",
                                   run_id="run-x", paso="1/1", nivel="complex")
        ejecutor.intentar(_Agente("a", salida="nada"), req, intento=0)
        fila = json.loads((self.tmp / "runs" / "trazas.jsonl").read_text(encoding="utf-8")
                          .splitlines()[-1])
        self.assertEqual((fila["soou.run_id"], fila["soou.step"], fila["soou.attempt"]),
                         ("run-x", "1/1", 0))
        self.assertEqual(fila["soou.estado"], "sin_cambios")
        self.assertTrue(fila["soou.restored"])


class PresupuestoTests(Base):
    def test_sin_cupo_no_se_anuncia_ni_se_lanza(self):
        ag = _Agente("a")
        anunciados = []
        with patch.object(config, "_budget_reserve", return_value=False):
            res, intentos = ejecutor.ejecutar_con_fallback(
                contrato.TaskRequest(tarea="t"), [ag, _Agente("b")],
                al_empezar=lambda a, r: anunciados.append(a.name))
        self.assertIsNone(res)
        self.assertEqual(ag.llamadas, 0)
        self.assertEqual(anunciados, [])
        self.assertEqual(intentos[0].codigo_error, contrato.E_PRESUPUESTO)


class ReservaRealTests(unittest.TestCase):
    """La reserva de verdad, contra un ledger temporal."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.previo = rutas.BUDGET_PATH
        rutas.BUDGET_PATH = self.tmp / "budget.json"

    def tearDown(self):
        rutas.BUDGET_PATH = self.previo

    def _cfg(self, cap):
        return patch.object(config, "load_config", return_value={"caps": {"budget_per_day": cap}})

    def test_procesos_concurrentes_no_pasan_el_cupo(self):
        cfg = self.tmp / "soou.config.json"
        cfg.write_text(json.dumps({"override": {"budget_per_day": 7}}), encoding="utf-8")
        codigo = ("import sys; sys.path.insert(0, sys.argv[1]); "
                  "from soou_core import config; "
                  "print(sum(config._budget_reserve(1) for _ in range(5)))")
        env = dict(os.environ, SOOU_HOME=str(self.tmp))
        procs = [subprocess.Popen([sys.executable, "-c", codigo, str(RAIZ)], env=env,
                                  stdout=subprocess.PIPE, text=True) for _ in range(4)]
        concedidas = sum(int(p.communicate(timeout=60)[0].strip() or 0) for p in procs)
        self.assertEqual(concedidas, 7, "20 pedidos contra un cupo de 7")
        hoy = json.loads((self.tmp / ".soou-budget.json").read_text(encoding="utf-8"))
        self.assertEqual(list(hoy.values())[-1], 7)

    def test_un_lock_abandonado_se_recupera(self):
        lock = Path(str(rutas.BUDGET_PATH) + ".lock")
        lock.write_text("99999 0", encoding="utf-8")
        viejo = time.time() - config.LOCK_VIEJO_S - 5
        os.utime(lock, (viejo, viejo))
        with self._cfg(3):
            self.assertTrue(config._budget_reserve(1))
        self.assertFalse(lock.exists())

    def test_un_lock_vivo_con_cupo_niega(self):
        lock = Path(str(rutas.BUDGET_PATH) + ".lock")
        lock.write_text("1 0", encoding="utf-8")
        with self._cfg(3), patch.object(config, "LOCK_ESPERA_S", 0.2):
            self.assertFalse(config._budget_reserve(1), "sin poder bloquear no se arriesga")


class TimeoutTests(unittest.TestCase):
    def _sin_rastro(self, modo):
        with tempfile.TemporaryDirectory() as tmp:
            marca = Path(tmp) / "marca.txt"
            ad = agentes.GenericAdapter("lento", sys.executable,
                                        [FALSO, modo, str(marca), "3", "{prompt}"])
            t0 = time.time()
            res = ad.run("x", allow_write=True, timeout=1.5)
            self.assertLess(time.time() - t0, 20)
            self.assertEqual(res["estado"], "timeout")
            self.assertEqual(res["codigo_error"], "E_TIMEOUT")
            time.sleep(4.5)            # el hijo habria escrito a los 3 s
            self.assertFalse(marca.exists(), "un descendiente siguio corriendo tras el timeout")

    def test_el_timeout_mata_al_hijo(self):
        self._sin_rastro("hijo-lento")

    def test_el_timeout_mata_al_nieto(self):
        self._sin_rastro("hijo-de-hijo")


if __name__ == "__main__":
    unittest.main()
