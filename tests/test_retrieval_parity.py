#!/usr/bin/env python3
"""Tests de paridad del retrieval y el grafo (F14).

Red de seguridad ANTES de migrar el almacén a SQLite: fija el comportamiento
actual de `rank_notes` y `compute_graph` sobre un corpus de prueba FIJO
(tests/fixtures/wiki), como golden. Tras migrar a SQLite deben seguir verdes:
misma data + mismo algoritmo = mismo ranking.

Correr:  python -m unittest tests.test_retrieval_parity   (o este archivo directo)
"""
import unittest
import json
import importlib.util
from pathlib import Path

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent

# importar el core soou.py como módulo (tiene guard __main__)
_spec = importlib.util.spec_from_file_location("soou", ROOT / "soou.py")
soou = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(soou)

FIXTURES = TESTS / "fixtures" / "wiki"
TEST_DB = TESTS / "brain.test.db"
GOLDEN = TESTS / "golden_retrieval.json"

QUERIES = [
    "wind turbine pipeline",
    "solar panel inspection",
    "naval hull cascos",
    "retrieval bm25 ranking rrf",
    "postgres ports",
    "blade defect yolo",
]


def _point_to_fixtures():
    """Apunta el core al corpus de prueba (no al cerebro real, que muta)."""
    # tambien la raiz: load_notes suma las REGLAS del proyecto (schema.yaml,
    # AGENTS.md) y esas no son parte del corpus golden.
    soou.rutas.ROOT = TESTS / "fixtures"
    soou.rutas.WIKI = FIXTURES
    soou.rutas.DB_PATH = TEST_DB
    for suffix in ("", "-wal", "-shm"):
        f = Path(str(TEST_DB) + suffix)
        if f.exists():
            f.unlink()


def current_rankings():
    _point_to_fixtures()
    notes = soou.load_notes()
    out = {}
    for q in QUERIES:
        fused, _ = soou.rank_notes(q, notes)
        out[q] = [[n["name"], round(s, 6)] for s, n in fused[:6]]
    return out


class RetrievalParity(unittest.TestCase):

    def test_retrieval_ranking(self):
        rankings = current_rankings()
        if not GOLDEN.exists():
            GOLDEN.write_text(json.dumps(rankings, indent=2, ensure_ascii=False),
                              encoding="utf-8")
            self.skipTest("golden_retrieval.json generado (baseline) — vuelve a correr para comparar")
        golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
        self.assertEqual(rankings, golden,
                         "El ranking del retrieval cambió respecto al golden.")

    def test_graph_edges(self):
        _point_to_fixtures()
        _, nodes, edges = soou.compute_graph()
        self.assertEqual(len(nodes), 6, "deben ser 6 notas en el corpus de prueba")
        got = {(e["source"], e["target"]) for e in edges}
        expected = {
            ("alpha", "beta"), ("alpha", "gamma"), ("beta", "gamma"),
            ("delta", "alpha"), ("zeta", "alpha"), ("zeta", "beta"),
        }
        self.assertEqual(got, expected, "las aristas del grafo cambiaron")

    def test_adjacency_symmetric(self):
        _point_to_fixtures()
        notes = soou.load_notes()
        adj = soou.build_adjacency(notes)
        # la adyacencia es no dirigida: si alpha~beta, beta~alpha
        self.assertIn("beta", adj.get("alpha", set()))
        self.assertIn("alpha", adj.get("beta", set()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
