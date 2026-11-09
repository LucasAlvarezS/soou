"""Controles de envio: una nota privada no sale hacia ningun agente.

Captura (que entra al cerebro) se prueba en test_cosecha; aqui, recuperacion y
envio: recall, MCP, `query` y el contexto que `run` inyecta."""

import argparse
import contextlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("soou_mcp", ROOT / "soou_mcp.py")
mcp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mcp)
soou = mcp.brain
indice = soou.indice

SECRETO = "zafiro-confidencial"


def _nota(nombre, cuerpo, ruta, **meta):
    return {"name": nombre, "area": "personal", "body": cuerpo, "path": ruta,
            "meta": {"type": "concept", **meta}, "_links": []}


class PrivacidadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.previo = (soou.rutas.ROOT, soou.rutas.WIKI)
        soou.rutas.ROOT = self.tmp
        soou.rutas.WIKI = self.tmp / "wiki"
        w = soou.rutas.WIKI
        self.notas = [
            _nota("receta-publica", "zafiro: como se pule una gema", w / "personal" / "a.md"),
            _nota("salud-propia", f"{SECRETO} zafiro diagnostico", w / "personal" / "b.md",
                  private="true"),
            _nota("finanzas-casa", f"{SECRETO} zafiro cuentas", w / "privado" / "c.md"),
        ]
        self.cfg = {"private_paths": ["privado"], "brain_context": True}

    def tearDown(self):
        soou.rutas.ROOT, soou.rutas.WIKI = self.previo

    def _entorno(self, cfg=None):
        pila = contextlib.ExitStack()
        pila.enter_context(patch.object(soou.config, "load_config",
                                        return_value=cfg or self.cfg))
        pila.enter_context(patch.object(indice, "load_notes", return_value=self.notas))
        pila.enter_context(patch.object(soou, "load_notes", return_value=self.notas))
        return pila

    def test_marcada_o_en_carpeta_privada(self):
        with self._entorno():
            self.assertEqual([n["name"] for n in indice.para_agentes(self.notas)],
                             ["receta-publica"])

    def test_mcp_query_y_recall_no_las_devuelven(self):
        with self._entorno():
            for salida in (mcp.tool_query({"query": "zafiro"}),
                           mcp._TOOL_FUNCS["brain_recall"]({"prompt": "zafiro gema"})):
                self.assertIn("receta-publica", salida)
                self.assertNotIn(SECRETO, salida)

    def test_mcp_note_get_las_trata_como_inexistentes(self):
        with self._entorno(), patch.object(soou, "buscar_nota", return_value=self.notas[1]):
            salida = mcp.tool_note_get({"name": "salud-propia"})
        self.assertNotIn(SECRETO, salida)
        self.assertIn("No existe", salida)

    def test_el_contexto_de_run_no_las_incluye(self):
        with self._entorno():
            ctx = soou.harness._brain_context("zafiro")
        self.assertIn("receta-publica", ctx)
        self.assertNotIn(SECRETO, ctx)

    def test_query_las_oculta_salvo_que_las_pidas(self):
        for privadas, esperado in ((False, False), (True, True)):
            with self._entorno(), contextlib.redirect_stdout(io.StringIO()) as out:
                soou.cmd_query(argparse.Namespace(text=["zafiro"], limit=8, privadas=privadas))
            self.assertEqual(SECRETO in out.getvalue(), esperado)

    def test_brain_context_false_apaga_el_recall_automatico(self):
        cfg = {**self.cfg, "brain_context": False}
        with self._entorno(cfg), patch.object(soou.indice, "compute_graph") as grafo, \
                contextlib.redirect_stdout(io.StringIO()) as out:
            soou.cmd_recall(argparse.Namespace(text=["zafiro", "gema"]))
        grafo.assert_not_called()
        self.assertEqual(out.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
