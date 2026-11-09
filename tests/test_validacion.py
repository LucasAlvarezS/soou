"""Veredicto de los validadores: que se aprobo, que no se ejecuto y si hubo
verificacion funcional.

Lo que NO debe pasar: que un validador que no se pudo correr cuente como
aprobado, ni que revisar el diff se presente como prueba de que la tarea
funciona."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from soou_core import harness  # noqa: E402


class VeredictoTests(unittest.TestCase):
    def test_solo_el_diff_no_es_verificacion_funcional(self):
        veredicto, texto = harness._verificacion_de_validadores(
            [("git diff", True, "1 file changed"), ("py compile", True, "ok")])
        self.assertEqual(veredicto, "sin_verificacion_funcional")
        self.assertIn("aprobadas: git diff, py compile", texto)
        self.assertTrue(texto.startswith("parcial"))

    def test_una_prueba_aprobada_si_lo_es(self):
        veredicto, texto = harness._verificacion_de_validadores(
            [("git diff", True, ""), ("unittest", True, "OK")])
        self.assertEqual(veredicto, "funcional")
        self.assertTrue(texto.startswith("si"))

    def test_no_ejecutado_no_es_aprobado(self):
        veredicto, texto = harness._verificacion_de_validadores(
            [("git diff", True, ""), ("npm test", None, "no ejecutado: sin node_modules")])
        self.assertEqual(veredicto, "sin_verificacion_funcional",
                         "npm test no corrio: no puede contar como prueba aprobada")
        self.assertIn("no ejecutadas: npm test", texto)

    def test_un_fallo_manda(self):
        veredicto, texto = harness._verificacion_de_validadores(
            [("pytest", True, ""), ("npm lint", False, "2 errores")])
        self.assertEqual(veredicto, "fallo")
        self.assertIn("npm lint", texto)

    def test_sin_validadores(self):
        veredicto, texto = harness._verificacion_de_validadores([])
        self.assertEqual(veredicto, "sin_verificacion_funcional")
        self.assertTrue(texto.startswith("no"))

    def test_clases(self):
        for nombre, clase in [("git diff", "basica"), ("py compile", "basica"),
                              ("npm lint", "estatica"), ("npm typecheck", "estatica"),
                              ("npm test", "funcional"), ("pytest", "funcional"),
                              ("unittest", "funcional"), ("cargo test", "funcional"),
                              ("make check", "funcional"), ("ruff .", "estatica")]:
            self.assertEqual(harness._clase_validador(nombre), clase, nombre)

    def test_un_validador_que_no_se_puede_correr_queda_en_none(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(harness, "_detectar_validadores",
                             return_value=[("npm test", None, "npm no esta en el PATH")]):
            checks = {n: ok for n, ok, _ in harness._run_validators(tmp)}
        self.assertIsNone(checks["npm test"])


if __name__ == "__main__":
    unittest.main()
