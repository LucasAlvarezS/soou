"""Paquete de contexto: que notas del cerebro ve un agente, por que y cuales
quedaron fuera. Un solo armado para todos los caminos (el hook de recall,
`run`, el MCP y `soou context --explain`).

Orden de prioridad, dentro de un presupuesto de caracteres:
  1. el resumen del proyecto (su nota `type: project`), si la carpeta de
     trabajo corresponde a un proyecto conocido;
  2. las notas pertinentes a la tarea: semillas del ranking (BM25 + vectorial,
     RRF) y, si la tarea es un tema y no un nombre, vecinos del grafo.

Nunca entran: las notas privadas (`indice.para_agentes`) ni las reemplazadas
(`status: reemplazada` o `superseded_by`). Una nota de OTRO proyecto entra solo
si es la mas pertinente o la tarea nombra ese proyecto, y queda explicado.

Capa core: usa indice y router."""

import re
from dataclasses import dataclass, field
from pathlib import Path

import os

from soou_core import config, indice, router, vigencia

PRESUPUESTO = 1600          # caracteres del bloque entero: ~5 notas, lo que mandaba run antes
MAX_PIEZAS = 7
TRANSVERSAL = 3            # una nota que enlaza a 3+ proyectos es general
CAMPOS_PROCEDENCIA = ("sources", "origin", "source_session", "source_run", "recorded_at",
                      "status", "verification", "supersedes", "updated")


@dataclass
class Pieza:
    nota: dict
    razon: str
    puntaje: float | None = None
    proyecto: str | None = None
    texto: str = ""

    @property
    def nombre(self) -> str:
        return self.nota["name"]

    @property
    def coste(self) -> int:
        """Caracteres que ocupa en el prompt: la linea entera, no solo el texto."""
        return len(self.texto) + len(self.nombre) + 9      # "- [[x]] — " + salto


@dataclass
class Paquete:
    tarea: str
    modo: str = ""
    backend: str = ""
    proyecto: str | None = None
    presupuesto: int = PRESUPUESTO
    piezas: list = field(default_factory=list)
    excluidas: list = field(default_factory=list)     # (nombre, motivo)
    ranking: list = field(default_factory=list)       # [(rrf, nota)] sin privadas

    @property
    def usados(self) -> int:
        return sum(p.coste for p in self.piezas)

    @property
    def notas(self) -> list:
        return [p.nota for p in self.piezas]


def presupuesto() -> int:
    """`context_budget` de soou.config.json, o el valor por defecto."""
    try:
        v = int(config.load_config().get("context_budget") or PRESUPUESTO)
    except (TypeError, ValueError):
        v = PRESUPUESTO
    return max(200, v)


def _clave(texto: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(texto).lower())


def reemplazada(n: dict, mapa: dict | None = None) -> str | None:
    """Motivo si la nota fue reemplazada (no se borra: sale del contexto). Con
    `mapa` (vigencia.reemplazos) cuenta tambien el `supersedes` de la nota nueva."""
    return vigencia.motivo(n["name"], mapa if mapa is not None else vigencia.reemplazos([n]))


def proyectos(notas: list) -> dict:
    """{clave de carpeta: [notas de proyecto]}. Una nota `type: project` apunta
    a la carpeta de su repo en `sources` (p.ej. [Api]); una carpeta
    puede tener mas de una (p.ej. la del producto y la del repo)."""
    out = {}
    for n in notas:
        if n["meta"].get("type") != "project":
            continue
        fuentes = n["meta"].get("sources") or []
        fuentes = fuentes if isinstance(fuentes, list) else [fuentes]
        for f in fuentes:
            partes = [p for p in re.split(r"[\\/]", str(f)) if p]
            lista = out.setdefault(_clave(partes[0]), []) if partes else None
            if lista is not None and n not in lista:
                lista.append(n)
    for lista in out.values():     # la nota "proj-*" es la principal
        lista.sort(key=lambda n: (not n["name"].startswith("proj-"), n["name"]))
    return out


def proyecto_de(cwd, notas: list) -> list:
    """Notas de proyecto de una carpeta (la carpeta o alguna de sus madres);
    la primera es la principal. Lista vacia si no hay proyecto conocido."""
    if not cwd:
        return []
    por_clave = proyectos(notas)
    try:
        p = Path(cwd).resolve()
    except OSError:
        return []
    for parte in [p, *p.parents]:
        hit = por_clave.get(_clave(parte.name))
        if hit:
            return hit
    return []


def _pertenencia(notas: list) -> dict:
    """{nota: {proyectos}}: la propia nota de proyecto, o las notas de proyecto a
    las que ESA nota enlaza. Que una nota de proyecto enlace a una general (la
    empresa, la persona) no la vuelve de ese proyecto."""
    nombres_proy = {n["name"] for n in notas if n["meta"].get("type") == "project"}
    out = {}
    for n in notas:
        nm = n["name"]
        if nm in nombres_proy:
            out[nm] = {nm}
            continue
        suyos = set(indice.note_links(n)) & nombres_proy
        # la persona y las notas que cruzan 3+ proyectos son generales
        if n["meta"].get("type") == "persona" or len(suyos) >= TRANSVERSAL:
            suyos = set()
        out[nm] = suyos
    return out


def _fragmento(n: dict, recorte: int) -> str:
    # la etiqueta va DENTRO del fragmento: cuenta para el presupuesto
    marca = vigencia.etiqueta(n)
    return marca + " ".join(n["body"].split())[:max(0, recorte - len(marca))]


def armar_paquete(tarea: str, notas: list | None = None, *, cwd=None,
                  presupuesto: int = PRESUPUESTO, recorte: int = 280,
                  max_piezas: int = MAX_PIEZAS, vecinos: bool = True,
                  resumen_proyecto: bool = True) -> Paquete:
    """Arma el paquete. `notas` = todas las notas (las privadas se excluyen
    aqui, y quedan registradas como excluidas sin mostrar su contenido)."""
    todas = notas if notas is not None else indice.load_notes()
    visibles = indice.para_agentes(todas)
    ids_visibles = {id(n) for n in visibles}
    paq = Paquete(tarea=tarea, presupuesto=presupuesto)
    adj = indice.build_adjacency(visibles)
    pertenece = _pertenencia(visibles)
    proys = proyecto_de(cwd, visibles)
    paq.proyecto = proys[0]["name"] if proys else None
    propios = {n["name"] for n in proys}
    tarea_clave = _clave(tarea)
    mapa_reemplazos = vigencia.reemplazos(visibles)

    vistos, pasaron = set(), set()

    def considerar(n, razon, s):
        """Aplica los filtros y el presupuesto; True si paso los filtros."""
        nm = n["name"]
        if nm in vistos:
            return nm in pasaron
        vistos.add(nm)
        motivo = reemplazada(n, mapa_reemplazos)
        if motivo:
            paq.excluidas.append((nm, motivo))
            return False
        suyos = pertenece.get(nm, set())
        ajeno = None
        if suyos and propios and not (suyos & propios):
            ajeno = sorted(suyos)[0]
            nombrado = any(_clave(p.replace("proj-", "")) in tarea_clave for p in suyos)
            if razon != "semilla #1 del ranking" and not nombrado:
                paq.excluidas.append((nm, f"de otro proyecto ({ajeno})"))
                return False
            razon += f" · de otro proyecto ({ajeno}), incluida por relevancia"
        pasaron.add(nm)
        if len(paq.piezas) >= max_piezas:
            paq.excluidas.append((nm, f"tope de {max_piezas} notas"))
            return True
        texto = _fragmento(n, recorte)
        if paq.usados + len(texto) + len(nm) + 9 > presupuesto:
            paq.excluidas.append((nm, f"presupuesto ({presupuesto} caracteres)"))
            return True
        paq.piezas.append(Pieza(nota=n, razon=razon, puntaje=s, texto=texto,
                                proyecto=ajeno or (paq.proyecto if suyos & propios else None)))
        return True

    if proys and resumen_proyecto:
        considerar(proys[0], "resumen del proyecto", None)

    terms = re.findall(r"[a-záéíóúñ0-9\-]{4,}", tarea.lower())
    fused, paq.backend = indice.rank_notes(tarea, todas) if terms else ([], "")
    # las privadas no entran nunca: se registran y se saltan
    ranking = []
    for s, n in fused:
        if id(n) not in ids_visibles:
            if len(paq.excluidas) < 20:
                paq.excluidas.append((n["name"], "privada"))
            continue
        ranking.append((s, n))
    paq.ranking = ranking
    entidad = bool(terms) and router.looks_like_entity(tarea, terms)
    paq.modo = "nota exacta" if entidad else "tema · semillas + vecinos del grafo"
    semillas = ranking[:2] if entidad else ranking[:3]
    # solo las semillas que pasan los filtros abren vecinos: una excluida (de
    # otro proyecto, reemplazada) no trae notas por la puerta de atras
    validas = [n for i, (s, n) in enumerate(semillas, 1)
               if considerar(n, f"semilla #{i} del ranking", s)]
    if vecinos and not entidad:
        por_nombre = {n["name"]: n for n in visibles}
        puntaje = {n["name"]: s for s, n in ranking}
        cands, ya = [], set(vistos)
        for n in validas:
            for nb in sorted(adj.get(n["name"], ())):
                if nb not in ya and nb in por_nombre:
                    cands.append((nb, n["name"]))
                    ya.add(nb)
        # conceptos primero; dentro de cada grupo, el mejor puntaje del ranking
        cands.sort(key=lambda c: (0 if por_nombre[c[0]]["meta"].get("type") in indice.CONCEPT_TYPES
                                  else 1, -puntaje.get(c[0], 0.0)))
        for nb, de in cands:
            considerar(por_nombre[nb], f"vecino de [[{de}]]", puntaje.get(nb))
    return paq


# ------------------------------------------------------------------ formatos

def como_prompt(paq: Paquete) -> str:
    """Bloque para el prompt de un agente (run, plan)."""
    return "\n".join(f"- [[{p.nombre}]] — {p.texto}" for p in paq.piezas)


def como_hook(paq: Paquete) -> list:
    """Lineas del hook UserPromptSubmit (fragmentos cortos, sin markdown)."""
    lineas = [f"🧠 Del cerebro ({paq.modo} · {paq.backend}+bm25/rrf):"]
    for p in paq.piezas:
        frag = re.sub(r"\s+", " ", re.sub(r"[#>*`\[\]()-]", " ", p.texto)).strip()[:130]
        lineas.append(f"  · [[{p.nombre}]] — {frag}")
    return lineas


def _procedencia(n: dict) -> str:
    m = n.get("meta") or {}
    partes = []
    for k in CAMPOS_PROCEDENCIA:
        v = m.get(k)
        if v in (None, "", [], "[]"):
            continue
        if isinstance(v, list):
            v = ", ".join(map(str, v[:3])) + ("…" if len(v) > 3 else "")
        partes.append(f"{k}: {v}")
    return " · ".join(partes) or "sin procedencia registrada"


def cmd_context(args):
    """`soou context "tarea"`: lo que veria un agente para esa tarea; con
    --explain, por que entro cada nota y por que quedaron fuera las demas."""
    tarea = " ".join(getattr(args, "tarea", []) or []).strip()
    if not tarea:
        print("[no] falta la tarea: soou context \"que quieres hacer\" [--explain]")
        return
    paq = armar_paquete(tarea, cwd=getattr(args, "cwd", None) or os.getcwd(),
                        presupuesto=getattr(args, "presupuesto", None) or presupuesto())
    if getattr(args, "explain", False):
        print(explicar(paq))
    else:
        print(como_prompt(paq) or "(ninguna nota del cerebro para esta tarea)")


def explicar(paq: Paquete) -> str:
    """Que veria el agente y por que (soou context --explain)."""
    out = [f"Tarea: {paq.tarea}",
           f"Proyecto: {paq.proyecto or 'ninguno reconocido (sin aislamiento por proyecto)'}",
           f"Modo: {paq.modo or '-'} · ranking {paq.backend or '-'}",
           f"Presupuesto: {paq.usados} de {paq.presupuesto} caracteres en {len(paq.piezas)} nota(s)",
           "", "Incluidas:"]
    if not paq.piezas:
        out.append("  (ninguna)")
    for i, p in enumerate(paq.piezas, 1):
        punt = f" · rrf {p.puntaje:.4f}" if p.puntaje is not None else ""
        out.append(f"  {i}. [[{p.nombre}]] ({p.nota['meta'].get('type', '?')}) · "
                   f"{p.razon}{punt} · {p.coste} caracteres")
        out.append(f"     procedencia: {_procedencia(p.nota)}")
    out += ["", "Excluidas:"]
    tope = [nm for nm, motivo in paq.excluidas if motivo.startswith("tope")]
    resto = [f"  - [[{nm}]]: {motivo}" for nm, motivo in paq.excluidas
             if not motivo.startswith("tope")]
    if tope:
        resto.append(f"  - {len(tope)} nota(s) mas por el tope de piezas: "
                     + ", ".join(tope[:5]) + ("…" if len(tope) > 5 else ""))
    out += resto or ["  (ninguna)"]
    return "\n".join(out)
