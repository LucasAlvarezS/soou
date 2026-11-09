"""El destilado pide sus agentes al ejecutor: presupuesto, trazas, verificacion
minima y fallback compartidos con el harness. Ningun test lanza un agente real
ni escribe en los datos del usuario."""

import argparse
import ast
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
from soou_core import config, destilado, ejecutor, indice, registro, rutas, sesiones  # noqa: E402


class _Agente:
    can_write = True

    def __init__(self, name, salida, escribe=None, al_correr=None):
        self.name, self.salida, self.escribe, self.al_correr = name, salida, escribe, al_correr
        self.llamadas = []

    def available(self):
        return True

    def run(self, prompt, allow_write=False, cwd=None, **k):
        self.llamadas.append({"allow_write": allow_write, "cwd": cwd, **k})
        if self.escribe:
            (Path(cwd) / self.escribe).write_text("nota parcial", encoding="utf-8")
        if self.al_correr:
            self.al_correr()
        return self.salida


class _Entorno(unittest.TestCase):
    """Datos en un temporal, agentes falsos, presupuesto y trazas controlados."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.previos = {n: getattr(rutas, n) for n in ("ROOT", "WIKI", "SESSIONS", "DISTILL_DIR")}
        rutas.ROOT, rutas.WIKI = self.tmp, self.tmp / "wiki"
        rutas.SESSIONS, rutas.DISTILL_DIR = self.tmp / "sessions" / "raw", self.tmp / "distill"
        rutas.WIKI.mkdir(parents=True)
        self.pendientes = [self.tmp / "s1.md", self.tmp / "s2.md"]
        self.reservas = 0

    def tearDown(self):
        for n, v in self.previos.items():
            setattr(rutas, n, v)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _entorno(self, agentes_, cupo=True):
        def reservar(n=1):
            self.reservas += 1
            return cupo
        pila = ExitStack()
        for obj, nombre, kw in [
            (registro, "build_adapters", {"return_value": agentes_}),
            (config, "load_config", {"return_value": {"routing": {}}}),
            (config, "_budget_reserve", {"side_effect": reservar}),
            (config, "_budget_left_str", {"return_value": "0"}),
            (config, "_model_for", {"return_value": None}),
            (config, "_reasoning_for", {"return_value": None}),
            (sesiones, "_runs_dir", {"return_value": self.tmp / "runs"}),
            (sesiones, "_pull_all_sessions", {"return_value": 0}),
            (sesiones, "pending_sessions", {"side_effect": lambda *a: list(self.pendientes)}),
            (destilado, "run_lite", {"return_value": []}),
            (indice, "load_notes", {"return_value": []}),
            (indice, "build_index", {}),
            (indice, "cmd_reindex", {}),
        ]:
            pila.enter_context(patch.object(obj, nombre, **kw))
        return pila

    def _trazas(self):
        f = self.tmp / "runs" / "trazas.jsonl"
        return [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines()] if f.exists() else []


class DestiladoPorEjecutorTests(_Entorno):
    def test_nivel_b_usa_el_ejecutor_con_fallback_y_trazas(self):
        malo = _Agente("codex", {"ok": False, "output": "usage limit reached"})
        bueno = _Agente("claude-code", {"ok": True, "output": "Destile 2 sesiones."},
                        al_correr=lambda: self.pendientes.clear())
        with self._entorno([malo, bueno]):
            ejecutor.cmd_distill(argparse.Namespace(limit=0))
        self.assertEqual((len(malo.llamadas), len(bueno.llamadas)), (1, 1))
        self.assertTrue(bueno.llamadas[0]["allow_write"])
        self.assertEqual(bueno.llamadas[0]["cwd"], str(self.tmp))
        self.assertEqual(self.reservas, 2, "cada intento reserva presupuesto")
        trazas = self._trazas()
        self.assertEqual([t["soou.mode"] for t in trazas], ["distill", "distill"])
        self.assertEqual([t["soou.status"] for t in trazas], ["error", "ok"])
        self.assertEqual(len({t["soou.run_id"] for t in trazas}), 1, "un run por destilado")
        self.assertEqual(len(list(rutas.DISTILL_DIR.glob("distill-*.txt"))), 2, "log por intento")

    def test_sin_presupuesto_no_lanza_ningun_agente(self):
        a = _Agente("codex", {"ok": True, "output": "x"})
        b = _Agente("claude-code", {"ok": True, "output": "x"})
        with self._entorno([a, b], cupo=False):
            ejecutor.cmd_distill(argparse.Namespace(limit=0))
        self.assertEqual((a.llamadas, b.llamadas), ([], []))
        self.assertEqual(self.reservas, 1, "sin cupo se corta: no prueba al siguiente")

    def test_un_intento_fallido_conserva_el_trabajo_aditivo(self):
        import subprocess
        subprocess.run(["git", "init", "-q", str(self.tmp)], check=True)
        subprocess.run(["git", "-C", str(self.tmp), "-c", "user.name=t", "-c", "user.email=t@t",
                        "commit", "-q", "--allow-empty", "-m", "base"], check=True)
        parcial = _Agente("codex", {"ok": False, "output": "se corto"}, escribe="nota-nueva.md")
        with self._entorno([parcial]):
            ejecutor.cmd_distill(argparse.Namespace(limit=0))
        self.assertTrue((self.tmp / "nota-nueva.md").exists(),
                        "el destilado es aditivo: un fallo no deshace notas ya escritas")

    def test_sin_invocador_queda_solo_el_nivel_a(self):
        a = _Agente("codex", {"ok": True, "output": "x"})
        with self._entorno([a]):
            destilado.cmd_distill(argparse.Namespace(limit=0))
        self.assertEqual(a.llamadas, [])

    def test_sync_deep_pasa_el_invocador(self):
        a = _Agente("codex", {"ok": True, "output": "ok"}, al_correr=lambda: self.pendientes.clear())
        with self._entorno([a]), patch.object(destilado, "_escribir_heartbeat"):
            ejecutor.cmd_sync(argparse.Namespace(deep=True, limit=0))
        self.assertEqual(len(a.llamadas), 1)

    def test_persona_con_fallback(self):
        malo = _Agente("codex", {"ok": True, "output": "No pude: permission denied"})
        bueno = _Agente("claude-code", {"ok": True, "output": "Persona escrita."})
        with self._entorno([malo, bueno]):
            ejecutor.cmd_persona(argparse.Namespace())
        self.assertEqual((len(malo.llamadas), len(bueno.llamadas)), (1, 1))
        self.assertEqual([t["soou.mode"] for t in self._trazas()], ["persona", "persona"])


class PlanificadorTests(_Entorno):
    """El planificador de run --plan tambien pasa por el ejecutor."""

    def test_plan_task_usa_el_ejecutor_en_lectura(self):
        from soou_core import agentes, harness

        class Planificador(_Agente):
            restriccion_lectura = "sandbox_os"

        a = Planificador("codex", {"ok": True, "output": '["uno", "dos"]'})
        with self._entorno([a]), patch.object(harness, "_brain_context", return_value=""):
            subs = harness._plan_task("tarea grande", [])
        self.assertEqual(subs, ["uno", "dos"])
        self.assertFalse(a.llamadas[0]["allow_write"])
        self.assertTrue(agentes.lectura_segura(a))
        self.assertEqual([t["soou.mode"] for t in self._trazas()], ["plan"])
        self.assertEqual(self.reservas, 1)

    def test_sin_agente_de_lectura_segura_no_planifica(self):
        from soou_core import harness
        a = _Agente("goose", {"ok": True, "output": '["x"]'})   # sin restriccion de lectura
        with self._entorno([a]), patch.object(harness, "_brain_context", return_value=""):
            self.assertEqual(harness._plan_task("tarea", []), ["tarea"])
        self.assertEqual(a.llamadas, [])


class CapasTests(unittest.TestCase):
    """La capa de memoria no importa la capa superior: recibe al ejecutor."""

    def test_memoria_no_importa_la_capa_superior(self):
        superior = {"ejecutor", "harness", "instalacion", "ui"}
        for mod in ("sesiones", "destilado", "compactar"):
            arbol = ast.parse((RAIZ / "soou_core" / f"{mod}.py").read_text(encoding="utf-8"))
            nombres = set()
            for nodo in ast.walk(arbol):
                if isinstance(nodo, ast.ImportFrom):
                    nombres |= {a.name for a in nodo.names} | {(nodo.module or "").split(".")[-1]}
                elif isinstance(nodo, ast.Import):
                    nombres |= {a.name.split(".")[-1] for a in nodo.names}
            self.assertFalse(nombres & superior, f"{mod} importa {nombres & superior}")


if __name__ == "__main__":
    unittest.main()
