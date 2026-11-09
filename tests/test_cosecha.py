#!/usr/bin/env python3
"""Pruebas de los cosechadores de sesion y de la politica de captura.

Los cosechadores leen carpetas globales de la maquina, asi que lo que se prueba
sobre todo es QUE entra al cerebro y que no se duplique. Todo contra carpetas
temporales: nunca se lee la instalacion real.
"""

import importlib.util
import json
import os
import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent

_spec = importlib.util.spec_from_file_location("soou", ROOT / "soou.py")
soou = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(soou)


def _config(capture_roots=None, exclude_projects=None, capture_unknown_cwd=False):
    return {"capture_roots": capture_roots or [],
            "exclude_projects": exclude_projects or [],
            "capture_unknown_cwd": capture_unknown_cwd}


# ruta de sistema ABSOLUTA en el SO donde corre el test (Windows o Unix)
_RUTA_SISTEMA = "C:/Windows/system32" if os.name == "nt" else "/usr/lib"


class PoliticaDeCapturaTests(unittest.TestCase):
    def test_una_ruta_de_tu_carpeta_personal_entra(self):
        with patch.object(soou.config, "load_config", return_value=_config()):
            ok, _ = soou._captura_permitida(str(Path.home() / "proyectos" / "app"))
        self.assertTrue(ok)

    def test_una_ruta_de_sistema_queda_fuera(self):
        with patch.object(soou.config, "load_config", return_value=_config()):
            ok, motivo = soou._captura_permitida(_RUTA_SISTEMA)
        self.assertFalse(ok)
        self.assertIn("carpeta personal", motivo)

    def test_sin_ruta_conocida_se_descarta(self):
        with patch.object(soou.config, "load_config", return_value=_config()):
            ok, motivo = soou._captura_permitida(None)
        self.assertFalse(ok, "sin ruta no se puede comprobar capture_roots")
        self.assertIn("sin ruta", motivo)

    def test_sin_ruta_se_admite_solo_con_opcion_explicita(self):
        cfg = _config(capture_unknown_cwd=True)
        with patch.object(soou.config, "load_config", return_value=cfg):
            self.assertTrue(soou._captura_permitida(None)[0])
        cfg = _config(capture_roots=["/permitida"], capture_unknown_cwd=False)
        with patch.object(soou.config, "load_config", return_value=cfg):
            self.assertFalse(soou._captura_permitida("")[0])

    def test_la_lista_blanca_acota(self):
        with tempfile.TemporaryDirectory() as permitida, \
             tempfile.TemporaryDirectory() as ajena:
            cfg = _config(capture_roots=[permitida])
            with patch.object(soou.config, "load_config", return_value=cfg):
                self.assertTrue(soou._captura_permitida(str(Path(permitida) / "app"))[0])
                ok, motivo = soou._captura_permitida(str(Path(ajena) / "app"))
        self.assertFalse(ok)
        self.assertIn("capture_roots", motivo)

    def test_exclude_projects_descarta_por_nombre(self):
        ruta = Path.home() / "cliente-secreto"
        cfg = _config(exclude_projects=[soou.slugify("cliente-secreto")])
        with patch.object(soou.config, "load_config", return_value=cfg):
            ok, motivo = soou._captura_permitida(str(ruta))
        self.assertFalse(ok)
        self.assertIn("exclude_projects", motivo)


class TemporalTests(unittest.TestCase):
    """eval --tareas y la conformidad corren agentes en el temporal (en Windows,
    dentro de tu carpeta): sus sesiones, con contenido inventado, no entran."""

    def test_el_temporal_queda_fuera_por_defecto(self):
        ruta = str(Path(tempfile.gettempdir()) / "soou-eval-x" / "faro-umbral-codex-con" / "faro")
        with patch.object(soou.config, "load_config", return_value=_config()):
            ok, motivo = soou._captura_permitida(ruta)
        self.assertFalse(ok)
        if soou.sesiones._bajo(Path(tempfile.gettempdir()), Path.home()):   # Windows
            self.assertIn("temporal", motivo)                                # (en Linux: /tmp)

    def test_una_raiz_amplia_no_lo_reabre(self):
        ruta = str(Path(tempfile.gettempdir()) / "soou-eval-x" / "telar")
        cfg = _config(capture_roots=[str(Path.home()), str(Path(tempfile.gettempdir()).parent)])
        with patch.object(soou.config, "load_config", return_value=cfg):
            self.assertFalse(soou._captura_permitida(ruta)[0])


class TranscripcionDeClaudeTests(unittest.TestCase):
    def test_extrae_id_ruta_y_mensajes(self):
        registros = [
            {"type": "user", "sessionId": "abc-123", "cwd": "C:/proy",
             "message": {"role": "user", "content": "hola"}},
            {"type": "assistant", "sessionId": "abc-123",
             "message": {"role": "assistant",
                         "content": [{"type": "text", "text": "respuesta"}]}},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            ruta = Path(tmp) / "t.jsonl"
            ruta.write_text("\n".join(json.dumps(r) for r in registros), encoding="utf-8")
            sid, cwd, body = soou.extract_claude_transcript(ruta)
        self.assertEqual(sid, "abc-123")
        self.assertEqual(cwd, "C:/proy")
        self.assertEqual(len(body), 2)


class CosechadorDeClaudeTests(unittest.TestCase):
    def setUp(self):
        self.previos = (soou.rutas.CLAUDE_SESSIONS, soou.rutas.SESSIONS)
        self.tmp = Path(tempfile.mkdtemp())
        soou.rutas.CLAUDE_SESSIONS = self.tmp / "projects"
        soou.rutas.SESSIONS = self.tmp / "raw"
        soou.rutas.SESSIONS.mkdir(parents=True)
        self.parche = patch.object(soou.config, "load_config", return_value=_config())
        self.parche.start()

    def tearDown(self):
        self.parche.stop()
        soou.rutas.CLAUDE_SESSIONS, soou.rutas.SESSIONS = self.previos

    def _transcripcion(self, sid, cwd, mensajes=40, vieja=True):
        carpeta = soou.rutas.CLAUDE_SESSIONS / ("proj-" + sid)
        carpeta.mkdir(parents=True, exist_ok=True)
        ruta = carpeta / (sid + ".jsonl")
        registros = []
        for i in range(mensajes):
            registros.append({"type": "assistant", "sessionId": sid, "cwd": cwd,
                              "message": {"role": "assistant",
                                          "content": [{"type": "text",
                                                       "text": "contenido " + str(i) + " " * 120}]}})
        ruta.write_text("\n".join(json.dumps(r) for r in registros), encoding="utf-8")
        if vieja:
            viejo = time.time() - 3600
            os.utime(ruta, (viejo, viejo))
        return ruta

    def _importadas(self):
        return sorted(soou.rutas.SESSIONS.rglob("session-*.md"))

    def test_una_sesion_de_carpeta_de_sistema_se_importa_solo_explicita(self):
        self._transcripcion("sis", _RUTA_SISTEMA)
        self.assertEqual(soou._pull_claude_sessions(), 0, "la automatica la descarta")
        proyecto = str(Path.home() / "proy")
        ok, msg = soou.sesiones.importar_sesion("sis", proyecto)
        self.assertTrue(ok, msg)
        txt = self._importadas()[0].read_text(encoding="utf-8")
        self.assertIn("Proyecto: " + str(Path(proyecto).resolve()), txt)
        self.assertIn("Importada: explicita (directorio original: " + _RUTA_SISTEMA, txt)
        ok, msg = soou.sesiones.importar_sesion("sis", proyecto)
        self.assertFalse(ok)
        self.assertIn("ya estaba capturada", msg)

    def test_importar_no_amplia_la_politica(self):
        self._transcripcion("sis2", _RUTA_SISTEMA)
        ok, msg = soou.sesiones.importar_sesion("sis2", _RUTA_SISTEMA)
        self.assertFalse(ok, "atribuirla a la carpeta de sistema no la vuelve capturable")
        ok, msg = soou.sesiones.importar_sesion("sis2", "")
        self.assertFalse(ok)
        self.assertIn("--proyecto", msg)
        self.assertEqual(self._importadas(), [])

    def test_importa_una_sesion_sustancial(self):
        self._transcripcion("aaa", str(Path.home() / "proy"))
        self.assertEqual(soou._pull_claude_sessions(), 1)
        self.assertEqual(len(self._importadas()), 1)

    def test_descarta_una_ruta_de_sistema(self):
        self._transcripcion("bbb", _RUTA_SISTEMA)
        self.assertEqual(soou._pull_claude_sessions(), 0)

    def test_descarta_una_sesion_trivial(self):
        self._transcripcion("ccc", str(Path.home() / "proy"), mensajes=1)
        self.assertEqual(soou._pull_claude_sessions(), 0)

    def test_salta_una_sesion_todavia_abierta(self):
        self._transcripcion("ddd", str(Path.home() / "proy"), vieja=False)
        self.assertEqual(soou._pull_claude_sessions(), 0)

    def test_es_idempotente(self):
        self._transcripcion("eee", str(Path.home() / "proy"))
        soou._pull_claude_sessions()
        self.assertEqual(soou._pull_claude_sessions(), 0)
        self.assertEqual(len(self._importadas()), 1)

    def test_no_duplica_lo_que_ya_capturo_el_hook(self):
        sid = "fff"
        self._transcripcion(sid, str(Path.home() / "proy"))
        previa = soou.rutas.SESSIONS / "2026" / "01" / "01" / ("session-" + sid + ".md")
        previa.parent.mkdir(parents=True, exist_ok=True)
        previa.write_text("<!-- distilled: no -->\n# ya capturada por el hook\n",
                          encoding="utf-8")

        soou._pull_claude_sessions()

        self.assertEqual(len(self._importadas()), 1)

    def test_no_pisa_una_sesion_ya_sintetizada_por_ia(self):
        sid = "ggg"
        self._transcripcion(sid, str(Path.home() / "proy"))
        previa = soou.rutas.SESSIONS / "2026" / "01" / "01" / ("session-" + sid + ".md")
        previa.parent.mkdir(parents=True, exist_ok=True)
        previa.write_text("<!-- distilled: yes -->\n# sintetizada\n", encoding="utf-8")

        soou._pull_claude_sessions(refresh=True)

        self.assertIn("distilled: yes", previa.read_text(encoding="utf-8"))

    def test_lo_importado_lo_entiende_el_destilador(self):
        self._transcripcion("hhh", str(Path.home() / "proy"))
        soou._pull_claude_sessions()
        datos = soou._parse_session_raw(self._importadas()[0])
        self.assertEqual(datos["source"], "claude-code")
        self.assertGreater(datos["n_msgs"], 0)


class FuentesDeSesionTests(unittest.TestCase):
    def test_el_sync_cosecha_las_dos_fuentes(self):
        nombres = [n for n, _ in soou._session_sources()]
        self.assertIn("codex", nombres)
        self.assertIn("claude-code", nombres)

    def test_el_extractor_de_codex_reporta_la_ruta(self):
        registros = [
            {"type": "session_meta",
             "payload": {"session_id": "s1", "cwd": "C:/proy"}},
            {"type": "event_msg",
             "payload": {"type": "item_completed",
                         "item": {"type": "AgentMessage",
                                  "content": [{"text": "respuesta"}]}}},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            ruta = Path(tmp) / "rollout-x.jsonl"
            ruta.write_text("\n".join(json.dumps(r) for r in registros), encoding="utf-8")
            sid, cwd, body = soou.extract_codex_rollout(ruta)
        self.assertEqual(sid, "s1")
        self.assertEqual(cwd, "C:/proy")
        self.assertEqual(len(body), 1)


if __name__ == "__main__":
    unittest.main()
