"""Paquete de contexto unico: proyecto, aislamiento entre proyectos, privadas,
reemplazadas, presupuesto y explicacion. Sin agentes y sin datos del usuario."""

import argparse
import contextlib
import io
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
from soou_core import config, contexto, indice, rutas  # noqa: E402

SECRETO = "obsidiana-confidencial"


def _nota(nombre, cuerpo, tipo="concept", **meta):
    # sin "_links": los enlaces se derivan del cuerpo (cubre note_links -> links_in)
    return {"name": nombre, "area": "code", "body": cuerpo,
            "path": rutas.WIKI / "code" / f"{nombre}.md" if rutas.WIKI else Path(f"{nombre}.md"),
            "meta": {"type": tipo, **meta}}


class ContextoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.previo = (rutas.ROOT, rutas.WIKI)
        rutas.ROOT, rutas.WIKI = self.tmp, self.tmp / "wiki"
        (self.tmp / "Alfa" / "src").mkdir(parents=True)
        self.notas = [
            _nota("proj-alfa", "Proyecto Alfa: lanzador de cohetes de prueba.", "project",
                  sources=["Alfa"]),
            _nota("proj-beta", "Proyecto Beta: otra plataforma.", "project", sources=["Beta"]),
            _nota("alfa-despliegue", "Despliegue del cohete alfa por etapas. [[proj-alfa]]",
                  "decision", source_session="sessions/raw/2026/11/01/session-x.md"),
            _nota("beta-despliegue", "Despliegue del cohete beta con otra receta. [[proj-beta]]"),
            _nota("cohete-teoria", "Teoria general del cohete y su despliegue. [[alfa-despliegue]] "
                  "[[beta-despliegue]]"),
            _nota("alfa-despliegue-viejo", "Despliegue del cohete alfa, version vieja. [[proj-alfa]]",
                  "decision", status="reemplazada", superseded_by="alfa-despliegue"),
            _nota("diario-privado", f"{SECRETO} despliegue del cohete alfa", private="true"),
        ]
        self.pila = contextlib.ExitStack()
        self.pila.enter_context(patch.object(config, "load_config",
                                             return_value={"private_paths": []}))

    def tearDown(self):
        self.pila.close()
        rutas.ROOT, rutas.WIKI = self.previo
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _paq(self, tarea, cwd=None, **kw):
        return contexto.armar_paquete(tarea, self.notas, cwd=cwd, **kw)

    def _nombres(self, paq):
        return [p.nombre for p in paq.piezas]

    def test_reconoce_el_proyecto_por_la_carpeta(self):
        paq = self._paq("despliegue del cohete alfa", cwd=self.tmp / "Alfa" / "src")
        self.assertEqual(paq.proyecto, "proj-alfa")
        self.assertEqual(paq.piezas[0].nombre, "proj-alfa")
        self.assertEqual(paq.piezas[0].razon, "resumen del proyecto")

    def test_las_notas_de_otro_proyecto_quedan_fuera_y_se_explica(self):
        paq = self._paq("como planificamos el despliegue de cohetes por etapas",
                        cwd=self.tmp / "Alfa")
        self.assertEqual(paq.modo, "tema · semillas + vecinos del grafo")
        self.assertNotIn("beta-despliegue", self._nombres(paq))
        self.assertIn(("beta-despliegue", "de otro proyecto (proj-beta)"), paq.excluidas)

    def test_otro_proyecto_entra_si_la_tarea_lo_nombra(self):
        paq = self._paq("despliegue del cohete en beta", cwd=self.tmp / "Alfa")
        pieza = next(p for p in paq.piezas if p.nombre == "beta-despliegue")
        self.assertIn("de otro proyecto (proj-beta), incluida por relevancia", pieza.razon)

    def test_las_notas_transversales_y_la_persona_no_se_aislan(self):
        self.notas += [
            _nota("proj-gama", "Proyecto Gama.", "project", sources=["Gama"]),
            _nota("cohete-comun", "Despliegue de cohetes en todos: [[proj-beta]] [[proj-gama]] "
                  "[[proj-delta]]"),
            _nota("proj-delta", "Proyecto Delta.", "project", sources=["Delta"]),
            _nota("mi-persona", "Despliegue de cohetes es lo mio. [[proj-beta]]", "persona"),
        ]
        per = contexto._pertenencia(indice.para_agentes(self.notas))
        self.assertEqual(per["cohete-comun"], set(), "cruza 3 proyectos: general")
        self.assertEqual(per["mi-persona"], set())
        self.assertEqual(per["beta-despliegue"], {"proj-beta"})
        self.assertEqual(per["proj-alfa"], {"proj-alfa"}, "una nota de proyecto es de el mismo")

    def test_un_enlace_desde_el_proyecto_no_vuelve_suya_a_una_nota(self):
        self.notas.append(_nota("guia-general", "Guia de despliegue."))
        self.notas[1]["body"] += " Ver [[guia-general]]."      # proj-beta la enlaza
        per = contexto._pertenencia(indice.para_agentes(self.notas))
        self.assertEqual(per["guia-general"], set())

    def test_sin_proyecto_no_hay_aislamiento(self):
        paq = self._paq("despliegue del cohete beta")
        self.assertIsNone(paq.proyecto)
        self.assertIn("beta-despliegue", self._nombres(paq))

    def test_las_reemplazadas_no_entran(self):
        paq = self._paq("despliegue del cohete alfa version vieja", cwd=self.tmp / "Alfa")
        self.assertNotIn("alfa-despliegue-viejo", self._nombres(paq))
        self.assertIn(("alfa-despliegue-viejo", "reemplazada por alfa-despliegue"), paq.excluidas)

    def test_las_privadas_nunca_llegan_al_agente(self):
        paq = self._paq("despliegue del cohete alfa")
        self.assertNotIn("diario-privado", self._nombres(paq))
        self.assertIn(("diario-privado", "privada"), paq.excluidas)
        self.assertNotIn(SECRETO, contexto.como_prompt(paq))
        self.assertNotIn(SECRETO, "\n".join(contexto.como_hook(paq)))
        self.assertNotIn(SECRETO, contexto.explicar(paq), "la explicacion no muestra su contenido")
        self.assertTrue(all(n["name"] != "diario-privado" for _, n in paq.ranking))

    def test_respeta_el_presupuesto(self):
        paq = self._paq("despliegue del cohete", presupuesto=100, recorte=60)
        self.assertLessEqual(paq.usados, 100)
        self.assertTrue(any(m.startswith("presupuesto") for _, m in paq.excluidas))

    def test_una_entidad_no_suma_vecinos(self):
        paq = self._paq("alfa-despliegue")
        self.assertEqual(paq.modo, "nota exacta")
        self.assertTrue(all(not p.razon.startswith("vecino") for p in paq.piezas))

    def test_explicar_muestra_razon_y_procedencia(self):
        texto = contexto.explicar(self._paq("despliegue del cohete alfa", cwd=self.tmp / "Alfa"))
        self.assertIn("Proyecto: proj-alfa", texto)
        self.assertIn("resumen del proyecto", texto)
        self.assertIn("source_session: sessions/raw/2026/11/01/session-x.md", texto)
        self.assertIn("Excluidas:", texto)

    def test_cmd_context(self):
        args = argparse.Namespace(tarea=["despliegue", "del", "cohete", "alfa"], explain=True,
                                  cwd=str(self.tmp / "Alfa"), presupuesto=None)
        salida = io.StringIO()
        with patch.object(indice, "load_notes", return_value=self.notas), \
                contextlib.redirect_stdout(salida):
            contexto.cmd_context(args)
        self.assertIn("Proyecto: proj-alfa", salida.getvalue())

    def test_presupuesto_configurable(self):
        with patch.object(config, "load_config", return_value={"context_budget": 900}):
            self.assertEqual(contexto.presupuesto(), 900)
        with patch.object(config, "load_config", return_value={"context_budget": "nada"}):
            self.assertEqual(contexto.presupuesto(), contexto.PRESUPUESTO)

    def test_note_links_sin_indice_no_se_recursa(self):
        self.assertEqual(indice.note_links(self.notas[2]), ["proj-alfa"])


class CliTests(unittest.TestCase):
    """La CLI arranca: un subcomando repetido rompe TODOS los comandos (hooks
    incluidos) y ningun test de modulo lo ve."""

    def test_el_parser_se_construye(self):
        import subprocess
        for argv in (["--help"], ["context", "--help"], ["agents", "--help"]):
            r = subprocess.run([sys.executable, str(RAIZ / "soou.py"), *argv],
                               capture_output=True, text=True, encoding="utf-8", timeout=60)
            self.assertEqual(r.returncode, 0, (argv, r.stderr[-400:]))


if __name__ == "__main__":
    unittest.main()
