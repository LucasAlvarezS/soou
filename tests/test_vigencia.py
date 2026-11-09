"""Procedencia y vigencia de las notas: reemplazos, lint de cadenas, el destilado
y la captura de runs que escriben procedencia, y la migracion opcional. Sin
agentes y sin datos del usuario."""

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
from soou_core import config, contexto, destilado, harness, indice, rutas, vigencia  # noqa: E402


def _n(nombre, cuerpo="cuerpo", tipo="decision", **meta):
    return {"name": nombre, "area": "code", "body": cuerpo, "path": Path(f"{nombre}.md"),
            "meta": {"type": tipo, **meta}}


class ReemplazosTests(unittest.TestCase):
    def test_lista_acepta_las_formas_del_frontmatter(self):
        self.assertEqual(vigencia.lista("[a, b]"), ["a", "b"])
        self.assertEqual(vigencia.lista("[[a]]"), ["a"])
        self.assertEqual(vigencia.lista(["a", "a"]), ["a"])
        self.assertEqual(vigencia.lista(""), [])

    def test_sin_campos_es_vigente(self):
        self.assertEqual(vigencia.estado(_n("x")), "vigente")
        self.assertEqual(vigencia.estado(_n("x", status="superseded")), "reemplazada")

    def test_cuenta_las_dos_direcciones(self):
        notas = [_n("vieja"), _n("nueva", supersedes="[vieja]"),
                 _n("otra-vieja", superseded_by="otra-nueva"), _n("otra-nueva")]
        mapa = vigencia.reemplazos(notas)
        self.assertEqual(mapa["vieja"], "nueva")
        self.assertEqual(mapa["otra-vieja"], "otra-nueva")
        self.assertNotIn("nueva", mapa)

    def test_una_reemplazada_no_reemplaza(self):
        mapa = vigencia.reemplazos([_n("a"), _n("b", status="reemplazada", supersedes="a")])
        self.assertNotIn("a", mapa)
        self.assertEqual(mapa["b"], "")

    def test_el_contexto_excluye_por_el_supersedes_de_la_nueva(self):
        notas = [_n("despliegue-viejo", "Despliegue del cohete por etapas, version vieja."),
                 _n("despliegue-nuevo", "Despliegue del cohete por etapas, version nueva.",
                    supersedes="despliegue-viejo")]
        with patch.object(config, "load_config", return_value={"private_paths": []}):
            paq = contexto.armar_paquete("despliegue del cohete por etapas", notas)
        self.assertNotIn("despliegue-viejo", [p.nombre for p in paq.piezas])
        self.assertIn(("despliegue-viejo", "reemplazada por despliegue-nuevo"), paq.excluidas)

    def test_lo_afirmado_por_un_agente_llega_marcado_y_dentro_del_presupuesto(self):
        nota = _n("hallazgo", "El bug estaba en el parser. " * 20, verification="afirmado_por_agente")
        frag = contexto._fragmento(nota, 80)
        self.assertTrue(frag.startswith("[sin comprobar] "))
        self.assertLessEqual(len(frag), 80)
        self.assertFalse(contexto._fragmento(_n("h", "x", verification="sin_verificar"), 80)
                         .startswith("["), "sin_verificar no se marca: seria ruido")


class RevisarTests(unittest.TestCase):
    def _problemas(self, notas):
        return [(p["nota"], p["problema"]) for p in vigencia.revisar(notas)]

    def test_notas_sin_campos_no_dan_problemas(self):
        self.assertEqual(self._problemas([_n("a"), _n("b", tipo="concept")]), [])

    def test_cadena_bien_formada_no_da_problemas(self):
        self.assertEqual(self._problemas([
            _n("v1", status="reemplazada", superseded_by="v2"),
            _n("v2", status="vigente", supersedes="v1")]), [])

    def test_cadena_rota(self):
        probs = self._problemas([_n("v2", supersedes="v1")])
        self.assertIn(("v2", "cadena rota: supersedes -> [[v1]] no existe"), probs)

    def test_la_vieja_sigue_vigente(self):
        probs = self._problemas([_n("v1"), _n("v2", supersedes="v1")])
        self.assertTrue(any(n == "v1" and "sigue vigente aunque [[v2]]" in t for n, t in probs))

    def test_dos_vigentes_reemplazan_a_la_misma(self):
        probs = self._problemas([_n("v1", status="reemplazada", superseded_by="v2"),
                                 _n("v2", supersedes="v1"), _n("v3", supersedes="v1")])
        self.assertTrue(any(n == "v1" and "dos vigentes" in t and "[[v3]]" in t
                            for n, t in probs))

    def test_cadena_incompleta_y_vigente_marcada(self):
        probs = self._problemas([_n("v1", status="vigente", superseded_by="v2"), _n("v2")])
        textos = [t for n, t in probs if n == "v1"]
        self.assertTrue(any("marcada vigente" in t for t in textos))
        self.assertTrue(any("cadena incompleta" in t for t in textos))

    def test_ciclo_y_valores_desconocidos(self):
        probs = self._problemas([
            _n("a", status="reemplazada", superseded_by="b", supersedes="b"),
            _n("b", status="reemplazada", superseded_by="a", supersedes="a"),
            _n("c", status="dudosa", verification="segurisimo")])
        textos = " | ".join(t for _, t in probs)
        self.assertIn("ciclo de reemplazos", textos)
        self.assertIn("status desconocido 'dudosa'", textos)
        self.assertIn("verification desconocida 'segurisimo'", textos)


class CabeceraTests(unittest.TestCase):
    def test_solo_una_prueba_funcional_es_tests_passed(self):
        self.assertEqual(vigencia.de_veredicto("funcional"), "tests_passed")
        for v in ("fallo", "sin_verificacion_funcional", None):
            self.assertEqual(vigencia.de_veredicto(v), "afirmado_por_agente")

    def test_de_cabecera(self):
        run = "<!-- distilled: no -->\n# Sesion run\nFuente: soou run (codex)\nRun: r-1\n" \
              "Verificacion: tests_passed\n\n**user:** x"
        self.assertEqual(vigencia.de_cabecera(run),
                         {"source_run": "r-1", "verification": "tests_passed"})
        viejo = "<!-- distilled: no -->\nFuente: soou run (codex)\n\n**user:** x"
        self.assertEqual(vigencia.de_cabecera(viejo)["verification"], "afirmado_por_agente")
        charla = "<!-- distilled: no -->\n# Sesion claude-code\n\n**user:** hola"
        self.assertEqual(vigencia.de_cabecera(charla), {"verification": "sin_verificar"})
        trucha = "Fuente: soou run (x)\nVerificacion: revisadisimo\n"
        self.assertEqual(vigencia.de_cabecera(trucha)["verification"], "afirmado_por_agente")


class _Datos(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.previos = {n: getattr(rutas, n) for n in ("ROOT", "WIKI", "SESSIONS", "DB_PATH")}
        rutas.ROOT, rutas.WIKI = self.tmp, self.tmp / "wiki"
        rutas.SESSIONS, rutas.DB_PATH = self.tmp / "sessions" / "raw", self.tmp / "soou.db"
        (rutas.WIKI / "code").mkdir(parents=True)
        self.pila = contextlib.ExitStack()
        self.pila.enter_context(patch.object(config, "load_config",
                                             return_value={"private_paths": []}))

    def tearDown(self):
        self.pila.close()
        for n, v in self.previos.items():
            setattr(rutas, n, v)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _sesion(self, nombre, cabecera=""):
        d = rutas.SESSIONS / "2026" / "11" / "01"
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"session-{nombre}.md"
        f.write_text("<!-- distilled: no -->\n# Sesion claude-code 2026-11-01\n" + cabecera
                     + "\n**user:** como desplegamos el cohete por etapas\n\n**assistant:** "
                     + "Se despliega por etapas con verificacion entre cada una. " * 4,
                     encoding="utf-8")
        return f


class CapturaTests(_Datos):
    def _capturar(self, **kw):
        with contextlib.redirect_stdout(io.StringIO()):
            harness._capture_run("tarea", "codex", "respuesta larga " * 30, run_id="r-9", **kw)
        return next(rutas.SESSIONS.rglob("session-r-9.md")).read_text(encoding="utf-8")

    def test_tests_aprobados(self):
        cab = self._capturar(verificado="si (aprobadas: unittest)", veredicto="funcional")
        self.assertIn("Verificacion: tests_passed\n", cab)
        self.assertEqual(vigencia.de_cabecera(cab)["source_run"], "r-9")

    def test_sin_validadores_es_lo_que_afirmo_el_agente(self):
        self.assertIn("Verificacion: afirmado_por_agente\n", self._capturar())


class DestiladoTests(_Datos):
    def _lite(self, sesion):
        notas = indice.load_notes()
        nombre = destilado.distill_lite_one(sesion, notas, indice.build_corpus(notas))
        return indice.buscar_nota(nombre)

    def test_nivel_a_escribe_la_procedencia(self):
        nota = self._lite(self._sesion("abc", "Fuente: soou run (codex)\nRun: r-1\n"
                                              "Verificacion: tests_passed\n"))
        m = nota["meta"]
        self.assertEqual(m["source_session"], "sessions/raw/2026/11/01/session-abc.md")
        self.assertEqual((m["source_run"], m["verification"]), ("r-1", "tests_passed"))
        self.assertTrue(m["recorded_at"])

    def test_redestilar_conserva_recorded_at(self):
        s = self._sesion("abc")
        primera = self._lite(s)["meta"]["recorded_at"]
        texto = s.read_text(encoding="utf-8").replace("distilled: lite", "distilled: no")
        s.write_text(texto, encoding="utf-8")
        with patch.object(vigencia, "ahora", return_value="2099-01-01T00:00:00"):
            self.assertEqual(self._lite(s)["meta"]["recorded_at"], primera)

    def test_el_prompt_del_nivel_b_pide_procedencia_sin_inventar_verificacion(self):
        p = destilado.build_distill_prompt([self._sesion("abc")], [])
        for clave in ("source_session", "recorded_at", "verification", "supersedes",
                      "superseded_by", "NUNCA escribas `tests_passed`"):
            self.assertIn(clave, " ".join(p.split()))


class MigracionTests(_Datos):
    def _generada(self, nombre, pinned="false", extra=""):
        f = rutas.WIKI / "code" / f"{nombre}.md"
        f.write_text(f"---\nname: {nombre}\narea: code\ntype: session\nsources: []\n"
                     f"origin: distill-lite\npinned: {pinned}\nupdated: 2026-11-02{extra}\n---\n\n"
                     "# resumen\n", encoding="utf-8")
        return f

    def test_vista_previa_no_escribe_y_aplicar_si(self):
        self._sesion("abc")
        f = self._generada("sesion-claude-code-abc")
        antes = f.read_text(encoding="utf-8")
        cambios = destilado.migrar_procedencia(aplicar=False)
        self.assertEqual(cambios[0][0], "sesion-claude-code-abc")
        self.assertEqual(f.read_text(encoding="utf-8"), antes)
        destilado.migrar_procedencia(aplicar=True)
        m = indice.parse_note(f)["meta"]
        self.assertEqual(m["source_session"], "sessions/raw/2026/11/01/session-abc.md")
        self.assertEqual((m["recorded_at"], m["verification"]), ("2026-11-02", "sin_verificar"))
        self.assertTrue(indice.parse_note(f)["body"].startswith("# resumen"))
        self.assertEqual(destilado.migrar_procedencia(aplicar=False), [], "idempotente")

    def test_no_toca_pinned_ni_notas_humanas(self):
        self._sesion("abc")
        self._sesion("def")
        fijada = self._generada("sesion-claude-code-abc", pinned="true")
        humana = rutas.WIKI / "code" / "sesion-claude-code-def.md"
        humana.write_text("---\nname: sesion-claude-code-def\ntype: concept\n---\n\nx",
                          encoding="utf-8")
        antes = (fijada.read_text(encoding="utf-8"), humana.read_text(encoding="utf-8"))
        self.assertEqual(destilado.migrar_procedencia(aplicar=True), [])
        self.assertEqual((fijada.read_text(encoding="utf-8"),
                          humana.read_text(encoding="utf-8")), antes)


class EscrituraYLintTests(_Datos):
    def test_guardar_nota_registra_procedencia_y_conserva_recorded_at(self):
        proc = {"recorded_at": "2026-11-01T10:00:00", "verification": "afirmado_por_agente",
                "source_run": ""}
        indice.guardar_nota("hallazgo", "primera version", area="code", procedencia=proc)
        indice.guardar_nota("hallazgo", "segunda version", area="code",
                            procedencia={**proc, "recorded_at": "2099-01-01T00:00:00"})
        m = indice.buscar_nota("hallazgo")["meta"]
        self.assertEqual(m["recorded_at"], "2026-11-01T10:00:00")
        self.assertEqual(m["verification"], "afirmado_por_agente")
        self.assertNotIn("source_run", m, "los campos vacios no se escriben")

    def test_el_lint_cuenta_los_problemas_de_vigencia(self):
        (rutas.WIKI / "code" / "v2.md").write_text(
            "---\nname: v2\ntype: decision\nsupersedes: v1\n---\n\nnueva", encoding="utf-8")
        import soou
        salida = io.StringIO()
        with contextlib.redirect_stdout(salida):
            soou.cmd_lint(None)
        texto = salida.getvalue()
        self.assertIn("Vigencia (reemplazos rotos o vigentes en conflicto): 1", texto)
        self.assertIn("v2: cadena rota: supersedes -> [[v1]] no existe", texto)


if __name__ == "__main__":
    unittest.main()
