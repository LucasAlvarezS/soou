#!/usr/bin/env python3
"""Pruebas de la memoria acotada y del harness (2026-11).

Sesiones: el L0 se guarda saneado (sin bloques que inyecta el cliente ni
secretos), deduplicado y con tope de tamaño; lo archivado va comprimido y los
logs rotan. Harness: los runs viven en UNA carpeta, cada paso deja traza y
checkpoint, y el router aprende a evitar al agente que falla.
"""

import argparse
import contextlib
import gzip
import io
import importlib.util
import json
import os
import shutil
import subprocess
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


class Entorno(unittest.TestCase):
    def setUp(self):
        self.previos = (soou.rutas.ROOT, soou.rutas.WIKI, soou.rutas.SESSIONS, soou.rutas.DB_PATH, soou.rutas.ARCHIVO)
        self.tmp = Path(tempfile.mkdtemp())
        soou.rutas.ROOT = self.tmp
        soou.rutas.WIKI = self.tmp / "wiki"
        soou.rutas.SESSIONS = self.tmp / "sessions" / "raw"
        soou.rutas.DB_PATH = self.tmp / "soou.db"
        soou.rutas.ARCHIVO = self.tmp / "archivo"
        (soou.rutas.WIKI / "code").mkdir(parents=True)
        soou.rutas.SESSIONS.mkdir(parents=True)

    def tearDown(self):
        (soou.rutas.ROOT, soou.rutas.WIKI, soou.rutas.SESSIONS, soou.rutas.DB_PATH, soou.rutas.ARCHIVO) = self.previos
        shutil.rmtree(self.tmp, ignore_errors=True)


class SaneadoTests(unittest.TestCase):
    def test_quita_bloques_inyectados_cerrados_y_recortados(self):
        t = ("Hola <system-reminder>CLAUDE.md enorme</system-reminder> mundo"
             "<environment_context><cwd>x</cwd></environment_context>")
        self.assertEqual(soou._limpiar_texto(t), "Hola  mundo")
        recortado = "pedido real\n<system-reminder>contenido cortado a 2000 sin cier"
        self.assertEqual(soou._limpiar_texto(recortado), "pedido real")

    def test_redacta_secretos(self):
        # claves FALSAS armadas por partes: con el literal entero los escaneres de
        # secretos (p.ej. el de GitHub) marcan el repo aunque no sea una clave real.
        falsa_sk = "sk-" + "ant-" + "abcdefghijklmnopqrstu"
        falsa_gh = "gh" + "p_" + "abcdefghijklmnopqrstuvwxyz12"
        t = (f"key {falsa_sk} y {falsa_gh} "
             "password=superSecreta123 postgres://admin:clave123@db.host:5432/x")
        limpio = soou._limpiar_texto(t)
        for fuga in ("abcdefghijklmnopqrstu", "superSecreta123", "clave123"):
            self.assertNotIn(fuga, limpio)
        self.assertIn("postgres://admin:…@db.host", limpio)

    def test_no_toca_texto_normal(self):
        t = "Usamos 32 tokens: bien. El password debe rotarse cada mes."
        self.assertEqual(soou._limpiar_texto(t), t)

    def test_mensaje_que_era_solo_ruido_se_descarta(self):
        self.assertIsNone(soou._linea_sesion("user", "<system-reminder>x</system-reminder>"))
        self.assertIsNone(soou._linea_sesion("tool", "algo"))


class AcotadoTests(unittest.TestCase):
    def test_deduplica(self):
        body = ["**user:** a", "**assistant:** b", "**user:** a"]
        self.assertEqual(soou._acotar_cuerpo(body, 10_000), ["**user:** a", "**assistant:** b"])

    def test_tope_conserva_inicio_y_final(self):
        body = [f"**user:** mensaje {i} " + "x" * 80 for i in range(200)]
        out = soou._acotar_cuerpo(body, 3000)
        self.assertLessEqual(sum(len(l) for l in out), 3200)
        self.assertEqual(out[0], body[0])
        self.assertEqual(out[-1], body[-1])
        self.assertTrue(any("omitidos" in l for l in out))


class LimpiezaDeExistentesTests(Entorno):
    def crudo(self, nombre, cuerpo, dias=40):
        ruta = soou.rutas.SESSIONS / nombre
        ruta.write_text("<!-- distilled: yes -->\n# Sesion x\nFuente: x.jsonl\n\n" + cuerpo,
                        encoding="utf-8")
        viejo = time.time() - dias * 86400
        os.utime(ruta, (viejo, viejo))
        return ruta

    def test_simulacion_no_escribe(self):
        r = self.crudo("session-a.md", "**user:** hola <system-reminder>ruido</system-reminder>")
        antes = r.read_text(encoding="utf-8")
        res = soou._limpiar_sesiones(False)
        self.assertEqual(res["cambiados"], 1)
        self.assertEqual(r.read_text(encoding="utf-8"), antes)
        self.assertFalse(soou.rutas.ARCHIVO.exists())

    def test_aplicar_respalda_conserva_cabecera_marca_y_mtime(self):
        r = self.crudo("session-a.md",
                       "**user:** hola <system-reminder>ruido</system-reminder>\n\n"
                       "**assistant:** listo, token=abcdef123456")
        mtime = r.stat().st_mtime
        res = soou._limpiar_sesiones(True)
        txt = r.read_text(encoding="utf-8")
        self.assertTrue(txt.startswith("<!-- distilled: yes -->\n# Sesion x"))
        self.assertIn("**user:** hola", txt)
        self.assertNotIn("ruido", txt)
        self.assertNotIn("abcdef123456", txt)
        self.assertAlmostEqual(r.stat().st_mtime, mtime, delta=1)
        self.assertTrue(res["respaldo"].exists())


class CompactTests(Entorno):
    def test_nota_con_nombre_antiguo_no_es_huerfana(self):
        sid = "01a06da5-573c-7693-961d-b4770a376a9f"
        crudo = soou.rutas.SESSIONS / f"session-codex-{sid}.md"
        crudo.write_text("<!-- distilled: yes -->\n# x\n\n**user:** hola", encoding="utf-8")
        legacy = soou._nota_legacy_de_crudo(crudo)
        self.assertEqual(legacy, f"sesion-codex-{sid}")
        self.assertNotEqual(legacy, soou._nota_de_crudo(crudo))
        (soou.rutas.WIKI / "code" / f"{legacy}.md").write_text(
            f"---\nname: {legacy}\narea: code\ntype: session\nsources: []\n"
            "pinned: false\nupdated: 2026-09-04\n---\n\n" + "contenido " * 30,
            encoding="utf-8")
        self.assertEqual(soou.plan_de_compactacion()["huerfanos"], [])

    def test_misma_sesion_en_dos_fechas_no_corta_el_compact(self):
        viejo = time.time() - 60 * 86400
        crudos = []
        for fecha in ("2026/08/20", "2026/08/21"):
            d = soou.rutas.SESSIONS / fecha
            d.mkdir(parents=True)
            c = d / "session-e9bbab7a-4a7.md"
            c.write_text("<!-- distilled: yes -->\n# x\n\n**user:** hola", encoding="utf-8")
            os.utime(c, (viejo, viejo))
            crudos.append(c)
        nombre = soou._nota_de_crudo(crudos[0])
        (soou.rutas.WIKI / "code" / f"{nombre}.md").write_text(
            f"---\nname: {nombre}\narea: code\ntype: session\nsources: []\n"
            "pinned: false\nupdated: 2026-08-20\n---\n\n" + "contenido " * 30,
            encoding="utf-8")
        soou.cmd_compact(argparse.Namespace(apply=True, dias=30, recuperar=False))
        registros = (soou.rutas.ARCHIVO / "manifiesto.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(registros), 3, "1 nota + 2 crudos, cada uno una sola vez")
        self.assertFalse(any(c.exists() for c in crudos))

    def test_archivar_una_nota_no_deja_enlaces_rotos(self):
        crudo = soou.rutas.SESSIONS / "session-abc.md"
        crudo.write_text("<!-- distilled: yes -->\n# x\n\n**user:** hola", encoding="utf-8")
        nombre = soou._nota_de_crudo(crudo)
        fm = "---\nname: {n}\narea: code\ntype: {t}\nsources: []\npinned: false\nupdated: 2026-09-01\n---\n\n"
        (soou.rutas.WIKI / "code" / f"{nombre}.md").write_text(
            fm.format(n=nombre, t="session") + "contenido " * 30, encoding="utf-8")
        concepto = soou.rutas.WIKI / "code" / "concepto.md"
        concepto.write_text(fm.format(n="concepto", t="concept")
                            + f"Visto en [[{nombre}]] y en [[{nombre}|la charla]]. "
                            + "relleno " * 30, encoding="utf-8")
        soou.cmd_compact(argparse.Namespace(apply=True, dias=30, recuperar=False))
        txt = concepto.read_text(encoding="utf-8")
        self.assertNotIn("[[" + nombre, txt)
        self.assertIn(f"{nombre} (archivada)", txt)
        self.assertIn("la charla (archivada)", txt)

    def test_archivo_comprimido_y_recuperable(self):
        f = soou.rutas.SESSIONS / "session-x.md"
        f.write_text("contenido " * 500, encoding="utf-8")
        original = f.read_bytes()
        destino = soou._mover_al_archivo(f, True)
        self.assertTrue(destino.endswith(".md.gz"))
        self.assertFalse(f.exists())
        self.assertEqual(gzip.decompress((soou.rutas.ROOT / destino).read_bytes()), original)

    def test_rota_router_log(self):
        log = soou.rutas.ROOT / "router-log.jsonl"
        log.write_text("\n".join(json.dumps({"i": i}) for i in range(soou.LOG_MAX_LINEAS + 50))
                       + "\n", encoding="utf-8")
        hechos = soou._rotar_logs(True)
        self.assertTrue(hechos)
        self.assertEqual(len(log.read_text(encoding="utf-8").splitlines()), soou.LOG_MAX_LINEAS)
        viejos = list((soou.rutas.ARCHIVO / "logs").glob("router-log-*.jsonl.gz"))
        self.assertEqual(len(gzip.decompress(viejos[0].read_bytes()).decode().splitlines()), 50)


class CapturaTests(Entorno):
    def _transcript(self, mensajes):
        tr = self.tmp / "t.jsonl"
        relleno = [{"type": "user", "message": {"role": "user", "content": "x" * 50}}] * 0
        filas = [{"type": r, "sessionId": "abc-123", "message": {"role": r, "content": t}}
                 for r, t in mensajes] + relleno
        tr.write_text("\n".join(json.dumps(f) for f in filas) + "\n" + " " * 5000,
                      encoding="utf-8")
        return tr

    def _capturar(self, tr, cwd=None):
        cwd = cwd or str(Path.home() / "proyecto")
        hook = {"transcript_path": str(tr), "session_id": "abc-123", "cwd": cwd}
        with patch.object(soou.sesiones, "read_hook_stdin", return_value=hook):
            soou.cmd_capture(argparse.Namespace())

    def test_precompact_y_sessionend_dejan_un_solo_archivo_saneado(self):
        tr = self._transcript([("user", "hola <system-reminder>CLAUDE.md</system-reminder>"),
                               ("assistant", "respuesta")])
        self._capturar(tr)                      # PreCompact
        tr2 = self._transcript([("user", "hola"), ("assistant", "respuesta"),
                                ("user", "sigue"), ("assistant", "hecho")])
        self._capturar(tr2)                     # SessionEnd, otro dia incluso
        archivos = list(soou.rutas.SESSIONS.rglob("session-abc-123.md"))
        self.assertEqual(len(archivos), 1)
        txt = archivos[0].read_text(encoding="utf-8")
        self.assertNotIn("CLAUDE.md", txt)
        self.assertIn("**assistant:** hecho", txt)
        self.assertIn("Proyecto: " + str(Path.home() / "proyecto"), txt)


class RunsTests(Entorno):
    def test_runs_en_carpeta_unica_y_configurable(self):
        with patch.object(soou.config, "load_config", return_value={}):
            self.assertEqual(soou._runs_dir(), soou.rutas.ROOT / "runs")
        otra = self.tmp / "otra"
        with patch.object(soou.config, "load_config", return_value={"runs_dir": str(otra)}):
            self.assertEqual(soou._runs_dir(), otra)

    def _repo(self):
        repo = self.tmp / "Escritorio" / "MiRepo"
        repo.mkdir(parents=True)
        g = lambda *a: subprocess.run(["git", "-C", str(repo), *a], capture_output=True,
                                      check=True)
        g("init", "-q"); g("config", "user.email", "t@t"); g("config", "user.name", "t")
        (repo / "a.txt").write_text("a", encoding="utf-8")
        g("add", "-A"); g("commit", "-q", "-m", "base")
        return repo

    def test_worktree_no_se_crea_junto_al_repo(self):
        repo = self._repo()
        with patch.object(soou.config, "load_config", return_value={}):
            wt, branch, err = soou._make_worktree(str(repo))
        self.assertIsNone(err)
        self.assertEqual(Path(wt).parent, soou.rutas.ROOT / "runs")
        self.assertEqual(list(repo.parent.iterdir()), [repo], "nada suelto en el Escritorio")

    def test_checkpoint_no_versiona_el_estado_del_plan(self):
        repo = self._repo()
        with patch.object(soou.config, "load_config", return_value={}):
            wt, _, _ = soou._make_worktree(str(repo))
        (Path(wt) / "nuevo.txt").write_text("n", encoding="utf-8")
        (Path(wt) / ".soou-plan.json").write_text("{}", encoding="utf-8")
        self.assertTrue(soou._checkpoint(wt, "soou paso 1/1: x"))
        files = subprocess.run(["git", "-C", wt, "show", "--name-only", "--format=", "HEAD"],
                               capture_output=True, text=True).stdout.split()
        self.assertEqual(files, ["nuevo.txt"])


class ValidadoresTests(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_npm_sin_node_modules_se_omite_y_con_ellos_corre(self):
        (self.d / "package.json").write_text(json.dumps({"scripts": {
            "test": "vitest run", "lint": "eslint ."}}), encoding="utf-8")
        with patch.object(soou.shutil, "which", return_value="npm"):
            v = soou._detectar_validadores(self.d)
            self.assertEqual({n for n, _, _ in v}, {"npm lint", "npm test"})
            self.assertTrue(all(argv is None for _, argv, _ in v))
            (self.d / "node_modules").mkdir()
            v = soou._detectar_validadores(self.d)
            self.assertEqual(dict((n, a) for n, a, _ in v)["npm test"],
                             ["npm", "run", "test", "--silent"])

    def test_ignora_el_test_por_defecto_de_npm_init(self):
        (self.d / "package.json").write_text(json.dumps({"scripts": {
            "test": 'echo "Error: no test specified" && exit 1'}}), encoding="utf-8")
        self.assertEqual(soou._detectar_validadores(self.d), [])

    def test_detecta_tests_python(self):
        (self.d / "tests").mkdir()
        (self.d / "tests" / "test_x.py").write_text("", encoding="utf-8")
        nombres = [n for n, _, _ in soou._detectar_validadores(self.d)]
        self.assertTrue(set(nombres) & {"pytest", "unittest"})

    def test_soou_json_manda_sobre_la_autodeteccion(self):
        (self.d / "package.json").write_text(json.dumps({"scripts": {"test": "x"}}),
                                             encoding="utf-8")
        (self.d / ".soou.json").write_text(json.dumps({"validators": ["make check"]}),
                                           encoding="utf-8")
        self.assertEqual([n for n, _, _ in soou._detectar_validadores(self.d)], ["make check"])

    def test_los_tests_del_proyecto_no_heredan_variables_internas(self):
        # encontrado usando el harness de verdad: con BRAIN_DISTILL=1 la captura
        # se apaga y 5 tests del propio soou fallaban en falso dentro del worktree
        with patch.dict(os.environ, {"BRAIN_DISTILL": "1", "SOOU_DEPTH": "2"}):
            env = soou._entorno_validador()
        self.assertNotIn("BRAIN_DISTILL", env)
        self.assertNotIn("SOOU_DEPTH", env)
        self.assertEqual(env["CI"], "1")

    def test_un_test_que_falla_gatea_la_validacion(self):
        import sys
        (self.d / "tests").mkdir()
        (self.d / "tests" / "test_rojo.py").write_text(
            "import unittest\nclass T(unittest.TestCase):\n"
            "    def test_x(self):\n        self.assertEqual(1, 2)\n", encoding="utf-8")
        with patch.object(soou.harness, "_detectar_validadores", return_value=[
                ("unittest", [sys.executable, "-m", "unittest", "discover", "-s", "tests"], "")]):
            checks = {n: ok for n, ok, _ in soou._run_validators(str(self.d))}
        self.assertFalse(checks["unittest"])


class SessionsTamanoTests(Entorno):
    def test_sessions_muestra_el_tamano_total_en_kb(self):
        (soou.rutas.SESSIONS / "session-a.md").write_bytes(b"x" * 1024)
        sub = soou.rutas.SESSIONS / "sub"
        sub.mkdir()
        (sub / "session-b.md").write_bytes(b"y" * 512)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            soou.cmd_sessions(argparse.Namespace(limpiar=False, apply=False))
        salida = buf.getvalue()
        self.assertIn("2 sesiones", salida)
        self.assertIn("Tamaño total de sessions/raw: 1.5 KB", salida)


class ResultadoUtilTests(Entorno):
    """Casos sacados de correr el harness en tareas reales (2026-11-05)."""

    def _repo(self):
        repo = self.tmp / "repo"
        repo.mkdir()
        g = lambda *a: subprocess.run(["git", "-C", str(repo), *a], capture_output=True, check=True)
        g("init", "-q"); g("config", "user.email", "t@t"); g("config", "user.name", "t")
        (repo / "a.txt").write_text("a", encoding="utf-8")
        g("add", "-A"); g("commit", "-q", "-m", "base")
        return repo

    def test_escritura_sin_cambios_no_sirve_aunque_salga_0(self):
        repo = self._repo()
        antes = soou._estado_git(repo)
        util, motivo = soou._resultado_util(
            {"ok": True, "output": "No pude aplicar el cambio: helper_unknown_error"},
            True, repo, antes)
        self.assertFalse(util)
        self.assertIn("cambios", motivo)

    def test_con_cambios_decide_el_validador_aunque_diga_no_pude_correr_tests(self):
        repo = self._repo()
        antes = soou._estado_git(repo)
        (repo / "b.txt").write_text("b", encoding="utf-8")
        util, _ = soou._resultado_util(
            {"ok": True, "output": "Agregue el test. No pude ejecutar pytest."}, True, repo, antes)
        self.assertTrue(util)

    def test_lectura_que_admite_no_poder_no_sirve(self):
        self.assertFalse(soou._resultado_util(
            {"ok": True, "output": "I couldn't read the file."}, False)[0])
        self.assertTrue(soou._resultado_util(
            {"ok": True, "output": "La funcion reordena los agentes."}, False)[0])

    def test_plan_usa_el_fallback_cuando_el_primero_no_puede(self):
        repo = self._repo()

        class Inutil:
            name, can_write = "codex", True
            def available(self): return True
            def run(self, *a, **k): return {"ok": True, "output": "No pude: helper_unknown_error"}

        class Util:
            name, can_write = "claude-code", True
            def available(self): return True
            def run(self, prompt, cwd=None, **k):
                (Path(cwd) / "hecho.txt").write_text("x", encoding="utf-8")
                return {"ok": True, "output": "Listo."}

        cfg = {"caps": {}, "verticals": {}, "routing": {}, "models": {}, "reasoning": {},
               "agents": [], "core_markers": [], "capture_roots": [], "exclude_projects": []}
        with patch.object(soou.config, "load_config", return_value=cfg), \
             patch.object(soou.config, "_budget_reserve", return_value=True), \
             patch.object(soou.config, "_budget_consume"), \
             patch.object(soou.registro, "build_adapters", return_value=[Inutil(), Util()]), \
             patch.object(soou.indice, "rank_notes", return_value=([], "tfidf")), \
             patch.object(soou.harness, "_brain_context", return_value=""), \
             patch.object(soou.harness, "_run_validators", return_value=[]):
            wt, branch, _ = soou._make_worktree(str(repo))
            soou._run_plan_execute("tarea", ["haz algo"], [], {"routing": {}}, 0,
                                   wt=(str(repo), wt, branch))
        estado = json.loads((Path(wt) / ".soou-plan.json").read_text(encoding="utf-8"))
        self.assertEqual(estado["steps"][0]["status"], "done")
        self.assertEqual(estado["steps"][0]["agent"], "claude-code")
        self.assertTrue((Path(wt) / "hecho.txt").exists())


class _Agente:
    def __init__(self, name):
        self.name = name


class RoutingAprendidoTests(Entorno):
    def _trazas(self, agente, ok, n, nivel="complex"):
        for _ in range(n):
            with patch.object(soou.config, "load_config", return_value={}):
                soou._traza({"gen_ai.operation.name": "execute_step",
                             "gen_ai.agent.name": agente, "soou.level": nivel,
                             "soou.status": "ok" if ok else "error"})

    def test_sin_historial_respeta_el_orden(self):
        a = [_Agente("claude-code"), _Agente("codex")]
        with patch.object(soou.config, "load_config", return_value={}):
            self.assertEqual([x.name for x in soou._ordenar_por_historial(a, "complex")],
                             ["claude-code", "codex"])

    def test_degrada_al_que_falla_con_evidencia(self):
        self._trazas("claude-code", False, 4)
        self._trazas("codex", True, 4)
        a = [_Agente("claude-code"), _Agente("codex")]
        with patch.object(soou.config, "load_config", return_value={}):
            self.assertEqual([x.name for x in soou._ordenar_por_historial(a, "complex")],
                             ["codex", "claude-code"])
            # en otro nivel no hay evidencia: no cambia
            self.assertEqual([x.name for x in soou._ordenar_por_historial(a, "trivial")],
                             ["claude-code", "codex"])

    def test_pocas_muestras_no_reordenan(self):
        self._trazas("claude-code", False, 2)
        a = [_Agente("claude-code"), _Agente("codex")]
        with patch.object(soou.config, "load_config", return_value={}):
            self.assertEqual(soou._ordenar_por_historial(a, "complex")[0].name, "claude-code")


class ClasificadorYRecallTests(unittest.TestCase):
    def _tier(self, prompt):
        with patch.object(soou.config, "load_config", return_value={"verticals": {}}):
            return soou.classify_task(prompt, [], [])[0]

    def test_accion_corta_no_es_trivial(self):
        self.assertEqual(self._tier("refactor del router con tests"), "T2")
        self.assertEqual(self._tier("arregla el login"), "T1")

    def test_pregunta_corta_sigue_siendo_lookup(self):
        self.assertEqual(self._tier("que es soou"), "T0")

    def test_recall_con_texto_no_lee_stdin(self):
        with patch.object(soou.sesiones, "read_hook_stdin", side_effect=AssertionError("leyo stdin")), \
             patch.object(soou.indice, "compute_graph", return_value=([], [], [])):
            soou.cmd_recall(argparse.Namespace(text=["harness", "planner"]))


class RutasTests(unittest.TestCase):
    def test_ruta_relativa_nunca_cuenta_como_propia(self):
        # en Linux "C:/Windows/system32" es relativa: antes resolvia bajo el cwd
        self.assertFalse(soou._bajo(Path("C:/Windows/system32") if os.name != "nt"
                                    else Path("relativa/x"), Path.home()))
        self.assertTrue(soou._bajo(Path.home() / "proy", Path.home()))


class CapturaDeRunsTests(unittest.TestCase):
    def test_no_cosecha_la_sesion_del_agente_dentro_de_runs(self):
        base = Path.home() / "soou-runs-prueba"
        with patch.object(soou.sesiones, "_runs_dir", return_value=base), \
             patch.object(soou.config, "load_config", return_value={}):
            ok, motivo = soou._captura_permitida(str(base / "repo--soou-run-1"))
            self.assertFalse(ok)
            self.assertIn("run", motivo)
            self.assertTrue(soou._captura_permitida(str(Path.home() / "proyecto"))[0])

    def test_reconoce_la_subsesion_por_su_prompt(self):
        self.assertTrue(soou._es_subsesion_de_soou(
            ["**user:** Contexto del cerebro (soou):\n..."]))
        self.assertFalse(soou._es_subsesion_de_soou(["**user:** arregla el login"]))


class DatosVsCodigoTests(unittest.TestCase):
    def test_soou_home_manda(self):
        with patch.dict(os.environ, {"SOOU_HOME": str(Path(tempfile.gettempdir()) / "mi-soou")}):
            self.assertEqual(soou._data_root().name, "mi-soou")

    def test_checkout_usa_su_carpeta_e_instalado_usa_home(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SOOU_HOME", None)
            checkout = Path(tempfile.mkdtemp())
            (checkout / ".git").mkdir()
            with patch.object(soou.rutas, "CODE_DIR", checkout):            # repo clonado
                self.assertEqual(soou._data_root(), checkout)
            shutil.rmtree(checkout, ignore_errors=True)
            vacio = Path(tempfile.mkdtemp())
            with patch.object(soou.rutas, "CODE_DIR", vacio):               # site-packages
                self.assertEqual(soou._data_root(), Path.home() / ".soou")
            shutil.rmtree(vacio, ignore_errors=True)


class HooksTests(unittest.TestCase):
    def test_instala_precompact(self):
        home = Path(tempfile.mkdtemp())
        (home / ".claude").mkdir()
        with patch.object(Path, "home", return_value=home), \
             patch.object(soou.shutil, "which", return_value="claude"):
            soou._install_claude_hooks(force=True)
        hooks = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))["hooks"]
        self.assertIn("PreCompact", hooks)
        self.assertIn(" capture", hooks["PreCompact"][0]["hooks"][0]["command"])
        shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
