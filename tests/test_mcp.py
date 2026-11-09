#!/usr/bin/env python3
"""Pruebas del servidor MCP: acotado de salida, captura sin perdida y protocolo."""

import importlib.util
import tempfile
import unittest
from pathlib import Path


TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent

_spec = importlib.util.spec_from_file_location("soou_mcp", ROOT / "soou_mcp.py")
mcp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mcp)


class AcotadoDeSalidaTests(unittest.TestCase):
    def test_limit_cae_al_default_si_es_invalido(self):
        for crudo in (None, "", "abc", object()):
            with self.subTest(crudo=crudo):
                self.assertEqual(mcp._clamp_limit(crudo), mcp.QUERY_LIMIT_DEFAULT)

    def test_limit_se_acota_al_rango(self):
        self.assertEqual(mcp._clamp_limit(100000), mcp.QUERY_LIMIT_MAX)
        self.assertEqual(mcp._clamp_limit(-5), 1)
        self.assertEqual(mcp._clamp_limit(3), 3)

    def test_los_enlaces_se_deduplican(self):
        nota = {"name": "n", "area": "code", "body": "x", "meta": {},
                "_links": ["alfa", "beta", "alfa"]}
        self.assertEqual(mcp._links_resumidos(nota), "alfa, beta")

    def test_los_enlaces_se_acotan_y_avisan(self):
        nota = {"name": "hub", "area": "code", "body": "x", "meta": {},
                "_links": [f"c-{i:03d}" for i in range(200)]}
        salida = mcp._links_resumidos(nota)
        self.assertEqual(salida.count(","), mcp.LINKS_PER_NOTE_MAX - 1)
        self.assertIn("mas)", salida)

    def test_una_nota_sin_enlaces_no_imprime_cola(self):
        nota = {"name": "hoja", "area": "code", "body": "cuerpo", "meta": {}, "_links": []}
        self.assertNotIn("->", mcp._fmt(nota))


class CapturaTests(unittest.TestCase):
    def setUp(self):
        self.previos = (mcp.brain.ROOT, mcp.brain.SESSIONS)
        self.tmp = Path(tempfile.mkdtemp())
        mcp.brain.ROOT = self.tmp
        mcp.brain.SESSIONS = self.tmp / "raw"

    def tearDown(self):
        mcp.brain.ROOT, mcp.brain.SESSIONS = self.previos

    def _archivos(self):
        return sorted(mcp.brain.SESSIONS.rglob("session-mcp-*.md"))

    def test_rechaza_un_area_invalida(self):
        salida = mcp.tool_capture({"text": "algo", "area": "finanzas"})
        self.assertIn("area invalida", salida)
        self.assertEqual(self._archivos(), [])

    def test_el_area_valida_queda_en_la_cabecera(self):
        mcp.tool_capture({"text": "algo", "area": "code"})
        self.assertIn("Area: code", self._archivos()[0].read_text(encoding="utf-8"))

    def test_conserva_el_texto_multilinea(self):
        texto = "Primera decision." + chr(10) + chr(10) + "Segunda decision." + chr(10) + "Tercera."
        mcp.tool_capture({"text": texto, "title": "t"})
        datos = mcp.brain._parse_session_raw(self._archivos()[0])
        self.assertEqual(len(datos["assistant"]), 3)

    def test_capturas_del_mismo_segundo_no_se_pisan(self):
        for i in range(3):
            mcp.tool_capture({"text": f"aporte {i}", "title": f"t{i}"})
        self.assertEqual(len(self._archivos()), 3)

    def test_avisa_cuando_recorta(self):
        salida = mcp.tool_capture({"text": "z" * (mcp.CAPTURE_MAX_CHARS + 10)})
        self.assertIn("AVISO", salida)

    def test_un_texto_vacio_no_crea_archivo(self):
        self.assertIn("vacio", mcp.tool_capture({"text": "   "}))
        self.assertEqual(self._archivos(), [])


class ProtocoloTests(unittest.TestCase):
    def setUp(self):
        self.enviados = []
        self.send_previo = mcp.send
        mcp.send = self.enviados.append

    def tearDown(self):
        mcp.send = self.send_previo

    def _initialize(self, version):
        mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": version, "capabilities": {}}})
        return self.enviados[-1]["result"]["protocolVersion"]

    def test_responde_la_version_que_implementa(self):
        self.assertEqual(self._initialize("2025-06-18"), mcp.PROTOCOL_VERSION)

    def test_acepta_la_version_soportada(self):
        self.assertEqual(self._initialize(mcp.PROTOCOL_VERSION), mcp.PROTOCOL_VERSION)

    def test_una_tool_desconocida_responde_error(self):
        mcp._run_tool(9, {"name": "no-existe", "arguments": {}})
        self.assertIn("error", self.enviados[-1])

    def test_el_esquema_declara_el_tope_de_limit(self):
        esquema = next(t for t in mcp.TOOLS if t["name"] == "brain_query")
        self.assertEqual(esquema["inputSchema"]["properties"]["limit"]["maximum"],
                         mcp.QUERY_LIMIT_MAX)

    def test_orchestrate_avisa_sus_efectos(self):
        esquema = next(t for t in mcp.TOOLS if t["name"] == "brain_orchestrate")
        self.assertIn("execute=true", esquema["description"])


if __name__ == "__main__":
    unittest.main()
