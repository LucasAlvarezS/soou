"""`init` muestra antes lo que va a hacer, y puede quedarse en lo minimo.

Lo que NO debe pasar: que --preview cambie algo, que --minimal toque hooks,
MCP o el programador de tareas, o que una respuesta negativa siga adelante."""

import argparse
import contextlib
import io
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from soou_core import instalacion  # noqa: E402

PASOS = ("_bootstrap_workspace", "_install_sync_trigger", "_install_claude_hooks",
         "_install_mcp", "_install_shortcut")


def _args(**kw):
    base = dict(no_hooks=False, no_mcp=False, no_trigger=False, shortcut=False,
                interval=5, preview=False, minimal=False, yes=False)
    base.update(kw)
    return argparse.Namespace(**base)


class _Tty(io.StringIO):
    def isatty(self):
        return True


class InitTests(unittest.TestCase):
    def _correr(self, args, stdin=None, respuesta=None):
        pila = contextlib.ExitStack()
        devuelve = {"_bootstrap_workspace": [], "_install_mcp": []}   # el resto, texto
        mocks = {n: pila.enter_context(patch.object(instalacion, n,
                                                    return_value=devuelve.get(n, "ok")))
                 for n in PASOS}
        pila.enter_context(patch.object(instalacion.registro, "build_adapters", return_value=[]))
        pila.enter_context(patch.object(instalacion.consola, "mostrar_marca"))
        if stdin is not None:
            pila.enter_context(patch.object(sys, "stdin", stdin))
        if respuesta is not None:
            pila.enter_context(patch("builtins.input", return_value=respuesta))
        with pila, contextlib.redirect_stdout(io.StringIO()) as out:
            instalacion.cmd_init(args)
        return mocks, out.getvalue()

    def test_preview_no_cambia_nada_y_lista_cada_integracion(self):
        mocks, out = self._correr(_args(preview=True))
        for nombre, m in mocks.items():
            m.assert_not_called()
        for texto in ("settings.json", "config.toml", "SoouSync" if sys.platform == "win32"
                      else "crontab", "uninstall"):
            self.assertIn(texto, out)

    def test_minimal_solo_prepara_el_workspace(self):
        mocks, _ = self._correr(_args(minimal=True))
        mocks["_bootstrap_workspace"].assert_called_once()
        for nombre in PASOS[1:]:
            mocks[nombre].assert_not_called()

    def test_en_una_terminal_pide_confirmacion_y_un_no_cancela(self):
        mocks, out = self._correr(_args(), stdin=_Tty(), respuesta="n")
        for m in mocks.values():
            m.assert_not_called()
        self.assertIn("Cancelado", out)

    def test_en_una_terminal_un_si_sigue(self):
        mocks, _ = self._correr(_args(), stdin=_Tty(), respuesta="s")
        mocks["_install_claude_hooks"].assert_called_once()

    def test_yes_y_los_scripts_no_preguntan(self):
        with patch("builtins.input", side_effect=AssertionError("no deberia preguntar")):
            mocks, _ = self._correr(_args(yes=True), stdin=_Tty())
            mocks["_install_mcp"].assert_called_once()
            mocks, _ = self._correr(_args(), stdin=io.StringIO())     # sin terminal
            mocks["_install_mcp"].assert_called_once()


if __name__ == "__main__":
    unittest.main()
