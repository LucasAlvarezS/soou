#!/usr/bin/env python3
"""Pruebas de las reglas como notas, la escritura de notas y el resumen de salud.

La escritura es la primera via por la que un agente puede MODIFICAR el cerebro,
asi que lo que mas se prueba es lo que debe rechazar.
"""

import importlib.util
import tempfile
import unittest
from pathlib import Path


TESTS = Path(__file__).resolve().parent
RAIZ = TESTS.parent

_spec = importlib.util.spec_from_file_location("soou", RAIZ / "soou.py")
soou = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(soou)


class EntornoCerebro(unittest.TestCase):
    def setUp(self):
        self.previos = (soou.rutas.ROOT, soou.rutas.WIKI, soou.rutas.DB_PATH)
        self.tmp = Path(tempfile.mkdtemp())
        soou.rutas.ROOT = self.tmp
        soou.rutas.WIKI = self.tmp / "wiki"
        soou.rutas.DB_PATH = self.tmp / "soou.db"
        (soou.rutas.WIKI / "code").mkdir(parents=True)

    def tearDown(self):
        soou.rutas.ROOT, soou.rutas.WIKI, soou.rutas.DB_PATH = self.previos

    def nota(self, nombre, cuerpo="Contenido de prueba con suficiente largo. " * 6,
             tipo="concept", pinned=False, area="code"):
        destino = soou.rutas.WIKI / area
        destino.mkdir(parents=True, exist_ok=True)
        ruta = destino / (nombre + ".md")
        ruta.write_text(
            "---\nname: " + nombre + "\narea: " + area + "\ntype: " + tipo + "\n"
            "sources: []\npinned: " + ("true" if pinned else "false") + "\n"
            "updated: 2026-01-01\n---\n\n" + cuerpo, encoding="utf-8")
        return ruta

    def reglas(self):
        (self.tmp / "schema.yaml").write_text(
            "areas:\n  code: tecnico\nsintesis:\n  - Enlaza con [[nombre]] liberalmente.\n"
            "contradicciones:\n  politica: registrar-no-inferir\n", encoding="utf-8")
        (self.tmp / "agentes-cerebro.md").write_text(
            "# Instrucciones\nConsulta el cerebro con brain_recall antes de responder.\n",
            encoding="utf-8")


class ReglasComoNotasTests(EntornoCerebro):
    def test_las_reglas_entran_al_cerebro(self):
        self.reglas()
        nombres = {n["name"] for n in soou.load_notes()}
        self.assertIn("regla-schema", nombres)
        self.assertIn("regla-agentes", nombres)

    def test_son_de_solo_lectura_por_estar_pinned(self):
        self.reglas()
        regla = soou.buscar_nota("regla-schema")
        self.assertEqual(regla["meta"]["pinned"], "true")
        self.assertEqual(regla["meta"]["type"], "rule")

    def test_no_aportan_aristas_ilustrativas(self):
        self.reglas()
        regla = soou.buscar_nota("regla-schema")
        self.assertEqual(soou.note_links(regla), [],
                         "los [[x]] de schema.yaml son ejemplos, no aristas")

    def test_se_pueden_recuperar_por_busqueda(self):
        self.reglas()
        notas = soou.load_notes()
        fusionadas, _ = soou.rank_notes("politica ante contradicciones", notas)
        self.assertIn("regla-schema", [n["name"] for _, n in fusionadas])

    def test_sin_los_archivos_no_falla(self):
        self.assertEqual(soou._notas_de_reglas(), [])

    def test_una_regla_no_cuenta_como_contradiccion(self):
        self.reglas()
        self.assertEqual(soou.resumen_salud()["contradictions"], [])


class GuardarNotaTests(EntornoCerebro):
    def test_crea_una_nota_nueva(self):
        ok, mensaje = soou.guardar_nota("mi-idea", "Cuerpo de la idea.", area="code")
        self.assertTrue(ok, mensaje)
        creada = soou.buscar_nota("mi-idea")
        self.assertEqual(creada["meta"]["type"], "concept")
        self.assertEqual(creada["meta"]["area"], "code")

    def test_actualiza_conservando_el_frontmatter(self):
        self.nota("existente")
        ok, _ = soou.guardar_nota("existente", "Cuerpo corregido.")
        self.assertTrue(ok)
        actualizada = soou.buscar_nota("existente")
        self.assertIn("Cuerpo corregido", actualizada["body"])
        self.assertEqual(actualizada["meta"]["area"], "code", "no debe migrar de area")
        self.assertNotEqual(actualizada["meta"]["updated"], "2026-01-01")

    def test_se_niega_a_pisar_una_nota_pinned(self):
        self.nota("corregida-a-mano", pinned=True)
        ok, mensaje = soou.guardar_nota("corregida-a-mano", "intento pisarla")
        self.assertFalse(ok)
        self.assertIn("pinned", mensaje)
        self.assertNotIn("intento pisarla", soou.buscar_nota("corregida-a-mano")["body"])

    def test_se_niega_a_pisar_una_regla(self):
        self.reglas()
        ok, mensaje = soou.guardar_nota("regla-schema", "intento pisar la regla")
        self.assertFalse(ok)
        self.assertIn("pinned", mensaje)

    def test_rechaza_un_area_invalida(self):
        ok, mensaje = soou.guardar_nota("x", "cuerpo", area="finanzas")
        self.assertFalse(ok)
        self.assertIn("area invalida", mensaje)

    def test_rechaza_un_tipo_invalido(self):
        ok, mensaje = soou.guardar_nota("x", "cuerpo", tipo="apunte")
        self.assertFalse(ok)
        self.assertIn("tipo invalido", mensaje)

    def test_rechaza_nombre_o_cuerpo_vacios(self):
        self.assertFalse(soou.guardar_nota("", "cuerpo")[0])
        self.assertFalse(soou.guardar_nota("x", "   ")[0])

    def test_normaliza_el_nombre(self):
        ok, _ = soou.guardar_nota("Mi Idea Genial", "cuerpo", area="code")
        self.assertTrue(ok)
        self.assertIsNotNone(soou.buscar_nota("mi-idea-genial"))

    def test_pinned_protege_lo_recien_escrito(self):
        soou.guardar_nota("decision", "La primera version.", area="code", pinned=True)
        ok, _ = soou.guardar_nota("decision", "otra version")
        self.assertFalse(ok)


class ResumenSaludTests(EntornoCerebro):
    def test_detecta_un_enlace_roto(self):
        self.nota("origen", cuerpo="Apunta a [[inexistente]]. " + "relleno " * 30)
        rotos = soou.resumen_salud()["broken"]
        self.assertEqual(len(rotos), 1)
        self.assertEqual(rotos[0]["to"], "inexistente")

    def test_detecta_una_huerfana(self):
        self.nota("sola")
        self.assertIn("sola", soou.resumen_salud()["orphans"])

    def test_no_cuenta_las_reglas_como_huerfanas(self):
        self.reglas()
        self.assertNotIn("regla-schema", soou.resumen_salud()["orphans"])

    def test_detecta_posibles_duplicados(self):
        self.nota("mi-concepto")
        self.nota("mi_concepto")
        self.assertEqual(len(soou.resumen_salud()["duplicates"]), 1)

    def test_cuenta_el_total(self):
        self.nota("una")
        self.nota("otra")
        self.assertEqual(soou.resumen_salud()["total"], 2)


if __name__ == "__main__":
    unittest.main()
