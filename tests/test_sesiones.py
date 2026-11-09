#!/usr/bin/env python3
"""Pruebas del pipeline de sesiones: identificadores, fuente, area y enlaces.

Cubre el defecto que la suite NO pillo en su momento: el identificador de sesion
se truncaba y todas las capturas del MCP destilaban al mismo nombre de nota.
"""

import importlib.util
import tempfile
import unittest
from pathlib import Path


TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent

_spec = importlib.util.spec_from_file_location("soou", ROOT / "soou.py")
soou = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(soou)


class IdentificadorDeSesionTests(unittest.TestCase):
    def test_capturas_del_mismo_segundo_dan_ids_distintos(self):
        nombres = ["session-mcp-20260920-173932.md",
                   "session-mcp-20260920-173932-1.md",
                   "session-mcp-20260920-173932-2.md"]
        ids = [soou._sid_of_session(n) for n in nombres]
        self.assertEqual(len(set(ids)), 3, f"ids colisionados: {ids}")

    def test_el_id_es_determinista(self):
        nombre = "session-mcp-20260920-173932-1.md"
        self.assertEqual(soou._sid_of_session(nombre), soou._sid_of_session(nombre))

    def test_un_id_corto_no_se_altera(self):
        self.assertEqual(soou._sid_of_session("session-abc123.md"), "abc123")

    def test_id_largo_se_acota(self):
        largo = "session-" + ("x" * 80) + ".md"
        self.assertLessEqual(len(soou._sid_of_session(largo)), soou._SID_MAX + 8)

    def test_la_fuente_sale_del_prefijo(self):
        casos = {"session-mcp-2026.md": "mcp",
                 "session-run-2026.md": "run",
                 "session-codex-2026.md": "codex",
                 "session-abc123.md": "claude-code"}
        for nombre, esperado in casos.items():
            with self.subTest(nombre=nombre):
                self.assertEqual(soou._source_of_session(nombre), esperado)


class CabeceraDeSesionTests(unittest.TestCase):
    def _sesion(self, carpeta, cabecera_area=True):
        ruta = Path(carpeta) / "session-mcp-20260920-100000.md"
        area = "Area: code\n" if cabecera_area else ""
        ruta.write_text(
            "<!-- distilled: no -->\n"
            "# Captura MCP prueba\n"
            "Fecha: 2026-09-20 10:00\n"
            "Fuente: mcp brain_capture\n"
            + area +
            "\n**user:** prueba\n"
            "\n**assistant:** Primera linea del aporte.\n"
            "\n**assistant:** Segunda linea del aporte.\n",
            encoding="utf-8")
        return ruta

    def test_lee_el_area_declarada(self):
        with tempfile.TemporaryDirectory() as tmp:
            datos = soou._parse_session_raw(self._sesion(tmp))
        self.assertEqual(datos["area"], "code")

    def test_sin_area_declarada_queda_en_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            datos = soou._parse_session_raw(self._sesion(tmp, cabecera_area=False))
        self.assertIsNone(datos["area"])

    def test_conserva_todas_las_lineas_del_asistente(self):
        with tempfile.TemporaryDirectory() as tmp:
            datos = soou._parse_session_raw(self._sesion(tmp))
        self.assertEqual(len(datos["assistant"]), 2)


class DestilacionPorAreaTests(unittest.TestCase):
    def setUp(self):
        self.previos = (soou.rutas.WIKI, soou.rutas.SESSIONS, soou.rutas.DB_PATH, soou.rutas.ROOT)
        self.tmp = Path(tempfile.mkdtemp())
        soou.rutas.ROOT = self.tmp
        soou.rutas.WIKI = self.tmp / "wiki"
        soou.rutas.SESSIONS = self.tmp / "raw"
        soou.rutas.DB_PATH = self.tmp / "soou.db"
        soou.rutas.WIKI.mkdir(parents=True)
        soou.rutas.SESSIONS.mkdir(parents=True)

    def tearDown(self):
        soou.rutas.WIKI, soou.rutas.SESSIONS, soou.rutas.DB_PATH, soou.rutas.ROOT = self.previos

    def _sesion(self, nombre, area=None):
        ruta = soou.rutas.SESSIONS / nombre
        cabecera = ("Area: " + area + "\n") if area else ""
        ruta.write_text(
            "<!-- distilled: no -->\n# Captura\nFecha: 2026-09-20 10:00\n"
            "Fuente: mcp brain_capture\n" + cabecera +
            "\n**user:** prueba\n"
            "\n**assistant:** Se decidio migrar el indice del cerebro a SQLite embebido.\n",
            encoding="utf-8")
        return ruta

    def test_el_area_declarada_manda(self):
        nombre = soou.distill_lite_one(self._sesion("session-mcp-a.md", "code"), [], [])
        self.assertTrue((soou.rutas.WIKI / "code" / (nombre + ".md")).exists())

    def test_sin_area_declarada_se_infiere(self):
        nombre = soou.distill_lite_one(self._sesion("session-mcp-b.md"), [], [])
        self.assertTrue((soou.rutas.WIKI / "work" / (nombre + ".md")).exists())

    def test_dos_capturas_del_mismo_segundo_no_se_pisan(self):
        a = soou.distill_lite_one(self._sesion("session-mcp-20260920-100000.md", "code"), [], [])
        b = soou.distill_lite_one(self._sesion("session-mcp-20260920-100000-1.md", "code"), [], [])
        self.assertNotEqual(a, b)
        self.assertEqual(len(list((soou.rutas.WIKI / "code").glob("sesion-*.md"))), 2)


class EnlacesTests(unittest.TestCase):
    def test_links_in_deduplica_conservando_orden(self):
        cuerpo = "Ver [[beta]], luego [[alfa]] y de nuevo [[beta]] y [[alfa]]."
        self.assertEqual(soou.links_in(cuerpo), ["beta", "alfa"])

    def test_links_in_ignora_codigo_inline(self):
        self.assertEqual(soou.links_in("literal `[[x]]` y real [[y]]"), ["y"])

    def test_links_in_resuelve_el_alias(self):
        self.assertEqual(soou.links_in("[[nombre|texto visible]]"), ["nombre"])

    def test_el_grafo_no_duplica_aristas(self):
        previos = (soou.rutas.WIKI, soou.rutas.DB_PATH)
        tmp = Path(tempfile.mkdtemp())
        soou.rutas.WIKI, soou.rutas.DB_PATH = tmp / "wiki", tmp / "soou.db"
        (soou.rutas.WIKI / "code").mkdir(parents=True)
        try:
            for nombre, cuerpo in (("destino", "Nota destino."),
                                   ("origen", "[[destino]] y [[destino]] y [[destino]].")):
                (soou.rutas.WIKI / "code" / (nombre + ".md")).write_text(
                    "---\nname: " + nombre + "\narea: code\ntype: concept\n"
                    "sources: []\npinned: false\nupdated: 2026-09-20\n---\n\n" + cuerpo,
                    encoding="utf-8")
            _, nodos, aristas = soou.compute_graph()
            hacia_destino = [a for a in aristas if a["target"] == "destino"]
            self.assertEqual(len(hacia_destino), 1)
            self.assertEqual(next(n["indeg"] for n in nodos if n["id"] == "destino"), 1)
        finally:
            soou.rutas.WIKI, soou.rutas.DB_PATH = previos


if __name__ == "__main__":
    unittest.main()
