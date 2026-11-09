"""eval --tareas sobre el corpus inventado: los pisos de CI (con el agente eco),
una tarea de escritura con un agente guionado y el aislamiento del cerebro real
(MCP, hooks, sesiones, mismo comando con y sin memoria).
Ningun test lanza un agente real ni escribe en los datos del usuario."""

import contextlib
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
from soou_core import agentes, config, evaluacion, rutas, sesiones  # noqa: E402


def _sin_config():
    return patch.object(config, "load_config", return_value={"private_paths": []})


class CorpusTests(unittest.TestCase):
    """Los pisos: si alguno baja, el paquete de contexto o el harness empeoraron."""

    @classmethod
    def setUpClass(cls):
        with _sin_config():
            cls.inf = evaluacion.evaluar()

    def test_el_contexto_trae_lo_esperado_y_nada_que_no_deba(self):
        c = self.inf["contexto"]
        self.assertEqual(c["esperadas_presentes"], 1.0)
        self.assertEqual((c["coladas"], c["reemplazadas"], c["privadas"]), (0, 0, 0))
        self.assertLessEqual(c["caracteres_medio"], 1600)

    def test_control_eco_con_memoria_y_sin_memoria(self):
        # eco no resuelve tareas: comprueba que el contexto trae los hechos y que
        # el evaluador los reconoce; sin memoria, que las tareas no los traen
        r = self.inf["agentes"]["eco"]["resumen"]
        self.assertEqual(r["resueltas_con_memoria"], 1.0, "el contexto trae todos los hechos")
        self.assertEqual(r["resueltas_sin_memoria"], 0.0, "las tareas no traen la respuesta")
        self.assertEqual(r["errores_inducidos_con_memoria"], 0)
        self.assertEqual((r["secretos_filtrados"], r["citas_inventadas"]), (0, 0))

    def test_lo_sin_comprobar_llega_marcado(self):
        fila = next(c for c in self.inf["contexto"]["por_tarea"] if c["id"] == "faro-incidente")
        self.assertEqual(fila["sin_comprobar_marcadas"], 1)

    def test_otro_proyecto_entra_solo_si_la_tarea_lo_nombra(self):
        fila = next(c for c in self.inf["contexto"]["por_tarea"]
                    if c["id"] == "telar-pregunta-colmena")
        self.assertIn("colmena-umbral-temperatura", fila["notas"])

    def test_la_reanudacion_no_repite_pasos(self):
        z = self.inf["reanudacion"]
        self.assertTrue(z["ok"], z)
        self.assertEqual(z["invocaciones"], ["1", "2", "3"])


class MedidasTests(unittest.TestCase):
    T = {"hechos": [[r"\b15\s*%", "quince"]], "prohibidos": [r"\b20\s*%"]}

    def test_hechos_sin_acentos_ni_mayusculas(self):
        self.assertTrue(evaluacion.cumple_hechos("Se dispara bajo el QUINCE por ciento", self.T))
        self.assertFalse(evaluacion.cumple_hechos("bajo 150 %", self.T))

    def test_error_inducido_solo_sin_el_dato_vigente(self):
        self.assertTrue(evaluacion.error_inducido("bajo 20 %", self.T))
        self.assertFalse(evaluacion.error_inducido("bajo 15 %; antes era 20 %", self.T))

    def test_citas(self):
        self.assertEqual(evaluacion.citas("ver [[a]] y [[b|texto]]"), {"a", "b"})


class EscrituraTests(unittest.TestCase):
    """Un agente guionado que solo acierta si el contexto trae el umbral: la
    prueba oculta lo distingue, y no estaba en el repo mientras trabajaba."""

    class Escritor:
        name, can_write, restriccion_lectura = "guion", True, "texto"

        def __init__(self):
            self.vio_la_prueba = False

        def available(self):
            return True

        def run(self, prompt, allow_write=False, cwd=None, **k):
            self.vio_la_prueba |= any(Path(cwd).glob("_prueba_oculta_*"))
            umbral = 15 if "15 %" in prompt else 20
            (Path(cwd) / "faro" / "alertas.py").write_text(
                f"def alerta_bateria(carga):\n    return carga < {umbral}\n", encoding="utf-8")
            return {"ok": True, "output": "implementado"}

    def test_con_memoria_resuelve_y_sin_memoria_no(self):
        tmp = Path(tempfile.mkdtemp())
        ad = self.Escritor()
        notas, tareas = evaluacion.cargar()
        tarea = next(t for t in tareas if t["id"] == "faro-implementa-alerta")
        try:
            with _sin_config(), patch.object(config, "_budget_reserve", return_value=True), \
                    patch.object(config, "_model_for", return_value=None), \
                    patch.object(config, "_reasoning_for", return_value=None), \
                    patch.object(sesiones, "_runs_dir", return_value=tmp / "runs"):
                con = evaluacion.correr(ad, tarea, notas, evaluacion.CORPUS, tmp, True)
                sin = evaluacion.correr(ad, tarea, notas, evaluacion.CORPUS, tmp, False)
        finally:
            evaluacion._borrar(tmp)
        self.assertEqual((con["resuelta"], sin["resuelta"]), (True, False), (con, sin))
        self.assertIn("return carga < 15", con["cambios"], "queda el diff del agente")
        self.assertNotIn(str(tmp), con["detalle"] + sin["detalle"])
        self.assertFalse(ad.vio_la_prueba, "la prueba oculta se copia despues")


class AislamientoTests(unittest.TestCase):
    """En eval el agente no ve el cerebro REAL: ni por MCP, ni por hooks, ni por
    sesiones guardadas. Con y sin memoria solo cambia el contexto del prompt."""

    def _argv(self, adaptador):
        r = Mock(returncode=0, stdout='{"result": "x"}', stderr="")
        with patch.object(agentes, "_launch", return_value=r) as lanzar:
            adaptador.run("hola", cwd=".")
        return lanzar.call_args[0][0]

    def test_codex_y_claude_aislados(self):
        with rutas.modo_evaluacion():
            codex = self._argv(agentes.CodexAdapter())
            claude = self._argv(agentes.ClaudeAdapter())
        self.assertIn("mcp_servers.soou.enabled=false", codex)
        self.assertIn("--ephemeral", codex)
        for flag in ("--strict-mcp-config", "--no-session-persistence"):
            self.assertIn(flag, claude)
        self.assertIn('{"disableAllHooks":true}', claude)
        self.assertNotIn(rutas.EVAL_ENV, os.environ)
        fuera = self._argv(agentes.ClaudeAdapter())
        self.assertNotIn("--strict-mcp-config", fuera)
        self.assertNotIn("--ephemeral", self._argv(agentes.CodexAdapter()))

    def test_con_y_sin_memoria_lanzan_el_mismo_comando(self):
        notas, tareas = evaluacion.cargar()
        tarea = next(t for t in tareas if t["id"] == "faro-umbral")
        tmp = Path(tempfile.mkdtemp())
        r = Mock(returncode=0, stdout='{"result": "x"}', stderr="")
        try:
            for adaptador in (agentes.ClaudeAdapter(), agentes.CodexAdapter()):
                with _sin_config(), rutas.modo_evaluacion(), \
                        patch.object(agentes, "_launch", return_value=r) as lanzar, \
                        patch.object(config, "_budget_reserve", return_value=True), \
                        patch.object(config, "_model_for", return_value=None), \
                        patch.object(config, "_reasoning_for", return_value=None), \
                        patch.object(sesiones, "_runs_dir", return_value=tmp / "runs"):
                    for memoria in (True, False):
                        evaluacion.correr(adaptador, tarea, notas, evaluacion.CORPUS, tmp,
                                          memoria)
                (con, kcon), (sin, ksin) = [(c[0][0], c[1]) for c in lanzar.call_args_list]
                quitar = lambda xs: [x.replace("-con", "-X").replace("-sin", "-X") for x in xs]
                self.assertEqual(quitar(con), quitar(sin), adaptador.name)
                self.assertIn("Contexto del cerebro", kcon["stdin_text"])
                self.assertNotIn("Contexto del cerebro", ksin["stdin_text"])
        finally:
            evaluacion._borrar(tmp)

    def test_los_hooks_de_soou_callan_en_evaluacion(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("soou", RAIZ / "soou.py")
        soou = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(soou)
        prohibido = Mock(side_effect=AssertionError("leyo el cerebro"))
        with rutas.modo_evaluacion(), patch.object(soou.indice, "compute_graph", prohibido), \
                patch.object(soou.config, "load_config", prohibido), \
                patch.object(soou.sesiones, "cmd_capture", prohibido):
            soou.cmd_context(Mock())
            soou.cmd_recall(Mock(text=["cual es el umbral de bateria"]))
            soou.destilado.cmd_session_end(Mock())

    def test_avisa_si_el_cerebro_real_tiene_datos_del_corpus(self):
        notas, tareas = evaluacion.cargar()
        marcas = evaluacion.marcadores(notas, tareas)
        self.assertIn("marea-turquesa-7731", marcas)
        self.assertIn("faro-umbral-bateria", marcas)
        self.assertNotIn("convencion-logs", marcas, "las genericas no cuentan")
        with tempfile.TemporaryDirectory() as tmp:
            wiki = Path(tmp) / "wiki"
            (wiki / "code").mkdir(parents=True)
            (wiki / "code" / "limpia.md").write_text("nada del corpus\n", encoding="utf-8")
            (wiki / "code" / "sucia.md").write_text("ver [[faro-umbral-bateria]]\n",
                                                    encoding="utf-8")
            self.assertEqual(evaluacion.corpus_en_cerebro_real(marcas, [wiki]),
                             ["wiki/code/sucia.md"])

    def test_las_rutas_del_temporal_no_llegan_al_informe(self):
        base = Path(tempfile.gettempdir()) / "soou-eval-abc"
        texto_ = f"File \"{base}{os.sep}faro\\x.py\" y {base.as_posix()}/faro"
        limpio = evaluacion._anonimo(texto_, base)
        self.assertNotIn(str(base), limpio)
        self.assertNotIn(base.as_posix(), limpio)
        self.assertIn("<tmp>", limpio)

    def test_se_guarda_tras_cada_corrida(self):
        class Otro(evaluacion.AgenteEco):
            name = "otro"
        vistos = []
        with _sin_config():
            inf = evaluacion.evaluar([evaluacion.AgenteEco(), Otro()], reanudacion=False,
                                     al_guardar=lambda i: vistos.append(
                                         {k: len(v["filas"]) for k, v in i["agentes"].items()}))
        n = 2 * inf["corpus"]["tareas"]
        self.assertEqual(list(inf["agentes"]), ["eco", "otro"], "no comparten directorio")
        self.assertEqual(len(vistos), 2 * n + 1, "una escritura por corrida y la final")
        self.assertEqual(vistos[0], {"eco": 1})
        self.assertEqual(vistos[-1], {"eco": n, "otro": n})
        self.assertTrue(all(a["completo"] for a in inf["agentes"].values()))
        self.assertNotIn("aislamiento", inf, "con eco no se lee el cerebro real")

    def test_evaluar_restaura_el_entorno(self):
        os.environ.pop(rutas.EVAL_ENV, None)
        with _sin_config(), open(os.devnull, "w") as nulo, contextlib.redirect_stdout(nulo):
            evaluacion.evaluar(reanudacion=False)
        self.assertNotIn(rutas.EVAL_ENV, os.environ)


if __name__ == "__main__":
    unittest.main()
