"""Modulo ui de soou (extraido de soou.py).

soou.py lo re-exporta; dentro de soou_core se usa como `ui.nombre`."""

from pathlib import Path
import datetime as dt
import json
import os
import subprocess
import sys
import webbrowser
from soou_core import config, consola, ejecutor, indice, registro, router, rutas, sesiones


def _note_badge(n: dict):
    m = n["meta"]
    if m.get("pinned") == "true":
        return "pinned"
    if m.get("origin") == "manual":
        return "manual"
    if indice.note_placeholder(n):
        return "placeholder"
    return None


class BrainUIApi:
    """API que el frontend (pywebview) invoca via window.pywebview.api.*.
    Todo lee del core (soou.db / .md). Retornos serializables a JSON."""

    def panorama(self):
        notes, _, edges = indice.compute_graph()
        by_type = {}
        for n in notes:
            t = n["meta"].get("type", "?")
            by_type[t] = by_type.get(t, 0) + 1
        concepts = sum(v for t, v in by_type.items() if t in indice.CONCEPT_TYPES)
        undistilled = sum(1 for n in notes if n["meta"].get("type") == "doc"
                          and n["meta"].get("distilled") != "yes")
        base = concepts + undistilled
        cov = round(concepts / base * 100) if base else 0
        names = {n["name"] for n in notes}
        broken = sum(1 for n in notes for l in indice.note_links(n) if l not in names)
        referenced = set()
        for n in notes:
            referenced.update(indice.note_links(n))
        orphans = sum(1 for n in notes if n["name"] not in referenced
                      and n["meta"].get("type") not in ("doc", "session", "rule"))
        vert = {}
        for n in notes:
            v = router.task_vertical(n) or "otros"
            vert[v] = vert.get(v, 0) + 1
        recent = sorted(notes, key=lambda n: n["meta"].get("updated", ""), reverse=True)[:6]
        recientes = [{"name": n["name"], "vert": (router.task_vertical(n) or n["area"]),
                      "type": n["meta"].get("type", "?"), "badge": _note_badge(n)}
                     for n in recent]
        return {"notes": len(notes), "edges": len(edges), "concepts": concepts,
                "coverage": cov, "broken": broken, "orphans": orphans,
                "pending": len(sesiones.pending_sessions()),
                "verticals": sorted(vert.items(), key=lambda x: -x[1]),
                "recent": recientes}

    def search(self, query):
        notes = indice.load_notes()
        fused, backend = indice.rank_notes(query or "", notes)
        out = []
        for s, n in fused[:25]:
            body = " ".join(n["body"].split())
            out.append({"name": n["name"], "area": n["area"],
                        "type": n["meta"].get("type", "?"), "score": round(s, 4),
                        "snippet": body[:200], "badge": _note_badge(n)})
        return {"backend": backend, "results": out}

    def note(self, name):
        notes = indice.load_notes()
        n = next((x for x in notes if x["name"] == name), None)
        if not n:
            return None
        names = {x["name"] for x in notes}
        return {"name": n["name"], "area": n["area"], "type": n["meta"].get("type", "?"),
                "body": n["body"],
                "links_out": [l for l in indice.note_links(n) if l in names],
                "links_in": [x["name"] for x in notes if name in indice.note_links(x)]}

    def graph(self):
        notes, nodes, edges = indice.compute_graph()
        vmap = {n["name"]: (router.task_vertical(n) or n["area"]) for n in notes}
        tmap = {n["name"]: n["meta"].get("type", "?") for n in notes}
        return {"nodes": [{"id": x["id"], "vert": vmap.get(x["id"], "?"),
                           "indeg": x["indeg"], "type": tmap.get(x["id"], "?")}
                          for x in nodes],
                "edges": [{"s": e["source"], "t": e["target"]} for e in edges]}

    def create_note(self, title, area="personal", ntype="concept", body="", pinned=True):
        area = area if area in indice.AREAS else "personal"
        name = indice.slugify(title or "")
        if not name or name == "sin-nombre":
            return {"ok": False, "error": "título vacío"}
        dest = rutas.WIKI / area / f"{name}.md"
        if dest.exists():
            return {"ok": False, "error": f"ya existe una nota '{name}'"}
        dest.parent.mkdir(parents=True, exist_ok=True)
        fm = ("---\n"
              f"name: {name}\narea: {area}\ntype: {ntype}\n"
              "sources: []\norigin: manual\n"
              f"pinned: {'true' if pinned else 'false'}\n"
              f"updated: {indice.today()}\n---\n\n"
              f"# {title}\n\n{body or ''}\n")
        dest.write_text(fm, encoding="utf-8")
        indice.build_index()   # el .md es la verdad; reindexa soou.db
        return {"ok": True, "name": name, "area": area}

    def health(self):
        return indice.resumen_salud()

    def sessions(self):
        out = []
        if rutas.SESSIONS.exists():
            for f in sorted(rutas.SESSIONS.rglob("session-*.md"), reverse=True):
                head = f.read_text(encoding="utf-8", errors="ignore")[:80]
                parts = f.relative_to(rutas.SESSIONS).parts
                out.append({"file": f.stem,
                            "date": "/".join(parts[:3]) if len(parts) >= 4 else "",
                            "distilled": "distilled: yes" in head,
                            "source": "codex" if "codex" in f.name else "claude-code"})
        pending = sum(1 for s in out if not s["distilled"])
        return {"total": len(out), "pending": pending, "sessions": out[:50]}

    def profile(self):
        cfg = config.load_config()
        caps = cfg["caps"]
        agents = []
        for ad in registro.build_adapters():
            agents.append({"name": ad.name, "channels": "harness",
                           "status": "live" if ad.available() else "sin CLI"})
        return {"profile": cfg["profile"], "profiles": cfg["profiles"],
                "caps": caps, "retrieval": "bm25 + tfidf · rrf",
                "store": "sqlite · soou.db", "notes": len(indice.load_notes()),
                "agents": agents}

    def budget_detail(self):
        """Detalle del presupuesto diario y sus últimos siete días para la UI."""
        cfg = config.load_config()
        cap = cfg["caps"].get("budget_per_day")
        try:
            ledger = json.loads(rutas.BUDGET_PATH.read_text(encoding="utf-8"))
            if not isinstance(ledger, dict):
                ledger = {}
        except Exception:
            ledger = {}

        history = []
        today_date = dt.date.today()
        for days_ago in range(6, -1, -1):
            date = today_date - dt.timedelta(days=days_ago)
            date_key = date.isoformat()
            try:
                count = int(ledger.get(date_key, 0))
            except (TypeError, ValueError):
                count = 0
            history.append({"date": date_key, "count": count})

        return {"profile": cfg["profile"],
                "per_day": cap if cap is not None else "ilimitado",
                "today": config._budget_today(), "left": config._budget_left_str(),
                "history": history}

    def reindex(self):
        notes = indice.build_index()
        names = {n["name"] for n in notes}
        broken = sum(1 for n in notes for l in indice.note_links(n) if l not in names)
        return {"ok": True, "notes": len(notes), "broken": broken}

    def distill_view(self):
        """Estado resumido de la tuberia de destilacion para la UI."""
        try:
            pending = len(sesiones.pending_sessions())
        except Exception:
            pending = 0
        try:
            lite = len(sesiones.lite_pending())
        except Exception:
            lite = 0
        try:
            concepts = sum(1 for n in indice.load_notes()
                           if n.get("meta", {}).get("type") == "concept")
        except Exception:
            concepts = 0

        last_sync = None
        try:
            sync_path = rutas.ROOT / "last-sync.json"
            if sync_path.exists():
                data = json.loads(sync_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    last_sync = {"timestamp": data.get("timestamp"),
                                 "notes_created": data.get("notes_created"),
                                 "names": data.get("names"),
                                 "agent": data.get("agent")}
        except Exception:
            last_sync = None
        return {"pending": pending, "lite_pending": lite,
                "concepts": concepts, "last_sync": last_sync}

    def sync_now(self):
        """Lanza la sincronizacion Nivel A como proceso detached."""
        if ejecutor.lanzar_desacoplado(["sync"], cwd=rutas.ROOT):
            return {"ok": True, "msg": "sincronizacion (Nivel A) disparada"}
        return {"ok": False, "msg": "no se pudo lanzar la sincronizacion"}

    def sync_deep(self):
        """Lanza la sincronizacion profunda Niveles A+B como proceso detached."""
        if ejecutor.lanzar_desacoplado(["sync", "--deep"], cwd=rutas.ROOT):
            return {"ok": True, "msg": "sincronizacion profunda (A+B) disparada"}
        return {"ok": False, "msg": "no se pudo lanzar la sincronizacion profunda"}

    def orchestration_config(self):
        """Configuracion de agentes, routing y presupuesto para la UI.
        Cada lectura es tolerante a errores para no bloquear la vista."""
        try:
            cfg = config.load_config() or {}
        except Exception:
            cfg = {}
        if not isinstance(cfg, dict):
            cfg = {}

        agents = []
        try:
            adapters = registro.build_adapters()
        except Exception:
            adapters = []
        for ad in adapters:
            try:
                name = str(getattr(ad, "name", "desconocido"))
            except Exception:
                name = "desconocido"
            try:
                available = bool(ad.available())
            except Exception:
                available = False
            try:
                can_write = bool(getattr(ad, "can_write", True))
            except Exception:
                can_write = True
            agents.append({"name": name, "available": available, "can_write": can_write,
                           "role": "escribe" if can_write else "solo-lectura"})

        routing = cfg.get("routing", {})
        models = cfg.get("models", {})
        caps = cfg.get("caps", {})
        if not isinstance(routing, dict):
            routing = {}
        if not isinstance(models, dict):
            models = {}
        if not isinstance(caps, dict):
            caps = {}
        try:
            today_count = config._budget_today()
        except Exception:
            today_count = 0
        try:
            left = config._budget_left_str()
        except Exception:
            left = "ilimitado"
        return {"agents": agents, "routing": routing, "models": models,
                "budget": {"profile": cfg.get("profile", "single"),
                           "per_day": caps.get("budget_per_day") or "ilimitado",
                           "today": today_count, "left": left}}

    # --- Edicion de config desde la UI --------------------------------------
    def config_get(self):
        """Estado editable actual de soou.config.json para el editor de la UI.
        Lee de load_config() (perfil/routing/models/verticals/markers/exclude)
        y ademas del archivo crudo para el override.budget_per_day.
        Tolerante a errores: cualquier fallo devuelve valores vacios/None."""
        try:
            cfg = config.load_config() or {}
        except Exception:
            cfg = {}
        if not isinstance(cfg, dict):
            cfg = {}
        raw = {}
        try:
            if rutas.CONFIG_PATH.exists():
                raw = json.loads(rutas.CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        override = raw.get("override") or {}
        if not isinstance(override, dict):
            override = {}
        budget_per_day = override.get("budget_per_day") if "budget_per_day" in override else None
        return {
            "profile": cfg.get("profile", "single"),
            "profiles": cfg.get("profiles") or list(config._DEFAULT_PROFILES.keys()),
            "routing": cfg.get("routing") or {},
            "models": cfg.get("models") or {},
            "verticals": cfg.get("verticals") or {},
            "core_markers": cfg.get("core_markers") or [],
            "exclude_projects": cfg.get("exclude_projects") or [],
            "budget_per_day": budget_per_day,
        }

    def config_save(self, patch):
        """Escribe soou.config.json de forma segura desde la UI. Valida TODO
        antes de tocar el disco para no dejar el archivo corrupto. Aplica solo
        las claves presentes en 'patch'. Devuelve {ok:True} o {ok:False,error}."""
        if not isinstance(patch, dict):
            return {"ok": False, "error": "patch invalido"}
        try:
            data = json.loads(rutas.CONFIG_PATH.read_text(encoding="utf-8")) if rutas.CONFIG_PATH.exists() else {}
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}

        # --- VALIDACION (antes de escribir nada) ---
        if "profile" in patch:
            prof = patch["profile"]
            valid = set(config._DEFAULT_PROFILES) | set(data.get("profiles") or {})
            if not isinstance(prof, str) or prof not in valid:
                return {"ok": False, "error": "perfil invalido"}
        if "routing" in patch:
            routing = patch["routing"]
            if not isinstance(routing, dict) or not all(
                    isinstance(v, list) and all(isinstance(x, str) for x in v)
                    for v in routing.values()):
                return {"ok": False, "error": "routing invalido"}
        if "models" in patch:
            models = patch["models"]
            if not isinstance(models, dict) or not all(
                    isinstance(v, dict) for v in models.values()):
                return {"ok": False, "error": "models invalido"}
        if "verticals" in patch:
            verticals = patch["verticals"]
            if not isinstance(verticals, dict) or not all(
                    isinstance(v, list) for v in verticals.values()):
                return {"ok": False, "error": "verticals invalido"}
        if "core_markers" in patch and not isinstance(patch["core_markers"], list):
            return {"ok": False, "error": "core_markers invalido"}
        if "exclude_projects" in patch and not isinstance(patch["exclude_projects"], list):
            return {"ok": False, "error": "exclude_projects invalido"}
        if "budget_per_day" in patch:
            bpd = patch["budget_per_day"]
            if bpd is not None and not (isinstance(bpd, int) and not isinstance(bpd, bool)):
                return {"ok": False, "error": "budget_per_day invalido"}

        # --- APLICACION (solo claves presentes) ---
        for key in ("routing", "models", "verticals", "core_markers", "exclude_projects"):
            if key in patch:
                data[key] = patch[key]
        if "profile" in patch:
            data["profile"] = patch["profile"]
        if "budget_per_day" in patch:
            override = data.get("override")
            if not isinstance(override, dict):
                override = {}
            if patch["budget_per_day"] is None:
                override.pop("budget_per_day", None)
            else:
                override["budget_per_day"] = patch["budget_per_day"]
            data["override"] = override

        try:
            rutas.CONFIG_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                                   encoding="utf-8")
        except Exception as e:
            return {"ok": False, "error": f"no se pudo escribir: {e}"}
        return {"ok": True}

    # --- Runs / Plan activo -------------------------------------------------
    def runs(self):
        """Lista los worktrees de runs activos (rama soou/run-*), leyendo su
        .soou-plan.json si existe. Tolerante a errores: si git falla, devuelve []."""
        try:
            r = subprocess.run(["git", "worktree", "list", "--porcelain"],
                               cwd=str(rutas.ROOT), capture_output=True, text=True)
            if r.returncode != 0:
                return []
        except Exception:
            return []
        out = []
        wt_path, branch = None, None
        for line in (r.stdout or "").splitlines() + [""]:
            if line.startswith("worktree "):
                wt_path = line[len("worktree "):].strip()
                branch = None
            elif line.startswith("branch refs/heads/"):
                branch = line[len("branch refs/heads/"):].strip()
            elif line == "":
                if wt_path and branch and branch.startswith("soou/run-"):
                    prompt, subs, done, total, status = "(run sin plan)", [], 0, 0, "activo"
                    plan = Path(wt_path) / ".soou-plan.json"
                    try:
                        if plan.exists():
                            st = json.loads(plan.read_text(encoding="utf-8"))
                            subs = st.get("subs", []) or []
                            done = int(st.get("done", 0))
                            total = len(subs)
                            prompt = st.get("prompt", "") or ""
                            steps = st.get("steps")
                            if not isinstance(steps, list) or len(steps) != total:
                                steps = [{"sub": sub, "agent": None, "model": None,
                                          "status": "done" if i < done else "pending"}
                                         for i, sub in enumerate(subs)]
                            if any(isinstance(step, dict) and step.get("status") == "running"
                                   for step in steps):
                                status = "en curso"
                            elif total > 0 and done >= total:
                                status = "completo"
                            elif done < total:
                                status = "cortado"
                            else:
                                status = "activo"
                        else:
                            steps = []
                    except Exception:
                        steps = []
                    out.append({"branch": branch, "wt_path": wt_path,
                                "prompt": prompt, "done": done, "total": total,
                                "subs": subs, "steps": steps, "status": status})
                wt_path, branch = None, None
        out.sort(key=lambda x: x["branch"], reverse=True)
        return out

    def plan_resume(self, wt_path):
        """Lanza `run --resume <wt_path>` como proceso detached en segundo plano."""
        if ejecutor.lanzar_desacoplado(["run", "--resume", wt_path], cwd=rutas.ROOT):
            return {"ok": True, "msg": "reanudacion disparada en segundo plano"}
        return {"ok": False, "msg": "no se pudo lanzar la reanudacion"}

    def plan_merge(self, branch, wt_path):
        """Acepta el trabajo: mergea la rama del run y quita su worktree."""
        try:
            m = subprocess.run(["git", "-C", str(rutas.ROOT), "merge", branch],
                               capture_output=True, text=True)
            if m.returncode != 0:
                return {"ok": False, "msg": ((m.stderr or m.stdout or "").strip())[:500]}
            rm = subprocess.run(["git", "-C", str(rutas.ROOT), "worktree", "remove", wt_path],
                               capture_output=True, text=True)
            if rm.returncode != 0:
                return {"ok": False, "msg": ((rm.stderr or rm.stdout or "").strip())[:500]}
            return {"ok": True, "msg": ((m.stdout or "merge ok").strip())[:500]}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def plan_discard(self, branch, wt_path):
        """Descarta el run: fuerza la remocion del worktree y borra la rama."""
        try:
            subprocess.run(["git", "-C", str(rutas.ROOT), "worktree", "remove", "--force", wt_path],
                          capture_output=True, text=True)
            subprocess.run(["git", "-C", str(rutas.ROOT), "branch", "-D", branch],
                          capture_output=True, text=True)
        except Exception:
            pass
        return {"ok": True}


def _crear_servidor_ui(puerto: int = 0):
    """Sirve la UI por HTTP con pura stdlib y la abre en el navegador.

    pywebview era la unica via para abrir la interfaz, y es una dependencia por
    pip: contradecia la promesa de cero dependencias y obligaba a un paso extra
    de instalacion. Este modo usa el MISMO BrainUIApi, solo cambia el transporte.

    Se ata a 127.0.0.1 y exige un token que se inyecta en la pagina servida: sin
    eso, cualquier sitio abierto en el navegador podria llamar a la API local, y
    esa API escribe notas y configuracion."""
    import secrets
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    ui = rutas.ASSETS / "ui"
    token = secrets.token_urlsafe(24)
    api = BrainUIApi()
    permitidos = {m for m in dir(api) if not m.startswith("_") and callable(getattr(api, m))}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass                                   # sin ruido en la consola

        def _responder(self, codigo, cuerpo, tipo="application/json"):
            datos = cuerpo if isinstance(cuerpo, bytes) else str(cuerpo).encode("utf-8")
            self.send_response(codigo)
            self.send_header("Content-Type", tipo)
            self.send_header("Content-Length", str(len(datos)))
            self.end_headers()
            self.wfile.write(datos)

        def do_GET(self):
            ruta = self.path.split("?")[0]
            if ruta in ("/", "/index.html"):
                html = (ui / "index.html").read_text(encoding="utf-8")
                inyectado = ("<script>window.__SOOU_TOKEN__=" + json.dumps(token)
                             + ";</script>")
                html = html.replace("<head>", "<head>" + inyectado, 1)
                return self._responder(200, html.encode("utf-8"), "text/html; charset=utf-8")
            archivo = (ui / ruta.lstrip("/")).resolve()
            if not str(archivo).startswith(str(ui.resolve())) or not archivo.is_file():
                return self._responder(404, b"no encontrado", "text/plain")
            tipos = {".png": "image/png", ".svg": "image/svg+xml", ".css": "text/css",
                     ".js": "application/javascript", ".ico": "image/x-icon"}
            return self._responder(200, archivo.read_bytes(),
                                   tipos.get(archivo.suffix.lower(), "application/octet-stream"))

        def do_POST(self):
            # el cuerpo se lee SIEMPRE antes de responder: si se rechaza sin
            # consumirlo, Windows aborta la conexion (WinError 10053) y el cliente
            # recibe un error de red en vez del 403/404.
            try:
                largo = min(int(self.headers.get("Content-Length") or 0), 10 * 1024 * 1024)
            except ValueError:
                largo = 0
            crudo = self.rfile.read(largo) if largo else b""
            if not self.path.startswith("/api/"):
                return self._responder(404, json.dumps({"error": "ruta desconocida"}))
            if not secrets.compare_digest(self.headers.get("X-Soou-Token", ""), token):
                return self._responder(403, json.dumps({"error": "token invalido"}))
            metodo = self.path[len("/api/"):]
            if metodo not in permitidos:
                return self._responder(404, json.dumps({"error": "metodo desconocido"}))
            try:
                cuerpo = json.loads(crudo or b"{}")
                resultado = getattr(api, metodo)(*(cuerpo.get("args") or []))
                return self._responder(200, json.dumps(resultado, ensure_ascii=False,
                                                       default=str))
            except Exception as e:
                return self._responder(500, json.dumps({"error": str(e)[:300]}))

    return ThreadingHTTPServer(("127.0.0.1", puerto), Handler), token


def _servir_ui(puerto: int = 0) -> None:
    """Levanta el servidor local y abre el navegador."""
    consola.mostrar_marca("interfaz local")
    servidor, _ = _crear_servidor_ui(puerto)
    url = f"http://127.0.0.1:{servidor.server_port}/"
    print(f"[ui] sirviendo en {url}  (Ctrl+C para cerrar)")
    try:
        webbrowser.open(url)
    except Exception:
        print("[ui] abri esa direccion a mano en tu navegador.")
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\n[ui] cerrado.")
    finally:
        servidor.server_close()


def cmd_ui(args):
    """Abre la interfaz. Usa pywebview si esta; si no, la sirve en el navegador."""
    ui = rutas.ASSETS / "ui" / "index.html"
    if not ui.exists():
        print(f"[error] no existe {ui}")
        return
    if not getattr(args, "browser", False):
        try:
            import webview
        except ImportError:
            print("[aviso] pywebview no esta instalado: abro la UI en el navegador.")
        else:
            webview.create_window("soou", str(ui), js_api=BrainUIApi(),
                                  width=1280, height=820, min_size=(980, 640),
                                  background_color="#0d0f14")
            webview.start()
            return
    _servir_ui(getattr(args, "port", 0) or 0)
