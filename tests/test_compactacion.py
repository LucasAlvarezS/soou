#!/usr/bin/env python3
"""Pruebas de la compactacion: lo unico del sistema que QUITA material.

Se prueba sobre todo lo que NO debe pasar: que no toque notas pinned, que no
retire una sesion que todavia no fue sintetizada, que no borre nada (archiva), y
que la simulacion no escriba en disco.
"""

import argparse
import importlib.util
import json
import os
import tempfile
import time
import unittest
from pathlib import Path


TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent

_spec = importlib.util.spec_from_file_location("soou", ROOT / "soou.py")
soou = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(soou)


class EntornoCompact(unittest.TestCase):
    def setUp(self):
        self.previos = (soou.rutas.ROOT, soou.rutas.WIKI, soou.rutas.SESSIONS, soou.rutas.DB_PATH, soou.rutas.ARCHIVO)
        self.tmp = Path(tempfile.mkdtemp())
        soou.rutas.ROOT = self.tmp
        soou.rutas.WIKI = self.tmp / "wiki"
        soou.rutas.SESSIONS = self.tmp / "sessions" / "raw"
        soou.rutas.DB_PATH = self.tmp / "soou.db"
        soou.rutas.ARCHIVO = self.tmp / "archivo"
        (soou.rutas.WIKI / "code").mkdir(parents=True)
        soou.rutas.SESSIONS.mkdir(parents=True)

    def tearDown(self):
        (soou.rutas.ROOT, soou.rutas.WIKI, soou.rutas.SESSIONS,
         soou.rutas.DB_PATH, soou.rutas.ARCHIVO) = self.previos

    def nota(self, nombre, cuerpo="Cuerpo con bastante contenido. " * 12,
             tipo="concept", pinned=False):
        ruta = soou.rutas.WIKI / "code" / (nombre + ".md")
        ruta.write_text(
            "---\nname: " + nombre + "\narea: code\ntype: " + tipo + "\n"
            "sources: []\npinned: " + ("true" if pinned else "false") + "\n"
            "updated: 2026-09-20\n---\n\n" + cuerpo,
            encoding="utf-8")
        return ruta

    def crudo(self, nombre, estado="yes", dias=0):
        ruta = soou.rutas.SESSIONS / nombre
        ruta.write_text(
            "<!-- distilled: " + estado + " -->\n# Sesion\nFecha: 2026-09-01 10:00\n"
            "\n**assistant:** contenido de la sesion.\n", encoding="utf-8")
        if dias:
            viejo = time.time() - dias * 86400
            os.utime(ruta, (viejo, viejo))
        return ruta

    def nota_de(self, crudo_path, **kw):
        return self.nota(soou._nota_de_crudo(crudo_path), tipo="session", **kw)


class SenalesTests(EntornoCompact):
    def test_reconoce_un_stub(self):
        soou.load_notes()
        corta = soou.parse_note(self.nota("corta", cuerpo="dos palabras"))
        larga = soou.parse_note(self.nota("larga"))
        self.assertTrue(soou._stub(corta))
        self.assertFalse(soou._stub(larga))

    def test_reconoce_un_placeholder(self):
        n = soou.parse_note(self.nota("ph", cuerpo="TODO:INGEST " + "x " * 200))
        self.assertTrue(soou._stub(n))

    def test_lee_el_estado_del_crudo(self):
        for estado in ("yes", "lite", "no"):
            with self.subTest(estado=estado):
                self.assertEqual(soou._estado_crudo(self.crudo("session-a-" + estado + ".md",
                                                               estado)), estado)


class PlanTests(EntornoCompact):
    def test_retira_la_nota_de_una_sesion_ya_sintetizada(self):
        c = self.crudo("session-mcp-20260901-100000.md", "yes")
        self.nota_de(c)
        plan = soou.plan_de_compactacion()
        tipos = [a["tipo"] for a in plan["acciones"]]
        self.assertIn("nota-sesion", tipos)

    def test_no_retira_una_sesion_que_espera_nivel_b(self):
        c = self.crudo("session-mcp-20260901-100000.md", "lite")
        self.nota_de(c)
        plan = soou.plan_de_compactacion()
        self.assertEqual(plan["acciones"], [])
        self.assertTrue(any("Nivel B" in x for x in plan["revisar"]))

    def test_nunca_retira_una_nota_pinned(self):
        c = self.crudo("session-mcp-20260901-100000.md", "yes")
        self.nota_de(c, pinned=True)
        plan = soou.plan_de_compactacion()
        self.assertNotIn("nota-sesion", [a["tipo"] for a in plan["acciones"]])

    def test_archiva_un_crudo_viejo_ya_sintetizado(self):
        self.crudo("session-mcp-vieja.md", "yes", dias=90)
        plan = soou.plan_de_compactacion(dias_crudos=30)
        self.assertIn("crudo", [a["tipo"] for a in plan["acciones"]])

    def test_no_archiva_un_crudo_reciente(self):
        self.crudo("session-mcp-nueva.md", "yes", dias=2)
        plan = soou.plan_de_compactacion(dias_crudos=30)
        self.assertNotIn("crudo", [a["tipo"] for a in plan["acciones"]])

    def test_detecta_una_destilacion_huerfana(self):
        self.crudo("session-mcp-sinnota.md", "lite")
        plan = soou.plan_de_compactacion()
        self.assertEqual(len(plan["huerfanos"]), 1)

    def test_propone_fusionar_un_stub_con_su_gemela(self):
        self.nota("mi-concepto")
        self.nota("mi_concepto", cuerpo="corto")
        plan = soou.plan_de_compactacion()
        self.assertEqual(len(plan["fusiones"]), 1)
        self.assertEqual(plan["fusiones"][0]["superviviente"]["name"], "mi-concepto")

    def test_dos_duplicados_con_cuerpo_quedan_para_criterio_humano(self):
        self.nota("mi-concepto")
        self.nota("mi_concepto")
        plan = soou.plan_de_compactacion()
        self.assertEqual(plan["fusiones"], [])
        self.assertTrue(any("criterio" in x for x in plan["revisar"]))


class AplicarTests(EntornoCompact):
    def _args(self, **kw):
        base = {"apply": False, "dias": 30, "recuperar": False}
        base.update(kw)
        return argparse.Namespace(**base)

    def test_la_simulacion_no_mueve_nada(self):
        c = self.crudo("session-mcp-20260901-100000.md", "yes")
        ruta = self.nota_de(c)
        soou.cmd_compact(self._args())
        self.assertTrue(ruta.exists())
        self.assertFalse(soou.rutas.ARCHIVO.exists())

    def test_al_aplicar_archiva_en_vez_de_borrar(self):
        c = self.crudo("session-mcp-20260901-100000.md", "yes")
        ruta = self.nota_de(c)
        original = ruta.read_bytes()
        soou.cmd_compact(self._args(apply=True))
        self.assertFalse(ruta.exists())
        # el archivo va comprimido (disco acotado) pero sin perder nada
        recuperables = list(soou.rutas.ARCHIVO.rglob(ruta.name + ".gz"))
        self.assertEqual(len(recuperables), 1, "la nota tiene que seguir existiendo")
        import gzip
        self.assertEqual(gzip.decompress(recuperables[0].read_bytes()), original)

    def test_deja_manifiesto_de_lo_movido(self):
        c = self.crudo("session-mcp-20260901-100000.md", "yes")
        self.nota_de(c)
        soou.cmd_compact(self._args(apply=True))
        registros = [json.loads(l) for l in
                     (soou.rutas.ARCHIVO / "manifiesto.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertTrue(registros)
        self.assertIn("motivo", registros[0])

    def test_reapunta_los_enlaces_del_stub_retirado(self):
        self.nota("mi-concepto")
        self.nota("mi_concepto", cuerpo="corto")
        tercera = self.nota("otra", cuerpo="Habla de [[mi_concepto]] y de [[mi_concepto|alias]]. "
                                           + "relleno " * 40)
        soou.cmd_compact(self._args(apply=True))
        texto = tercera.read_text(encoding="utf-8")
        self.assertNotIn("[[mi_concepto]]", texto)
        self.assertIn("[[mi-concepto]]", texto)
        self.assertIn("[[mi-concepto|alias]]", texto)

    def test_recuperar_devuelve_la_sesion_a_la_cola(self):
        c = self.crudo("session-mcp-sinnota.md", "lite")
        self.assertEqual(len(soou.lite_pending()), 0)
        soou.cmd_compact(self._args(apply=True, recuperar=True))
        self.assertEqual(soou._estado_crudo(c), "no")
        self.assertEqual(len(soou.lite_pending()), 1)

    def test_recuperar_no_toca_el_contenido_del_crudo(self):
        c = self.crudo("session-mcp-sinnota.md", "lite")
        soou.cmd_compact(self._args(apply=True, recuperar=True))
        self.assertIn("contenido de la sesion", c.read_text(encoding="utf-8"))

    def test_sin_recuperar_no_resetea_nada(self):
        c = self.crudo("session-mcp-sinnota.md", "lite")
        soou.cmd_compact(self._args(apply=True))
        self.assertEqual(soou._estado_crudo(c), "lite")


if __name__ == "__main__":
    unittest.main()
