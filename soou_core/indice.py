"""Modulo indice de soou (extraido de soou.py).

soou.py lo re-exporta; dentro de soou_core se usa como `indice.nombre`."""

from pathlib import Path
import datetime as dt
import json
import re
import sqlite3
from soou_core import rutas, texto, vigencia


AREAS = ("code", "personal", "work")


PLACEHOLDER_TAG = "TODO:INGEST"


LINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


CODE_SPAN_RE = re.compile(r"`[^`]*`")   # código inline: sus [[...]] son ilustrativos


def slugify(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    text = re.sub(r"[\s_]+", "-", text)
    return text.strip("-") or "sin-nombre"


def parse_note(path: Path) -> dict:
    """Devuelve {meta: dict, body: str, name: str, area: str, path: Path}."""
    raw = path.read_text(encoding="utf-8")
    meta, body = {}, raw
    if raw.startswith("---"):
        end = raw.find("\n---", 3)
        if end != -1:
            fm = raw[3:end].strip()
            body = raw[end + 4 :].lstrip("\n")
            for line in fm.splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip()
    name = meta.get("name") or path.stem
    area = meta.get("area") or (path.parent.name if path.parent.name in AREAS else "?")
    return {"meta": meta, "body": body, "name": name, "area": area, "path": path}


def links_in(body: str) -> list[str]:
    # soporta alias estilo Obsidian: [[nombre|texto visible]] -> nombre.
    # ignora enlaces dentro de codigo inline (`[[x]]`): son ilustrativos, no aristas.
    # DEDUPLICA conservando el orden: un [[x]] repetido es UNA arista, no N. Sin esto
    # compute_graph contaba aristas dobles y el grado de entrada quedaba inflado.
    body = CODE_SPAN_RE.sub(" ", body)
    vistos, out = set(), []
    for m in LINK_RE.findall(body):
        nombre = m.split("|")[0].strip()
        if nombre and nombre not in vistos:
            vistos.add(nombre)
            out.append(nombre)
    return out


def _index_entry(path: Path, st) -> dict:
    note = parse_note(path)
    body = note["body"]
    return {
        "mtime": int(st.st_mtime),
        "size": st.st_size,
        "meta": note["meta"],
        "name": note["name"],
        "area": note["area"],
        "body": body,
        "tokens": texto.tokenize(note["name"].replace("-", " ") + " " + body),
        "links": links_in(body),
        "placeholder": PLACEHOLDER_TAG in body,
    }


def _db():
    """Conexión a soou.db (SQLite embebido, stdlib). WAL para concurrencia
    entre agentes/hooks. El esquema es reconstruible desde los .md."""
    Path(rutas.DB_PATH).parent.mkdir(parents=True, exist_ok=True)   # instalacion nueva: ~/.soou
    con = sqlite3.connect(str(rutas.DB_PATH), timeout=5.0)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")
    con.execute("""CREATE TABLE IF NOT EXISTS notes(
        rel TEXT PRIMARY KEY, mtime INTEGER, size INTEGER,
        name TEXT, area TEXT, body TEXT, meta TEXT,
        tokens TEXT, links TEXT, placeholder INTEGER)""")
    return con


def build_index() -> list[dict]:
    """Carga las notas vía índice SQLite (soou.db); sólo re-tokeniza las que
    cambiaron (por mtime/size). Mismo algoritmo de ranking que antes: la data es
    idéntica, sólo cambia el almacén del índice (JSON → SQLite transaccional)."""
    con = _db()
    if con.execute("PRAGMA user_version").fetchone()[0] != texto.TOKENIZER_VERSION:
        with con:                      # tokenizador nuevo: tokens cacheados invalidos
            con.execute("DELETE FROM notes")
            con.execute(f"PRAGMA user_version = {int(texto.TOKENIZER_VERSION)}")
    cached = {row[0]: (row[1], row[2])
              for row in con.execute("SELECT rel, mtime, size FROM notes")}
    seen, upserts = set(), []
    for p in sorted(rutas.WIKI.rglob("*.md")):
        if p.name.upper() == "INDEX.MD":
            continue
        rel = p.relative_to(rutas.WIKI).as_posix()
        seen.add(rel)
        try:
            st = p.stat()
        except OSError:
            continue
        row = cached.get(rel)
        if not (row and row[0] == int(st.st_mtime) and row[1] == st.st_size):
            upserts.append((rel, _index_entry(p, st)))   # nueva/modificada
    stale = set(cached) - seen                             # borradas del disco
    with con:                                              # transacción atómica
        if stale:
            con.executemany("DELETE FROM notes WHERE rel=?", [(r,) for r in stale])
        for rel, e in upserts:
            con.execute(
                "INSERT OR REPLACE INTO notes"
                "(rel,mtime,size,name,area,body,meta,tokens,links,placeholder)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (rel, e["mtime"], e["size"], e["name"], e["area"], e["body"],
                 json.dumps(e["meta"], ensure_ascii=False),
                 json.dumps(e["tokens"], ensure_ascii=False),
                 json.dumps(e["links"], ensure_ascii=False),
                 1 if e["placeholder"] else 0))
    notes = []
    for rel, name, area, body, meta, tokens, links, ph in con.execute(
            "SELECT rel,name,area,body,meta,tokens,links,placeholder FROM notes"):
        notes.append({
            "meta": json.loads(meta), "body": body, "name": name, "area": area,
            "path": rutas.WIKI / rel, "_tokens": json.loads(tokens),
            "_links": json.loads(links), "_placeholder": bool(ph)})
    con.close()
    notes.sort(key=lambda n: n["path"].as_posix())
    return notes


ARCHIVOS_DE_REGLAS = (
    ("schema.yaml", "regla-schema",
     "Reglas de mantenimiento del cerebro: areas, frontmatter obligatorio, "
     "sintesis, lint y politica ante contradicciones."),
    ("agentes-cerebro.md", "regla-agentes",
     "Instrucciones para agentes: consultar el cerebro antes de responder, "
     "cuando orquestar y cuando aportar conocimiento."),
)


def _notas_de_reglas() -> list:
    """Las reglas del proyecto como notas recuperables (solo lectura)."""
    fuera = []
    for archivo, nombre, resumen in ARCHIVOS_DE_REGLAS:
        ruta = rutas.ROOT / archivo
        if not ruta.exists() and rutas.ASSETS != rutas.CODE_DIR:   # instalado: reglas del paquete
            ruta = rutas.ASSETS / archivo
        try:
            cuerpo = ruta.read_text(encoding="utf-8")
        except OSError:
            continue
        fuera.append({
            "name": nombre,
            "area": "code",
            "body": f"# {nombre}\n\n{resumen}\n\nFuente: {archivo}\n\n{cuerpo}",
            "meta": {"name": nombre, "area": "code", "type": "rule",
                     "sources": [archivo], "pinned": "true",
                     "updated": today()},
            "path": ruta,
            "_tokens": None,
            # sin aristas a proposito: los [[x]] de schema.yaml son ilustrativos y
            # ensuciarian el lint con enlaces rotos.
            "_links": [],
            "_placeholder": False,
        })
    return fuera


def load_notes() -> list[dict]:
    # las reglas se suman al vuelo: no viven en wiki/ pero tienen que ser
    # recuperables como cualquier otra nota.
    return build_index() + _notas_de_reglas()


def note_links(n: dict) -> list[str]:
    """Enlaces de la nota, del índice si están; si no, se derivan del body."""
    l = n.get("_links")
    return l if l is not None else links_in(n["body"])


def note_placeholder(n: dict) -> bool:
    p = n.get("_placeholder")
    return p if p is not None else (PLACEHOLDER_TAG in n["body"])


def cmd_reindex(args):
    """Reconstruye el índice soou.db desde los .md (fuente de verdad). Sin Docker.
    Reemplaza al viejo 'sync' a Neo4j en los flujos automáticos."""
    if getattr(args, "full", False):
        for suf in ("", "-wal", "-shm"):
            f = Path(str(rutas.DB_PATH) + suf)
            try:
                if f.exists():
                    f.unlink()
            except OSError:
                pass
    notes = build_index()
    print(f"[ok] soou.db al día: {len(notes)} notas indexadas.")


def today() -> str:
    return dt.date.today().isoformat()


CONCEPT_TYPES = {"concept", "reference", "decision", "person", "event"}


def source_files(meta: dict) -> list[Path]:
    """Rutas de archivo reales referenciadas en 'sources' (ignora URLs)."""
    raw = meta.get("sources", "").strip().strip("[]")
    paths = []
    for s in raw.split(","):
        s = s.strip()
        if not s or s.startswith("http"):
            continue
        p = (rutas.ROOT.parent / s)
        paths.append(p)
    return paths


def is_stale(note: dict) -> bool:
    """True si alguna fuente de la nota cambio despues de registrarla."""
    stored = note["meta"].get("src_mtime")
    if not stored or not str(stored).strip().isdigit():
        return False
    stored = int(stored)
    for p in source_files(note["meta"]):
        try:
            if p.exists() and int(p.stat().st_mtime) > stored:
                return True
        except Exception:
            pass
    return False


TIPOS_DE_NOTA = ("concept", "decision", "person", "source", "reference", "event")


def resumen_salud() -> dict:
    """Estado del cerebro en una pasada: enlaces rotos, huerfanas, placeholders,
    obsoletas, contradicciones registradas y posibles duplicados."""
    notas = load_notes()
    nombres = {n["name"] for n in notas}
    rotos, placeholders, referenciadas = [], [], set()
    for n in notas:
        for l in note_links(n):
            referenciadas.add(l)
            if l not in nombres:
                rotos.append({"from": n["name"], "to": l})
        if note_placeholder(n):
            placeholders.append(n["name"])
    huerfanas = [n["name"] for n in notas if n["name"] not in referenciadas
                 and n["meta"].get("type") not in ("doc", "session", "rule")]
    obsoletas = [n["name"] for n in notas if is_stale(n)]
    # las notas de regla DESCRIBEN la politica de contradicciones; no son una.
    contradicciones = [n["name"] for n in notas
                       if n["meta"].get("type") != "rule"
                       and ("registrar-no-inferir" in n["body"].lower()
                            or ("contradicci" in n["body"].lower()
                                and "⚠" in n["body"]))]
    vistos, duplicados = {}, []
    for n in notas:
        clave = re.sub(r"[-_\s]", "", n["name"].lower())
        if clave in vistos:
            duplicados.append([vistos[clave], n["name"]])
        else:
            vistos[clave] = n["name"]
    return {"total": len(notas), "broken": rotos, "orphans": huerfanas,
            "stale": obsoletas, "placeholders": placeholders,
            "contradictions": contradicciones, "duplicates": duplicados,
            "vigencia": vigencia.revisar(notas)}


def es_privada(n: dict) -> bool:
    """Una nota privada no sale hacia ningun agente: ni recall, ni MCP, ni el
    contexto de `run`, ni la persona. Se marca con `private: true` en el
    frontmatter, o viviendo bajo una carpeta de `private_paths` (relativa a wiki/).
    Sigue siendo tuya: se ve en la interfaz y en el grafo locales."""
    if str(n.get("meta", {}).get("private", "")).lower() == "true":
        return True
    from soou_core import config            # import diferido: config no depende de indice
    carpetas = config.load_config().get("private_paths") or []
    ruta = n.get("path")
    if not carpetas or ruta is None:
        return False
    try:
        rel = Path(ruta).resolve().relative_to(rutas.WIKI.resolve()).as_posix()
    except (ValueError, OSError):
        return False
    return any(rel == c.strip("/") or rel.startswith(c.strip("/") + "/") for c in carpetas)


def para_agentes(notes: list) -> list:
    """Las notas que pueden enviarse a un agente (sin las privadas)."""
    return [n for n in notes if not es_privada(n)]


def buscar_nota(nombre: str):
    """La nota con ese nombre exacto, o None."""
    objetivo = slugify(nombre or "")
    return next((n for n in load_notes() if n["name"] == objetivo), None)


def guardar_nota(nombre, cuerpo, area="personal", tipo="concept", pinned=None,
                 procedencia: dict | None = None):
    """Crea o actualiza una nota del cerebro. Devuelve (ok, mensaje).

    Respeta `pinned: true`: una correccion humana no se sobreescribe nunca, tal
    como manda schema.yaml; ante un conflicto corresponde una nota-contradiccion
    aparte. Nunca borra: actualizar reemplaza el cuerpo y conserva el resto del
    frontmatter. `procedencia` agrega campos de vigencia.py (recorded_at,
    verification, ...); los vacios se ignoran."""
    crudo = (nombre or "").strip()
    if not crudo:
        return False, "nombre vacio."
    nombre = slugify(crudo)
    # slugify cae a "sin-nombre" ante entrada vacia o solo simbolos: sin este
    # chequeo un nombre invalido creaba en silencio una nota basura.
    if nombre == "sin-nombre":
        return False, "nombre invalido: '" + crudo + "' no produce un slug util."
    cuerpo = (cuerpo or "").strip()
    if not cuerpo:
        return False, "cuerpo vacio."
    if area not in AREAS:
        return False, "area invalida: " + str(area) + ". Validas: " + ", ".join(AREAS) + "."
    if tipo not in TIPOS_DE_NOTA:
        return False, ("tipo invalido: " + str(tipo) + ". Validos: "
                       + ", ".join(TIPOS_DE_NOTA) + ".")

    previa = buscar_nota(nombre)
    if previa is not None:
        if str(previa["meta"].get("pinned", "")).lower() == "true":
            return False, ("'" + nombre + "' esta pinned (correccion humana o regla del "
                           "sistema): no se sobreescribe. Si la contradice algo nuevo, "
                           "crea una nota aparte y enlazalas.")
        meta = dict(previa["meta"])
        meta["updated"] = today()
        if pinned is not None:
            meta["pinned"] = "true" if pinned else "false"
        destino, accion = previa["path"], "actualizada"
    else:
        meta = {"name": nombre, "area": area, "type": tipo, "sources": "[]",
                "pinned": "true" if pinned else "false", "updated": today()}
        destino = rutas.WIKI / area / (nombre + ".md")
        destino.parent.mkdir(parents=True, exist_ok=True)
        accion = "creada"
    for k, v in (procedencia or {}).items():
        if v in (None, ""):
            continue
        if k == "recorded_at":
            meta.setdefault(k, v)          # cuando se registro, no cuando se edito
        else:
            meta[k] = v

    orden = ["name", "area", "type", "sources", "pinned", "updated"]
    claves = orden + [k for k in meta if k not in orden]
    frontmatter = "\n".join(k + ": " + str(meta[k]) for k in claves if k in meta)
    destino.write_text("---\n" + frontmatter + "\n---\n\n" + cuerpo + "\n", encoding="utf-8")
    build_index()
    return True, "Nota " + accion + ": " + destino.relative_to(rutas.ROOT).as_posix() + "."


def compute_graph():
    notes = load_notes()
    by_name = {n["name"]: n for n in notes}
    nodes, edges = [], []
    indeg = {n["name"]: 0 for n in notes}
    for n in notes:
        for l in note_links(n):
            if l in by_name:
                edges.append({"source": n["name"], "target": l})
                indeg[l] += 1
    for n in notes:
        nodes.append({
            "id": n["name"],
            "area": n["area"],
            "indeg": indeg[n["name"]],
            "placeholder": note_placeholder(n),
            "size": len(n["body"]),
        })
    return notes, nodes, edges


def build_adjacency(notes: list) -> dict:
    names = {n["name"] for n in notes}
    adj = {}
    for n in notes:
        for l in note_links(n):
            if l in names:
                adj.setdefault(n["name"], set()).add(l)
                adj.setdefault(l, set()).add(n["name"])
    return adj


def build_corpus(notes: list) -> list[list[str]]:
    # reutiliza los tokens del índice (F2); tokeniza sólo si no vienen cacheados
    return [n["_tokens"] if n.get("_tokens") is not None
            else texto.tokenize(n["name"].replace("-", " ") + " " + n["body"])
            for n in notes]


def _env(key: str):
    import os
    if os.environ.get(key):
        return os.environ[key]
    envf = rutas.ROOT / ".env"
    if envf.exists():
        for line in envf.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith(key + "="):
                return line.split("=", 1)[1].strip()
    return None


def embed_backend() -> str | None:
    """Desactivado: no usamos proveedores de embeddings externos (OpenAI/Ollama descartados).
    El stream vectorial es siempre TF-IDF cosine (offline, sin dependencias)."""
    return None


def _embed_texts(texts: list) -> list | None:
    """Embeddings reales via OpenAI u Ollama, con cache en .embeddings.json.
    Devuelve lista de vectores, o None si falla / no hay backend."""
    backend = embed_backend()
    if not backend:
        return None
    import hashlib, urllib.request
    cache_f = rutas.ROOT / ".embeddings.json"
    cache = {}
    if cache_f.exists():
        try:
            cache = json.loads(cache_f.read_text(encoding="utf-8"))
        except Exception:
            cache = {}

    def key(t):
        return backend + ":" + hashlib.sha1(t.encode("utf-8")).hexdigest()

    missing = [t for t in texts if key(t) not in cache]
    try:
        for t in missing:
            if backend == "openai":
                req = urllib.request.Request(
                    "https://api.openai.com/v1/embeddings",
                    data=json.dumps({"model": "text-embedding-3-small", "input": t[:8000]}).encode(),
                    headers={"Authorization": f"Bearer {_env('OPENAI_API_KEY')}",
                             "Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    cache[key(t)] = json.loads(r.read())["data"][0]["embedding"]
            else:  # ollama
                url = _env("EMBED_OLLAMA_URL").rstrip("/") + "/api/embeddings"
                model = _env("EMBED_OLLAMA_MODEL") or "nomic-embed-text"
                req = urllib.request.Request(
                    url, data=json.dumps({"model": model, "prompt": t[:8000]}).encode(),
                    headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    cache[key(t)] = json.loads(r.read())["embedding"]
        if missing:
            cache_f.write_text(json.dumps(cache), encoding="utf-8")
    except Exception:
        return None
    return [cache.get(key(t)) for t in texts]


def vector_scores(query, notes, docs, df=None) -> tuple:
    """Stream vectorial: embeddings semanticos si hay backend, si no TF-IDF cosine.
    Devuelve (scores, etiqueta)."""
    vecs = _embed_texts([query] + [n["name"] + " " + n["body"][:2000] for n in notes])
    if vecs and all(v is not None for v in vecs):
        qv = vecs[0]
        return [texto._cosine(qv, dv) for dv in vecs[1:]], f"embeddings:{embed_backend()}"
    return texto.tfidf_cosine(texto.tokenize(query), docs, df=df), "tfidf"


def rank_notes(query: str, notes: list, k: int = 60) -> tuple:
    """Ranking hibrido: BM25 + vectorial, fusionados con RRF. Boost a conceptos.
    Devuelve (lista [(score, note)] desc, etiqueta_backend_vectorial)."""
    docs = build_corpus(notes)
    qt = texto.tokenize(query)
    if not qt:
        return [], "tfidf"
    df = texto._df(docs)                 # una sola pasada: lo usan los dos streams
    bm = texto.bm25_scores(qt, docs, df=df)
    vec, label = vector_scores(query, notes, docs, df=df)
    rb, rv = texto._ranks(bm), texto._ranks(vec)
    fused = []
    for i, n in enumerate(notes):
        f = 0.0
        if i in rb:
            f += 1 / (k + rb[i])
        if i in rv:
            f += 1 / (k + rv[i])
        if f > 0:
            if n["meta"].get("type") in CONCEPT_TYPES:
                f *= 1.15
            fused.append((f, n))
    fused.sort(key=lambda x: -x[0])
    return fused, label
