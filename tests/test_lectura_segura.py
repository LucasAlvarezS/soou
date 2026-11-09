"""Lectura segura y lanzamiento seguro de agentes.

Lo que NO debe pasar: que un pedido de solo lectura use un comando que escribe,
o que texto libre (el prompt) llegue a la linea de comandos de cmd.exe."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from soou_core import agentes  # noqa: E402

HOSTILES = ['di "hola" & del *', "%PATH% y %USERPROFILE%", "linea uno\nlinea dos",
            "acentos: canción, ñandú, 日本語", "a | b > c < d ^ e !x!",
            r"C:\Program Files (x86)\con espacios\archivo.txt"]


class _Proceso:
    def __init__(self, out="listo"):
        self.returncode, self.stdout, self.stderr = 0, out, ""


class RecetasTests(unittest.TestCase):
    def test_ninguna_receta_lee_con_el_comando_de_escritura(self):
        for nombre, r in agentes.RECIPE_REGISTRY.items():
            with self.subTest(nombre):
                if r.get("read") is None:
                    continue
                self.assertNotEqual(r["read"], r["write"])
                self.assertIn(r.get("lectura"),
                              ("sandbox_os", "aprobacion_cli", "no_verificada"),
                              "cada modo lectura declara que restriccion aplica")

    def test_sin_modo_lectura_se_rechaza_sin_lanzar(self):
        ad = agentes.RecipeAdapter("goose", agentes.RECIPE_REGISTRY["goose"])
        with patch.object(agentes, "_launch") as lanzar:
            res = ad.run("lee el repo", allow_write=False)
        lanzar.assert_not_called()
        self.assertFalse(res["ok"])
        self.assertEqual(res["estado"], "rechazado")

    def test_lectura_no_verificada_se_rechaza(self):
        ad = agentes.RecipeAdapter("crush", agentes.RECIPE_REGISTRY["crush"])
        with patch.object(agentes, "_launch") as lanzar:
            res = ad.run("lee", allow_write=False)
        lanzar.assert_not_called()
        self.assertEqual(res["estado"], "rechazado")
        self.assertIn("no esta verificado", res["output"])

    def test_el_usuario_puede_declarar_la_restriccion(self):
        receta = {**agentes.RECIPE_REGISTRY["crush"], "lectura": "aprobacion_cli"}
        ad = agentes.RecipeAdapter("crush", receta)
        with patch.object(agentes, "_launch", return_value=_Proceso()) as lanzar:
            res = ad.run("lee", allow_write=False)
        self.assertTrue(res["ok"])
        self.assertNotIn("--yolo", lanzar.call_args[0][0])

    def test_receta_con_prompt_por_stdin(self):
        receta = {"bins": ["x"], "write": ["run", "{prompt}"], "read": ["ask", "{prompt}"],
                  "lectura": "aprobacion_cli", "prompt_via": "stdin"}
        ad = agentes.RecipeAdapter("x", receta)
        with patch.object(agentes, "_launch", return_value=_Proceso()) as lanzar:
            ad.run('texto "libre" & %X%', allow_write=False)
        argv, kw = lanzar.call_args[0][0], lanzar.call_args[1]
        self.assertEqual(argv, ["x", "ask"])
        self.assertEqual(kw["stdin_text"], 'texto "libre" & %X%')

    def test_comando_generico_sin_restriccion_no_lee(self):
        ad = agentes.GenericAdapter("mio", "mio", ["{prompt}"])
        with patch.object(agentes, "_launch") as lanzar:
            res = ad.run("lee", allow_write=False)
        lanzar.assert_not_called()
        self.assertEqual(res["estado"], "rechazado")


class ClaudeTests(unittest.TestCase):
    def _argv(self, escritura):
        with patch.object(agentes, "_launch", return_value=_Proceso('{"result": "ok"}')) as l:
            agentes.ClaudeAdapter().run('prompt "con" & cosas', allow_write=escritura)
        return l.call_args[0][0], l.call_args[1]

    def test_el_prompt_va_por_stdin(self):
        argv, kw = self._argv(False)
        self.assertNotIn('prompt "con" & cosas', argv)
        self.assertEqual(kw["stdin_text"], 'prompt "con" & cosas')

    def test_en_lectura_no_se_aceptan_ediciones(self):
        argv, _ = self._argv(False)
        self.assertNotIn("acceptEdits", argv)
        i = argv.index("--disallowedTools")
        self.assertTrue({"Write", "Edit", "Bash"} <= set(argv[i + 1:]))

    def test_en_escritura_si(self):
        argv, _ = self._argv(True)
        self.assertIn("acceptEdits", argv)
        self.assertNotIn("--disallowedTools", argv)


class LanzamientoTests(unittest.TestCase):
    def test_los_prompts_hostiles_llegan_literales_a_un_ejecutable(self):
        ad = agentes.GenericAdapter(
            "eco", sys.executable,
            ["-c", "import sys; sys.stdout.buffer.write(sys.argv[1].encode('utf-8'))",
             "{prompt}"], lectura="aprobacion_cli")
        for prompt in HOSTILES:
            with self.subTest(prompt=prompt):
                res = ad.run(prompt, allow_write=False)
                self.assertTrue(res["ok"], res["output"])
                self.assertEqual(res["output"], prompt)

    def test_lista_blanca_de_cmd(self):
        for bueno in ["--model", "gpt-5", r"C:\Users\Con Espacio\x.txt", "a=b", "-p"]:
            self.assertTrue(agentes._ARG_SEGURO_CMD.match(bueno), bueno)
        for malo in HOSTILES[:5]:
            self.assertFalse(agentes._ARG_SEGURO_CMD.match(malo), malo)

    @unittest.skipUnless(os.name == "nt", "cmd.exe solo existe en Windows")
    def test_un_script_cmd_con_texto_libre_se_rechaza(self):
        with tempfile.TemporaryDirectory() as tmp:
            bat = Path(tmp) / "agente.bat"
            bat.write_text("@echo %*\n", encoding="ascii")
            for prompt in HOSTILES:
                with self.subTest(prompt=prompt):
                    with self.assertRaises(agentes.LanzamientoRechazado):
                        agentes._launch([str(bat), "-p", prompt])
            r = agentes._launch([str(bat), "--version"])       # lo estatico si pasa
            self.assertIn("--version", r.stdout)

    def test_un_shim_de_npm_se_lanza_con_node_sin_cmd(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node no esta instalado")
        with tempfile.TemporaryDirectory() as tmp:
            raiz = Path(tmp)
            script = raiz / "node_modules" / "pkg" / "cli.js"
            script.parent.mkdir(parents=True)
            script.write_text("process.stdout.write(process.argv[2])", encoding="utf-8")
            shim = raiz / "pkg.cmd"
            shim.write_text('@ECHO off\r\n"%_prog%"  "%dp0%\\node_modules\\pkg\\cli.js" %*\r\n',
                            encoding="ascii")
            directo = agentes._script_de_shim(shim)
            self.assertEqual(Path(directo[1]), script)
            if os.name == "nt":
                r = agentes._launch([str(shim), HOSTILES[0]])
                self.assertEqual(r.stdout, HOSTILES[0])

    def test_un_rechazo_de_lanzamiento_es_un_resultado_no_una_excepcion(self):
        ad = agentes.RecipeAdapter("x", {"bins": ["x"], "write": ["{prompt}"]})
        with patch.object(agentes, "_launch",
                          side_effect=agentes.LanzamientoRechazado("inseguro")):
            res = ad.run("hola", allow_write=True)
        self.assertEqual(res["estado"], "rechazado")


if __name__ == "__main__":
    unittest.main()
