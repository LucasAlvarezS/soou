#!/usr/bin/env python3
"""Calidad del retrieval medida, no opinada.

Sobre el corpus fijo de tests/fixtures/wiki corre las consultas de
tests/fixtures/eval_retrieval.json y exige un PISO de metricas. Si un cambio
baja la calidad, el CI lo marca; si la sube, se actualiza el piso.

Medido al introducir el stemmer (2026-11-05):
  v1 (sin stemmer)  hit@1 0.57  recall@5 0.54  parafrasis recall@5 0.00
  v2 (stemmer es/en) hit@1 0.79  recall@5 0.71  parafrasis recall@5 0.42
"""

import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path


TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent

_spec = importlib.util.spec_from_file_location("soou", ROOT / "soou.py")
soou = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(soou)

PISO = {"lexica": 0.90, "parafrasis": 0.40}


class StemmerTests(unittest.TestCase):
    def test_plurales_y_genero_comparten_raiz(self):
        pares = [("aerogeneradores", "aerogenerador"), ("drones", "dron"),
                 ("fotovoltaicos", "fotovoltaica"), ("turbines", "turbine"),
                 ("inspecciones", "inspeccion"), ("ports", "port")]
        for a, b in pares:
            self.assertEqual(soou._raiz(a), soou._raiz(b), f"{a} ~ {b}")

    def test_tildes_no_separan(self):
        self.assertEqual(soou.tokenize("Inspección"), soou.tokenize("inspeccion"))

    def test_no_toca_ids_numeros_ni_cortas(self):
        for t in ("z01", "5432", "api-v2", "base", "dato"):
            self.assertEqual(soou._raiz(t), t)

    def test_la_ene_se_conserva(self):
        self.assertIn("año", soou._plegar("AÑO"))

    def test_etiquetas_se_muestran_legibles(self):
        formas = soou._superficies("Los aerogeneradores y el aerogenerador nuevo")
        self.assertIn(formas[soou._raiz("aerogeneradores")],
                      ("aerogeneradores", "aerogenerador"))


class PisoDeCalidadTests(unittest.TestCase):
    def setUp(self):
        self.previos = (soou.rutas.ROOT, soou.rutas.WIKI, soou.rutas.DB_PATH)
        self.tmp = Path(tempfile.mkdtemp())
        soou.rutas.ROOT = TESTS / "fixtures"
        soou.rutas.WIKI = TESTS / "fixtures" / "wiki"
        soou.rutas.DB_PATH = self.tmp / "eval.db"

    def tearDown(self):
        soou.rutas.ROOT, soou.rutas.WIKI, soou.rutas.DB_PATH = self.previos
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_recall_no_baja_del_piso(self):
        consultas = json.loads((TESTS / "fixtures" / "eval_retrieval.json")
                               .read_text(encoding="utf-8"))["consultas"]
        r = soou.evaluar_retrieval(consultas, soou.load_notes(), 5)
        for tipo, piso in PISO.items():
            got = r["por_tipo"][tipo]["recall@5"]
            self.assertGreaterEqual(got, piso, f"recall@5 {tipo} = {got:.2f} < {piso}")

    def test_indice_se_reconstruye_si_cambia_el_tokenizador(self):
        soou.load_notes()
        import sqlite3
        con = sqlite3.connect(str(soou.rutas.DB_PATH))
        con.execute("UPDATE notes SET tokens='[\"basura\"]'")
        con.execute("PRAGMA user_version = 1")
        con.commit(); con.close()
        notas = soou.load_notes()
        self.assertTrue(all(n["_tokens"] != ["basura"] for n in notas if n.get("_tokens")))


if __name__ == "__main__":
    unittest.main()
