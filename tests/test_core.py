#!/usr/bin/env python3
"""Pruebas unitarias autocontenidas para funciones centrales de soou.py."""

import importlib.util
import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import Mock, patch


TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent

_spec = importlib.util.spec_from_file_location("soou", ROOT / "soou.py")
soou = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(soou)

# Los tests del planner ejecutan pasos simulados: sus trazas NO deben caer en el
# runs/ real (alimentan el learned routing). Cada modulo de test carga su propia
# instancia de soou, asi que esto no afecta a los demas.
_RUNS_TMP = Path(tempfile.mkdtemp(prefix="soou-test-runs-"))
soou._runs_dir = lambda: _RUNS_TMP


class CoreTests(unittest.TestCase):
    def test_extract_codex_rollout_supports_current_direct_message_events(self):
        records = [
            {"type": "session_meta", "payload": {"session_id": "current-id"}},
            {"type": "event_msg", "payload": {
                "type": "user_message", "message": "Resume este proyecto"}},
            {"type": "event_msg", "payload": {
                "type": "agent_message", "message": "Resumen listo", "phase": "final"}},
            {"type": "event_msg", "payload": {
                "type": "user_message", "message": "<environment_context>ruido</environment_context>"}},
        ]
        with tempfile.TemporaryDirectory() as tmp_dir:
            rollout = Path(tmp_dir) / "rollout.jsonl"
            rollout.write_text(
                "\n".join(json.dumps(record) for record in records),
                encoding="utf-8",
            )
            session_id, _cwd, body = soou.extract_codex_rollout(rollout)

        self.assertEqual(session_id, "current-id")
        self.assertEqual(body, [
            "**user:** Resume este proyecto",
            "**assistant:** Resumen listo",
        ])

    def test_extract_codex_rollout_keeps_legacy_item_completed_events(self):
        records = [
            {"type": "session_meta", "payload": {"session_id": "legacy-id"}},
            {"type": "event_msg", "payload": {
                "type": "item_completed",
                "item": {"type": "UserMessage", "content": [{"text": "Hola"}]},
            }},
            {"type": "event_msg", "payload": {
                "type": "item_completed",
                "item": {"type": "AgentMessage", "content": [{"text": "Hola tambien"}]},
            }},
        ]
        with tempfile.TemporaryDirectory() as tmp_dir:
            rollout = Path(tmp_dir) / "rollout.jsonl"
            rollout.write_text(
                "\n".join(json.dumps(record) for record in records),
                encoding="utf-8",
            )
            session_id, _cwd, body = soou.extract_codex_rollout(rollout)

        self.assertEqual(session_id, "legacy-id")
        self.assertEqual(body, ["**user:** Hola", "**assistant:** Hola tambien"])

    def test_pull_codex_sessions_merges_compacted_rollout_segments(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            source = root / "codex" / "2026" / "09" / "04"
            sessions = root / "brain" / "sessions" / "raw"
            source.mkdir(parents=True)
            first = source / "rollout-2026-09-04T12-00-00-first.jsonl"
            second = source / "rollout-2026-09-04T12-05-00-second.jsonl"
            first.write_text("\n".join(json.dumps(record) for record in [
                {"type": "session_meta", "payload": {"session_id": "shared-id", "cwd": str(Path.home() / "proyecto")}},
                {"type": "event_msg", "payload": {"type": "user_message", "message": "Primera parte " + "x" * 2100}},
            ]), encoding="utf-8")
            second.write_text("\n".join(json.dumps(record) for record in [
                {"type": "session_meta", "payload": {"session_id": "shared-id", "cwd": str(Path.home() / "proyecto")}},
                {"type": "event_msg", "payload": {"type": "agent_message", "message": "Segunda parte " + "y" * 2100}},
            ]), encoding="utf-8")
            with patch.object(soou.rutas, "CODEX_SESSIONS", root / "codex"), \
                    patch.object(soou.rutas, "SESSIONS", sessions), \
                    patch.object(soou.time, "time", return_value=10**12):
                imported = soou._pull_codex_sessions(refresh=True)

            dest = sessions / "2026" / "09" / "04" / "session-codex-shared-id.md"
            content = dest.read_text(encoding="utf-8")

        self.assertEqual(imported, 1)
        self.assertIn("**user:** Primera parte", content)
        self.assertIn("**assistant:** Segunda parte", content)
        self.assertIn(first.name, content)
        self.assertIn(second.name, content)

    def test_pending_sessions_can_filter_codex_source(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            sessions = Path(tmp_dir)
            (sessions / "session-codex-one.md").write_text(
                "<!-- distilled: no -->\n", encoding="utf-8")
            (sessions / "session-run-one.md").write_text(
                "<!-- distilled: no -->\n", encoding="utf-8")
            with patch.object(soou.rutas, "SESSIONS", sessions):
                codex = soou.pending_sessions("codex")
                other = soou.pending_sessions("other")

        self.assertEqual([path.name for path in codex], ["session-codex-one.md"])
        self.assertEqual([path.name for path in other], ["session-run-one.md"])

    def test_parse_session_raw_keeps_codex_ids_unique_and_stable(self):
        # Dos hilos de Codex con el mismo prefijo no deben colapsar en el mismo sid.
        ids = ["01a06da5-573c-7693-961d-b4770a376a9f",
               "01a06da5-573c-7693-961d-b4770a376aa0"]
        sids = []
        with tempfile.TemporaryDirectory() as tmp_dir:
            for session_id in ids:
                raw = Path(tmp_dir) / f"session-codex-{session_id}.md"
                raw.write_text(
                    "<!-- distilled: no -->\n"
                    "# Sesion codex\n\n"
                    "**user:** conserva el identificador",
                    encoding="utf-8",
                )
                parsed = soou._parse_session_raw(raw)
                self.assertEqual(parsed["source"], "codex")
                self.assertEqual(parsed["sid"], soou._sid_of_session(raw.name))
                sids.append(parsed["sid"])

        self.assertNotEqual(sids[0], sids[1])

    def test_parse_note_reads_frontmatter_and_content(self):
        content = (
            "---\n"
            "name: demo\n"
            "area: code\n"
            "type: concept\n"
            "sources: []\n"
            "---\n"
            "# Demo\n"
            "cuerpo aqui"
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            note_path = Path(tmp_dir) / "demo.md"
            note_path.write_text(content, encoding="utf-8")

            note = soou.parse_note(note_path)

        self.assertIn("name", note)
        self.assertIn("body", note)
        self.assertIsInstance(note.get("meta"), dict)
        self.assertEqual(note["meta"].get("area"), "code")
        self.assertEqual(note["meta"].get("type"), "concept")

    def test_task_kind_classifies_supported_tasks(self):
        cases = {
            " crea un endpoint ": "build",
            " refactoriza el modulo ": "edit",
            " audita la seguridad ": "audit",
            " que hace esto ": "question",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(soou.task_kind(text), expected)

    def test_textrank_returns_at_most_requested_original_sentences(self):
        sentences = [
            f"Esta es la oracion numero {number} y contiene suficiente texto para superar claramente el filtro establecido."
            for number in range(1, 10)
        ]
        parsed = soou._sentences(" ".join(sentences))

        self.assertGreaterEqual(len(parsed), 8)
        ranked = soou._textrank(parsed, 3)

        self.assertLessEqual(len(ranked), 3)
        self.assertTrue(all(sentence in parsed for sentence in ranked))

    def test_keyphrases_prioritizes_repeated_alpha(self):
        result = soou._keyphrases(
            ["alpha", "alpha", "beta"],
            [["gamma"], ["delta"]],
            3,
        )

        self.assertIsInstance(result, list)
        self.assertIn("alpha", result[:3])

    def test_auto_links_detects_note_name_with_spaces(self):
        notes = [{"name": "llm-wiki", "meta": {"type": "concept"}}]

        result = soou._auto_links("hablo del patron llm wiki hoy", notes, 8)

        self.assertIsInstance(result, list)
        self.assertIn("llm-wiki", result)

    def test_run_plan_resume_skips_completed_subtasks_and_updates_state(self):
        agent = Mock(name="agent")
        agent.name = "codex"
        agent.available.return_value = True
        agent.can_write = True
        agent.run.return_value = {"ok": True, "output": "ok"}
        subs = ["hecha", "pendiente uno", "pendiente dos"]

        with tempfile.TemporaryDirectory() as tmp_dir:
            state_path = Path(tmp_dir) / ".soou-plan.json"
            state_path.write_text(json.dumps({"done": 1}), encoding="utf-8")
            completed = Mock(returncode=0, stdout="", stderr="")
            cfg = {"routing": {"direct": ["codex"]}}
            with patch.object(soou.harness, "_make_worktree") as make_worktree, \
                    patch.object(soou.sesiones, "_runs_dir", return_value=Path(tmp_dir) / "runs"), \
                    patch.object(soou.config, "_budget_reserve", return_value=True), \
                    patch.object(soou.config, "_budget_consume"), \
                    patch.object(soou.indice, "rank_notes", return_value=([], None)), \
                    patch.object(soou.router, "classify_task", return_value=(0, "", "")), \
                    patch.object(soou.router, "flow_for", return_value=("direct", "")), \
                    patch.object(soou.registro, "build_adapters", return_value=[agent]), \
                    patch.object(soou.config, "_model_for", return_value=None), \
                    patch.object(soou.harness, "_brain_context", return_value=""), \
                    patch.object(soou.harness, "_run_validators", return_value=[]), \
                    patch.object(soou.subprocess, "run", return_value=completed):
                soou._run_plan_execute(
                    "plan", subs, [], cfg, 0,
                    wt=("root", tmp_dir, "soou/run-test"), start=1)

            make_worktree.assert_not_called()
            self.assertEqual(agent.run.call_count, 2)
            self.assertEqual(json.loads(state_path.read_text(encoding="utf-8"))["done"], 3)

    def test_run_plan_new_worktree_writes_initial_state(self):
        # sin cupo no se lanza NINGUN agente (antes este test lanzaba los reales)
        agente_que_no_debe_correr = Mock(name="agente")
        agente_que_no_debe_correr.name = "codex"
        agente_que_no_debe_correr.available.return_value = True
        agente_que_no_debe_correr.can_write = True
        with tempfile.TemporaryDirectory() as tmp_dir:
            with patch.object(soou.harness, "_git_root", return_value="repo"), \
                    patch.object(
                        soou.harness, "_make_worktree",
                        return_value=(tmp_dir, "soou/run-test", None)), \
                    patch.object(soou.config, "_budget_reserve", return_value=False), \
                    patch.object(soou.sesiones, "_runs_dir", return_value=Path(tmp_dir) / "runs"), \
                    patch.object(soou.registro, "build_adapters",
                                 return_value=[agente_que_no_debe_correr]), \
                    patch.object(soou.config, "_budget_left_str", return_value="0"), \
                    patch.object(soou.harness, "_run_validators", return_value=[]):
                soou._run_plan_execute(
                    "plan original", ["una", "dos"], [], {"routing": {}}, 0)

            state = json.loads(
                (Path(tmp_dir) / ".soou-plan.json").read_text(encoding="utf-8"))
            self.assertEqual(state, {
                "prompt": "plan original",
                "subs": ["una", "dos"],
                "done": 0,
                "branch": "soou/run-test",
                "root": "repo",
                "wt_path": tmp_dir,
                "base": None,              # tmp_dir no es repo: sin base conocida
                "steps": [
                    {"sub": "una", "agent": None, "model": None, "status": "pending"},
                    {"sub": "dos", "agent": None, "model": None, "status": "pending"},
                ],
                "validacion": "sin_verificacion_funcional",   # sin validadores: nunca "ok"
                "run_id": state["run_id"],
            })
        agente_que_no_debe_correr.run.assert_not_called()

    def test_cmd_run_resume_uses_saved_plan_before_classifying(self):
        state = {
            "prompt": "plan original",
            "subs": ["hecha", "pendiente"],
            "done": 1,
            "branch": "soou/run-test",
            "root": "repo",
            "wt_path": "worktree",
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            state["wt_path"] = tmp_dir
            (Path(tmp_dir) / ".soou-plan.json").write_text(
                json.dumps(state), encoding="utf-8")
            with patch.object(soou.indice, "load_notes", return_value=["notes"]), \
                    patch.object(soou.config, "load_config", return_value={"config": True}), \
                    patch.object(soou.router, "classify_task") as classify, \
                    patch.object(soou.harness, "_run_plan_execute") as execute:
                soou.cmd_run(Namespace(task=[], resume=tmp_dir))

            classify.assert_not_called()
            execute.assert_called_once_with(
                "plan original", ["hecha", "pendiente"], ["notes"],
                {"config": True}, 0,
                wt=("repo", tmp_dir, "soou/run-test"), start=1)


if __name__ == "__main__":
    unittest.main()
