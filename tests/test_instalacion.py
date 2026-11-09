#!/usr/bin/env python3
"""Pruebas del instalador, el verificador (status) y el desinstalador.

Todo corre contra carpetas temporales: ninguna prueba toca la instalacion real
ni lanza procesos externos (el disparador se simula).
"""

import datetime as dt
import io
import importlib.util
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent

_spec = importlib.util.spec_from_file_location("soou", ROOT / "soou.py")
soou = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(soou)

N = chr(10)


class QuitarSeccionTomlTests(unittest.TestCase):
    MEDIO = ("[modelo]" + N + "x = 1" + N + N
             + "[mcp_servers.soou]" + N + "command = py" + N + N
             + "[otro]" + N + "y = 2" + N)

    def test_saca_la_seccion_del_medio(self):
        r = soou._quitar_seccion_toml(self.MEDIO, "mcp_servers.soou")
        self.assertNotIn("mcp_servers.soou", r)
        self.assertNotIn("command = py", r)

    def test_conserva_las_secciones_vecinas(self):
        r = soou._quitar_seccion_toml(self.MEDIO, "mcp_servers.soou")
        self.assertIn("[modelo]", r)
        self.assertIn("y = 2", r)

    def test_saca_la_seccion_final(self):
        texto = "[modelo]" + N + "x = 1" + N + N + "[mcp_servers.soou]" + N + "c = py" + N
        r = soou._quitar_seccion_toml(texto, "mcp_servers.soou")
        self.assertNotIn("soou", r)
        self.assertIn("[modelo]", r)

    def test_si_era_la_unica_el_archivo_queda_vacio(self):
        texto = "[mcp_servers.soou]" + N + "c = py" + N
        self.assertEqual(soou._quitar_seccion_toml(texto, "mcp_servers.soou"), "")

    def test_si_no_esta_no_cambia_nada(self):
        texto = "[modelo]" + N + "x = 1" + N
        self.assertIn("x = 1", soou._quitar_seccion_toml(texto, "mcp_servers.soou"))


class PurgarHooksTests(unittest.TestCase):
    def _hooks(self):
        propio = chr(34) + "py" + chr(34) + " " + chr(34) + "/x/soou.py" + chr(34)
        return {
            "SessionStart": [{"hooks": [{"type": "command", "command": propio + " context"}]}],
            "Stop": [
                {"hooks": [{"type": "command", "command": propio + " reindex"}]},
                {"hooks": [{"type": "command", "command": "prettier --write ."}]},
            ],
        }

    def test_quita_solo_las_entradas_de_soou(self):
        hooks = self._hooks()
        self.assertEqual(soou._purgar_hooks_soou(hooks), 2)
        self.assertNotIn("soou.py", json.dumps(hooks))

    def test_respeta_los_hooks_ajenos(self):
        hooks = self._hooks()
        soou._purgar_hooks_soou(hooks)
        self.assertIn("prettier", json.dumps(hooks))

    def test_el_evento_que_queda_vacio_desaparece(self):
        hooks = self._hooks()
        soou._purgar_hooks_soou(hooks)
        self.assertNotIn("SessionStart", hooks)


class RaizDeEscaneoTests(unittest.TestCase):
    def test_rechaza_la_carpeta_personal(self):
        self.assertIsNotNone(soou._motivo_para_no_escanear(Path.home().resolve(), False))

    def test_con_force_la_permite(self):
        self.assertIsNone(soou._motivo_para_no_escanear(Path.home().resolve(), True))

    def test_rechaza_la_raiz_del_disco(self):
        raiz = Path(Path.home().resolve().anchor)
        self.assertIsNotNone(soou._motivo_para_no_escanear(raiz, False))

    def test_una_carpeta_de_proyectos_es_valida(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(soou._motivo_para_no_escanear(Path(tmp).resolve(), False))


class ChequeoDeInstalacionTests(unittest.TestCase):
    """Home falso y disparador simulado: nada sale de la carpeta temporal."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        (self.home / ".claude").mkdir()
        self.muerto = str(self.home / "python-inexistente.exe")
        self.guion = str(ROOT / "soou.py")

    def _estado(self):
        with patch.object(Path, "home", return_value=self.home), \
             patch.object(soou.instalacion, "_tarea_sync_instalada", return_value=None):
            return {n: (ok, detalle) for n, ok, detalle, _ in soou._chequear_instalacion()}

    def _escribir_hooks(self, interprete, guion):
        comilla = chr(34)
        comando = (comilla + interprete + comilla + " "
                   + comilla + guion + comilla + " context")
        (self.home / ".claude" / "settings.json").write_text(
            json.dumps({"hooks": {"SessionStart": [
                {"hooks": [{"type": "command", "command": comando}]}]}}),
            encoding="utf-8")

    def test_sin_nada_instalado_reporta_faltantes(self):
        estado = self._estado()
        self.assertFalse(estado["sync automatico"][0])
        self.assertFalse(estado["hooks de Claude"][0])
        self.assertFalse(estado["servidor MCP"][0])

    def test_detecta_un_interprete_muerto_en_los_hooks(self):
        self._escribir_hooks(self.muerto, self.guion)
        ok, detalle = self._estado()["hooks de Claude"]
        self.assertFalse(ok)
        self.assertIn("interprete", detalle)

    def test_detecta_que_apunta_a_otra_copia_del_repo(self):
        self._escribir_hooks(sys.executable, "C:/otra/copia/soou.py")
        ok, detalle = self._estado()["hooks de Claude"]
        self.assertFalse(ok)
        self.assertIn("otra copia", detalle)

    def test_unos_hooks_sanos_pasan(self):
        self._escribir_hooks(sys.executable, self.guion)
        self.assertTrue(self._estado()["hooks de Claude"][0])

    def test_detecta_un_mcp_con_interprete_muerto(self):
        (self.home / ".claude.json").write_text(
            json.dumps({"mcpServers": {"soou": {
                "command": self.muerto, "args": [str(ROOT / "soou_mcp.py")]}}}),
            encoding="utf-8")
        ok, detalle = self._estado()["servidor MCP"]
        self.assertFalse(ok)
        self.assertIn("interprete", detalle)


class LatidoTests(unittest.TestCase):
    def setUp(self):
        self.previo = soou.rutas.HEARTBEAT_PATH
        self.tmp = Path(tempfile.mkdtemp())
        soou.rutas.HEARTBEAT_PATH = self.tmp / ".soou-heartbeat.json"

    def tearDown(self):
        soou.rutas.HEARTBEAT_PATH = self.previo

    def _estado_latido(self):
        with patch.object(Path, "home", return_value=self.tmp), \
             patch.object(soou.instalacion, "_tarea_sync_instalada", return_value=None):
            return next((ok, d) for n, ok, d, _ in soou._chequear_instalacion()
                        if n == "ultimo sync")

    def test_sin_latido_reporta_que_nunca_corrio(self):
        self.assertIn("nunca", self._estado_latido()[1])

    def test_un_latido_reciente_esta_vivo(self):
        soou._escribir_heartbeat()
        self.assertTrue(self._estado_latido()[0])

    def test_un_latido_viejo_se_reporta_detenido(self):
        soou.rutas.HEARTBEAT_PATH.write_text(json.dumps({
            "ts": (dt.datetime.now() - dt.timedelta(hours=3)).isoformat(timespec="seconds"),
            "interprete": "py", "notas_creadas": 0}), encoding="utf-8")
        ok, detalle = self._estado_latido()
        self.assertFalse(ok)
        self.assertIn("no esta corriendo", detalle)


class BorrarDerivadosTests(unittest.TestCase):
    def setUp(self):
        self.previo = soou.rutas.ROOT
        self.tmp = Path(tempfile.mkdtemp())
        soou.rutas.ROOT = self.tmp

    def tearDown(self):
        soou.rutas.ROOT = self.previo

    def test_borra_los_derivados_pero_no_las_notas(self):
        for nombre in ("soou.db", ".soou-heartbeat.json", "router-log.jsonl"):
            (self.tmp / nombre).write_text("x", encoding="utf-8")
        (self.tmp / "wiki").mkdir()
        nota = self.tmp / "wiki" / "nota.md"
        nota.write_text("conocimiento", encoding="utf-8")

        soou._borrar_derivados(False)

        self.assertFalse((self.tmp / "soou.db").exists())
        self.assertTrue(nota.exists())

    def test_la_simulacion_no_borra(self):
        (self.tmp / "soou.db").write_text("x", encoding="utf-8")
        soou._borrar_derivados(True)
        self.assertTrue((self.tmp / "soou.db").exists())


class FalsoTerminal:
    """stderr que se hace pasar por terminal y recuerda lo que le escribieron."""

    def __init__(self):
        self.escrito = []

    def isatty(self):
        return True

    def write(self, texto):
        self.escrito.append(texto)

    def flush(self):
        pass


class GiradorTests(unittest.TestCase):
    """El indicador tiene que desaparecer solo cuando no hay terminal: la tarea
    programada corre sin consola y CI captura la salida."""

    def test_sin_terminal_no_escribe_nada(self):
        mudo = FalsoTerminal()
        mudo.isatty = lambda: False
        with patch.object(sys, "stderr", mudo):
            with soou.Girador("probando"):
                time.sleep(0.05)
        self.assertEqual(mudo.escrito, [])

    def test_con_terminal_dibuja_cuadros(self):
        falso = FalsoTerminal()
        with patch.object(sys, "stderr", falso):
            with soou.Girador("probando"):
                time.sleep(0.4)
        cuadros = [x for x in falso.escrito if "probando" in x]
        self.assertGreaterEqual(len(cuadros), 2, "deberia haber animado")

    def test_al_salir_borra_lo_que_dibujo(self):
        falso = FalsoTerminal()
        with patch.object(sys, "stderr", falso):
            with soou.Girador("probando"):
                time.sleep(0.2)
        self.assertNotIn("probando", falso.escrito[-1])
        self.assertTrue(falso.escrito[-1].endswith(chr(13)))

    def test_una_excepcion_no_lo_deja_girando(self):
        falso = FalsoTerminal()
        girador = soou.Girador("probando")
        with patch.object(sys, "stderr", falso):
            try:
                with girador:
                    raise RuntimeError("falla dentro del bloque")
            except RuntimeError:
                pass
        self.assertFalse(girador.activo)

    def test_no_usa_stdout(self):
        """stdout queda libre para canalizar la salida del comando a otro."""
        falso = FalsoTerminal()
        salida = io.StringIO()
        with patch.object(sys, "stderr", falso), patch.object(sys, "stdout", salida):
            with soou.Girador("probando"):
                time.sleep(0.2)
        self.assertEqual(salida.getvalue(), "")


class ConsolaFalsa(io.StringIO):
    """stdout que se hace pasar por terminal con un encoding dado."""

    def __init__(self, encoding="utf-8"):
        super().__init__()
        self._enc = encoding

    @property
    def encoding(self):
        return self._enc

    def isatty(self):
        return True


class MarcaTests(unittest.TestCase):
    """La marca es decorativa: jamas debe romper un comando ni ensuciar un pipe."""

    BLOQUE = chr(0x2588)

    def _dibujar(self, consola):
        with patch.object(sys, "stdout", consola):
            soou.mostrar_marca("subtitulo", animar=False)
        return consola.getvalue()

    def test_en_utf8_usa_los_bloques(self):
        salida = self._dibujar(ConsolaFalsa("utf-8"))
        self.assertIn(self.BLOQUE, salida)
        self.assertIn("subtitulo", salida)

    def test_en_cp1252_degrada_a_ascii_sin_romper(self):
        salida = self._dibujar(ConsolaFalsa("cp1252"))
        self.assertNotIn(self.BLOQUE, salida)
        self.assertIn("___", salida, "deberia caer al respaldo ASCII")
        self.assertIn("subtitulo", salida)

    def test_sin_terminal_no_dibuja(self):
        salida = io.StringIO()
        with patch.object(sys, "stdout", salida):
            soou.mostrar_marca("subtitulo", animar=False)
        self.assertEqual(salida.getvalue(), "")

    def test_si_la_consola_falla_no_propaga(self):
        class ConsolaRota(ConsolaFalsa):
            def write(self, _):
                raise OSError("consola cerrada")

        with patch.object(sys, "stdout", ConsolaRota("utf-8")):
            soou.mostrar_marca("subtitulo", animar=False)   # no debe lanzar

    def test_sin_encoding_declarado_no_revienta(self):
        class SinEncoding(ConsolaFalsa):
            @property
            def encoding(self):
                return None

        salida = self._dibujar(SinEncoding())
        self.assertIn("___", salida)


if __name__ == "__main__":
    unittest.main()
