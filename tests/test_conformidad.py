"""Manifiestos de agentes y prueba de conformidad, contra agentes falsos (CLI y
ACP). Ningun test lanza un agente real ni escribe en los datos del usuario."""

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
from soou_core import acp, agentes, config, conformidad, registro, rutas, sesiones  # noqa: E402

FIXTURES = RAIZ / "tests" / "fixtures"
ACP_FALSO = [sys.executable, str(FIXTURES / "agente_acp_falso.py")]
CLI_FALSO = [str(FIXTURES / "agente_falso.py"), "obediente", "{prompt}"]


class ManifiestosTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root_previo = rutas.ROOT
        rutas.ROOT = self.tmp
        (self.tmp / "agents").mkdir()

    def tearDown(self):
        rutas.ROOT = self.root_previo
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _manifiesto(self, nombre, contenido):
        f = self.tmp / "agents" / nombre
        f.write_text(contenido if isinstance(contenido, str) else json.dumps(contenido),
                     encoding="utf-8")

    def _construir(self, agentes_cfg=None):
        cfg = {"agents": agentes_cfg if agentes_cfg is not None else
               [{"name": "codex", "type": "codex"}]}
        with patch.object(config, "load_config", return_value=cfg):
            return registro.build_adapters(), registro._especificaciones()

    def test_un_manifiesto_valido_suma_un_agente(self):
        self._manifiesto("mi-acp.json", {"type": "acp", "command": ACP_FALSO})
        ads, _ = self._construir()
        self.assertEqual([a.name for a in ads], ["codex", "mi-acp"], "nombre = archivo")
        self.assertIsInstance(ads[1], acp.AcpAdapter)

    def test_los_invalidos_no_se_construyen_y_se_explican(self):
        self._manifiesto("roto.json", "{ no es json")
        self._manifiesto("sin-comando.json", {"type": "cmd"})
        self._manifiesto("lectura-mala.json", {"type": "cmd", "command": "x", "lectura": "confio"})
        self._manifiesto("tipo-raro.json", {"type": "magia"})
        ads, (_, malas, _) = self._construir()
        self.assertEqual([a.name for a in ads], ["codex"])
        motivos = dict(malas)
        self.assertIn("JSON invalido", motivos["roto.json"][0])
        self.assertIn("falta 'command'", motivos["sin-comando.json"][0])
        self.assertIn("'lectura'", motivos["lectura-mala.json"][0])
        self.assertIn("desconocido", motivos["tipo-raro.json"][0])

    def test_un_nombre_repetido_no_pisa_la_config(self):
        self._manifiesto("codex.json", {"type": "acp", "command": ACP_FALSO})
        ads, (_, malas, _) = self._construir()
        self.assertEqual(len(ads), 1)
        self.assertNotIsInstance(ads[0], acp.AcpAdapter)
        self.assertIn("ya hay un agente", malas[0][1][0])

    def test_campo_desconocido_avisa_sin_descartar(self):
        ads, (_, malas, avisos) = self._construir(
            [{"name": "mio", "type": "cmd", "command": "x", "lecura": "texto", "_nota": "ok"}])
        self.assertEqual([a.name for a in ads], ["mio"])
        self.assertEqual((malas, avisos), ([], [("mio", ["lecura"])]))

    def test_la_config_de_ejemplo_es_valida(self):
        ejemplo = json.loads((RAIZ / "soou.config.example.json").read_text(encoding="utf-8"))
        for spec in ejemplo["agents"]:
            self.assertEqual(registro.validar_spec(spec), [], spec.get("name"))
            self.assertEqual(registro.campos_desconocidos(spec), [], spec.get("name"))


class ConformidadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.pila = ExitStack()
        for obj, nombre, kw in [
            (config, "_budget_reserve", {"return_value": True}),
            (config, "_model_for", {"return_value": None}),
            (config, "_reasoning_for", {"return_value": None}),
            (sesiones, "_runs_dir", {"return_value": self.tmp / "runs"}),
        ]:
            self.pila.enter_context(patch.object(obj, nombre, **kw))

    def tearDown(self):
        self.pila.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _casos(self, ad, **kw):
        return {c: (e, d) for c, e, d in conformidad.evaluar(ad, **kw)}

    def test_agente_acp_conforme(self):
        casos = self._casos(acp.AcpAdapter("falso", ACP_FALSO))
        for caso in ("arranque", "lectura", "lectura_no_escribe", "escritura", "timeout",
                     "normalizacion"):
            self.assertEqual(casos[caso][0], "ok", (caso, casos[caso]))

    def test_cli_sin_lectura_declarada_se_rechaza_y_es_conforme(self):
        ad = agentes.GenericAdapter("cli", sys.executable, CLI_FALSO)
        casos = self._casos(ad)
        self.assertEqual(casos["lectura"][0], "ok")
        self.assertIn("se rechaza", casos["lectura"][1])
        self.assertEqual(casos["lectura_no_escribe"][0], "no_aplica")
        self.assertEqual(casos["escritura"][0], "ok")
        self.assertEqual(casos["timeout"][0], "ok")

    def test_una_lectura_declarada_en_falso_se_detecta(self):
        # declara aprobacion_cli pero escribe igual: la conformidad lo atrapa
        ad = agentes.GenericAdapter("mentiroso", sys.executable, CLI_FALSO, lectura="aprobacion_cli")
        casos = self._casos(ad, con_timeout=False)
        self.assertEqual(casos["lectura"][0], "ok")
        self.assertEqual(casos["lectura_no_escribe"][0], "fallo")
        self.assertIn("no se cumple", casos["lectura_no_escribe"][1])
        self.assertEqual(casos["timeout"][0], "no_ejecutado")

    def test_si_no_arranca_no_se_ejecuta_nada(self):
        ad = acp.AcpAdapter("roto", [sys.executable, "-c", "import sys; sys.exit(3)"])
        casos = self._casos(ad)
        self.assertEqual(casos["arranque"][0], "fallo")
        self.assertTrue(all(e == "no_ejecutado" for c, (e, _) in casos.items() if c != "arranque"))

    def test_los_intentos_no_ensucian_el_routing_real(self):
        conformidad.evaluar(acp.AcpAdapter("falso", ACP_FALSO), con_timeout=False)
        trazas = [json.loads(l) for l in
                  (self.tmp / "runs" / "trazas.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertTrue(trazas)
        self.assertEqual({t["soou.level"] for t in trazas}, {"conformidad"})
        self.assertEqual({t["soou.mode"] for t in trazas}, {"conformidad"})

    def test_cli_sale_con_codigo_si_falla(self):
        ad = agentes.GenericAdapter("mentiroso", sys.executable, CLI_FALSO, lectura="aprobacion_cli")
        args = type("A", (), {"conformance": "mentiroso", "sin_timeout": True})()
        with patch.object(registro, "build_adapters", return_value=[ad]):
            with self.assertRaises(SystemExit) as e:
                conformidad.cmd_agents(args)
        self.assertEqual(e.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
