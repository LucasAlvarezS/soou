"""Texto y retrieval lexico: plegado de tildes, stemmer es/en, tokenizador,
BM25, TF-IDF, coseno, RRF y TextRank. Funciones puras, sin estado global.

Extraido de soou.py (que lo re-exporta: `soou.<nombre>` sigue valiendo)."""

import math
import re
import unicodedata


STOPWORDS = set((
    "de la el los las un una unos unas y o u a en que se con por para del al lo su sus es "
    "son ser esta este estos estas como mas más pero si no ni le les mi tu demas cada "
    "the of to and in that is are for with this from was were will can not you your it its "
    "on as be at or an by").split())


# Version del tokenizador: si cambia, el indice (soou.db) se reconstruye solo,
# porque guarda los tokens ya calculados y mezclar versiones rompe el ranking.
TOKENIZER_VERSION = 2


def _plegar(texto: str) -> str:
    """minusculas y sin tildes: 'Inspección' == 'inspeccion' (la ñ se conserva)."""
    import unicodedata
    t = texto.lower().replace("ñ", "\x00")
    t = "".join(c for c in unicodedata.normalize("NFKD", t) if not unicodedata.combining(c))
    return t.replace("\x00", "ñ")


def _raiz(t: str) -> str:
    """Stemmer liviano es/en, conservador: plurales y vocal final de genero.
    'aerogeneradores'->'aerogenerador', 'drones'->'dron', 'fotovoltaicos' y
    'fotovoltaica'->'fotovoltaic', 'turbines'->'turbin'. No toca numeros, ids
    con guion ni palabras cortas (<5)."""
    if len(t) < 5 or not t.isalpha():
        return t
    # (sin "-ces -> -z": "luces"->"luz" acierta pero "dulces"->"dulz" no)
    if t.endswith("iones") and len(t) >= 8:
        return t[:-5] + "ion"
    if t.endswith("es") and len(t) > 5 and t[-3] not in "aeiou":
        t = t[:-2]
    elif t.endswith("s") and not t.endswith("ss"):
        t = t[:-1]
    if len(t) >= 6 and t[-1] in "aoe":
        t = t[:-1]
    return t


_STOP_PLEGADAS = None


def tokenize(text: str) -> list[str]:
    global _STOP_PLEGADAS
    if _STOP_PLEGADAS is None:
        _STOP_PLEGADAS = {_plegar(w) for w in STOPWORDS}
    toks = re.findall(r"[a-zñ0-9\-]{3,}", _plegar(text))
    return [_raiz(t) for t in toks if t not in _STOP_PLEGADAS]


def _superficies(texto: str) -> dict:
    """raiz -> forma mas frecuente en el texto, para MOSTRAR etiquetas legibles."""
    cuenta = {}
    for t in re.findall(r"[a-záéíóúüñ0-9\-]{3,}", texto.lower()):
        r = _raiz(_plegar(t))
        cuenta.setdefault(r, {}).setdefault(t, 0)
        cuenta[r][t] += 1
    return {r: max(fs, key=fs.get) for r, fs in cuenta.items()}


def _df(docs: list) -> dict:
    df = {}
    for d in docs:
        for t in set(d):
            df[t] = df.get(t, 0) + 1
    return df


def bm25_scores(qterms, docs, k1=1.5, b=0.75, df=None) -> list:
    """`df` opcional: el llamador que tambien pide tfidf_cosine lo calcula una vez."""
    N = len(docs)
    if not N:
        return []
    df = _df(docs) if df is None else df
    avgdl = sum(len(d) for d in docs) / N
    out = []
    for d in docs:
        if not d:
            out.append(0.0); continue
        tf = {}
        for t in d:
            tf[t] = tf.get(t, 0) + 1
        dl = len(d)
        s = 0.0
        for q in qterms:
            f = tf.get(q, 0)
            if f:
                idf = math.log(1 + (N - df[q] + 0.5) / (df[q] + 0.5))
                s += idf * (f * (k1 + 1)) / (f + k1 * (1 - b + b * dl / avgdl))
        out.append(s)
    return out


def _tfidf_vec(tokens, df, N):
    tf = {}
    for t in tokens:
        tf[t] = tf.get(t, 0) + 1
    v = {t: (1 + math.log(f)) * math.log(N / df[t])
         for t, f in tf.items() if t in df and df[t]}
    norm = math.sqrt(sum(w * w for w in v.values())) or 1.0
    return {t: w / norm for t, w in v.items()}


def tfidf_cosine(qterms, docs, df=None) -> list:
    N = len(docs)
    if not N:
        return []
    df = _df(docs) if df is None else df
    qv = _tfidf_vec(qterms, df, N)
    if not qv:                  # ningun termino de la consulta esta en el corpus
        return [0.0] * N
    out = []
    for d in docs:
        dv = _tfidf_vec(d, df, N)
        out.append(sum(w * dv.get(t, 0) for t, w in qv.items()))
    return out


def _cosine(a, b):
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def _ranks(scores) -> dict:
    order = sorted(range(len(scores)), key=lambda i: -scores[i])
    return {i: rank for rank, i in enumerate(order) if scores[i] > 0}


def _sentences(text: str) -> list:
    out = []
    for s in re.split(r"(?<=[.!?])\s+|\n+", text):
        s = s.strip(" \t-*·>#`")
        if 30 <= len(s) <= 320 and " " in s and not s.startswith("```"):
            out.append(s)
    return out[:120]                       # cota: TextRank es O(n^2)


def _textrank(sentences: list, top_k: int = 5) -> list:
    """Resumen extractivo: grafo de frases (coseno TF-IDF) + PageRank."""
    n = len(sentences)
    if n <= top_k:
        return sentences
    toks = [tokenize(s) for s in sentences]
    df = _df(toks)
    vecs = [_tfidf_vec(t, df, n) for t in toks]
    sim = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            v = sum(w * vecs[j].get(t, 0) for t, w in vecs[i].items())
            sim[i][j] = sim[j][i] = v
    deg = [sum(row) or 1.0 for row in sim]
    score, d = [1.0 / n] * n, 0.85
    for _ in range(30):                    # power iteration
        nxt = [(1 - d) / n] * n
        for i in range(n):
            acc = 0.0
            for j in range(n):
                if sim[i][j]:
                    acc += sim[i][j] / deg[j] * score[j]
            nxt[i] += d * acc
        score = nxt
    top = sorted(sorted(range(n), key=lambda i: score[i], reverse=True)[:top_k])
    return [sentences[i] for i in top]


def _keyphrases(session_tokens: list, corpus_docs: list, top_n: int = 8) -> list:
    docs = corpus_docs + [session_tokens]
    df = _df(docs)
    v = _tfidf_vec(session_tokens, df, len(docs))
    return [t for t in sorted(v, key=v.get, reverse=True)
            if len(t) >= 4 and not t.isdigit()][:top_n]
