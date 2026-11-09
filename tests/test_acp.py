"""Cliente ACP contra un agente ACP falso: handshake, progreso, permisos segun
el modo, cancelacion y errores de protocolo. Nunca lanza un agente real."""

import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
from soou_core import acp, agentes, config, contrato, ejecutor, registro, sesiones  # noqa: E402

FALSO = str(RAIZ / "tests" / "fixtures" / "agente_acp_falso.py")


class AcpTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.ws = self.base / "ws"
        self.ws.mkdir()
        (self.ws / "README.md").write_text("hola readme", encoding="utf-8")
        self.ad = acp.AcpAdapter("falso", [sys.executable, FALSO])

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def _run(self, prompt, escritura=False, timeout=30, ad=None):
        return (ad or self.ad).run(prompt, allow_write=escritura, timeout=timeout, cwd=str(self.ws))

    # ------------------------------------------------------------ turno normal
    def test_lectura_completa_con_progreso(self):
        r = self._run("resume el repo")
        self.assertTrue(r["ok"], r["output"])
        self.assertEqual(r["estado"], "completado")
        self.assertIn("permiso lectura: concedido opcion=si", r["output"],
                      "en lectura se concede una vez, nunca 'siempre'")
        self.assertIn("contenido: hola readme", r["output"])
        tipos = [e["tipo"] for e in r["eventos"]]
        for t in ("plan", "tool_call", "permiso", "tool_call_update"):
            self.assertIn(t, tipos)
        self.assertEqual(r["uso"]["used"], 42)

    def test_lectura_niega_escribir(self):
        r = self._run("ESCRIBE x.txt")
        self.assertIn("permiso escritura: denegado", r["output"])
        self.assertIn("fs escritura: error -32000", r["output"])
        self.assertFalse((self.ws / "x.txt").exists())

    def test_lectura_niega_ejecutar(self):
        self.assertIn("permiso ejecutar: denegado", self._run("EJECUTA")["output"])

    def test_escritura_dentro_del_workspace(self):
        r = self._run("ESCRIBE sub/x.txt", escritura=True)
        self.assertIn("permiso escritura: concedido", r["output"])
        self.assertIn("fs escritura: ok", r["output"])
        self.assertEqual((self.ws / "sub" / "x.txt").read_text(encoding="utf-8"), "hola")

    def test_escritura_fuera_del_workspace_se_niega(self):
        r = self._run("FUERA", escritura=True)
        self.assertIn("permiso fuera: denegado", r["output"])
        self.assertIn("fs fuera: error", r["output"])
        self.assertFalse((self.base / "fuera-del-workspace.txt").exists())

    def test_escritura_permite_ejecutar_en_el_workspace(self):
        self.assertIn("permiso ejecutar: concedido", self._run("EJECUTA", escritura=True)["output"])

    # ------------------------------------------------------------ fin del turno
    def test_timeout_cancela_el_turno(self):
        t0 = time.time()
        r = self._run("LENTO", timeout=2)
        self.assertEqual((r["estado"], r["codigo_error"]), ("timeout", "E_TIMEOUT"))
        self.assertLess(time.time() - t0, 10)

    def test_agente_que_ignora_cancel_se_mata(self):
        with patch.object(acp, "GRACIA_CANCELAR_S", 1):
            t0 = time.time()
            r = self._run("SORDO", timeout=1)
        self.assertEqual(r["estado"], "timeout")
        self.assertLess(time.time() - t0, 10, "no espera al agente sordo indefinidamente")

    def test_refusal_es_no_pudo(self):
        r = self._run("RECHAZA")
        self.assertEqual((r["ok"], r["codigo_error"]), (False, "E_NO_PUDO"))

    def test_salida_no_acp(self):
        r = self._run("BASURA", timeout=10)
        self.assertEqual(r["codigo_error"], "E_SALIDA_INVALIDA")

    def test_version_no_soportada(self):
        ad = acp.AcpAdapter("v2", [sys.executable, FALSO, "--version", "2"])
        r = self._run("hola", ad=ad)
        self.assertEqual(r["codigo_error"], "E_SALIDA_INVALIDA")
        self.assertIn("version", r["output"])

    def test_agente_inexistente(self):
        ad = acp.AcpAdapter("nada", [str(self.base / "no-existe.exe")])
        self.assertFalse(ad.available())
        self.assertEqual(self._run("hola", ad=ad)["codigo_error"], "E_LANZAMIENTO")

    # ------------------------------------------------------------ modos de sesion
    def test_en_lectura_pone_la_sesion_en_solo_lectura(self):
        # como codex-acp: arranca en un modo que edita sin preguntar
        ad = acp.AcpAdapter("modos", [sys.executable, FALSO, "--modos"])
        r = self._run("ESCRIBE x.txt", ad=ad)
        self.assertFalse((self.ws / "x.txt").exists(), r["output"])
        self.assertIn("permiso escritura: denegado", r["output"])
        modo = next(e for e in r["eventos"] if e["tipo"] == "modo")
        self.assertEqual((modo["modo"], modo["previo"]), ("read-only", "auto"))

    def test_en_escritura_elige_el_workspace_y_nunca_acceso_total(self):
        ad = acp.AcpAdapter("modos", [sys.executable, FALSO, "--modos"])
        r = self._run("ESCRIBE x.txt", escritura=True, ad=ad)
        self.assertIn("fs escritura: ok", r["output"])
        self.assertEqual(next(e for e in r["eventos"] if e["tipo"] == "modo")["modo"],
                         "workspace-write")

    def test_sin_modo_de_lectura_queda_constancia(self):
        ad = acp.AcpAdapter("sin", [sys.executable, FALSO, "--modos-sin-lectura"])
        r = self._run("hola", ad=ad)
        aviso = next(e for e in r["eventos"] if e["tipo"] == "modo")
        self.assertIn("solo rigen los permisos", aviso["aviso"])

    def test_el_ejecutor_detecta_la_escritura_no_pedida(self):
        # sin modo de lectura el agente escribe igual: la verificacion posterior
        # lo marca como fallo de permisos (estado coherente, no "completado")
        import subprocess
        subprocess.run(["git", "init", "-q", str(self.ws)], check=True)
        subprocess.run(["git", "-C", str(self.ws), "-c", "user.name=t", "-c", "user.email=t@t",
                        "commit", "-q", "--allow-empty", "-m", "b"], check=True)
        ad = acp.AcpAdapter("sin", [sys.executable, FALSO, "--modos-sin-lectura"])
        req = contrato.TaskRequest(tarea="ESCRIBE x.txt", workspace=str(self.ws), modo="leer")
        with patch.object(config, "_budget_reserve", return_value=True),                 patch.object(config, "_model_for", return_value=None),                 patch.object(config, "_reasoning_for", return_value=None),                 patch.object(sesiones, "_runs_dir", return_value=self.base / "runs"):
            res = ejecutor.intentar(ad, req)
        self.assertFalse(res.cumple("minimo"))
        self.assertEqual((res.estado, res.codigo_error), ("fallo", "E_PERMISO"))

    def test_probe_hace_el_handshake(self):
        ok, detalle = self.ad.probe()
        self.assertTrue(ok, detalle)
        self.assertIn("ACP v1 falso", detalle)

    # ------------------------------------------------------------ integracion
    def test_lectura_acp_es_segura_y_pasa_por_el_ejecutor(self):
        self.assertTrue(agentes.lectura_segura(self.ad))
        req = contrato.TaskRequest(tarea="resume", workspace=str(self.ws), modo="leer")
        with patch.object(config, "_budget_reserve", return_value=True), \
                patch.object(config, "_model_for", return_value=None), \
                patch.object(config, "_reasoning_for", return_value=None), \
                patch.object(sesiones, "_runs_dir", return_value=self.base / "runs"):
            res = ejecutor.intentar(self.ad, req)
        self.assertTrue(res.cumple("minimo"), res.motivo)
        self.assertTrue(any(e["tipo"] == "permiso" for e in res.eventos))

    def test_config_construye_el_adaptador(self):
        cfg = {"agents": [{"name": "mi-acp", "type": "acp", "command": [sys.executable, FALSO]}]}
        with patch.object(config, "load_config", return_value=cfg):
            ads = registro.build_adapters()
        self.assertIsInstance(ads[0], acp.AcpAdapter)
        self.assertEqual((ads[0].name, ads[0].restriccion_lectura), ("mi-acp", "cliente_acp"))


class DecidirPermisoTests(unittest.TestCase):
    OPS = [{"optionId": "a", "kind": "allow_once"}, {"optionId": "s", "kind": "allow_always"},
           {"optionId": "r", "kind": "reject_once"}]

    def test_lectura_niega_kind_desconocido_o_ausente(self):
        for kind in (None, "other", "edit", "switch_mode"):
            out, concedido, _ = acp.decidir_permiso({"kind": kind}, self.OPS, False, ".")
            self.assertEqual((out, concedido), ({"outcome": "selected", "optionId": "r"}, False), kind)

    def test_sin_opcion_de_rechazo_se_cancela(self):
        out, concedido, _ = acp.decidir_permiso({"kind": "edit"}, [self.OPS[0]], False, ".")
        self.assertEqual((out, concedido), ({"outcome": "cancelled"}, False))

    def test_lectura_nunca_elige_siempre(self):
        out, _, _ = acp.decidir_permiso({"kind": "read"}, [self.OPS[1]], False, ".")
        self.assertEqual(out, {"outcome": "cancelled"})

    def test_ruta_relativa_cuenta_como_fuera(self):
        out, concedido, _ = acp.decidir_permiso(
            {"kind": "edit", "locations": [{"path": "../x"}]}, self.OPS, True, ".")
        self.assertFalse(concedido)


if __name__ == "__main__":
    unittest.main()
