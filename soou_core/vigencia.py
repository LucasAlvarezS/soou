"""Procedencia y vigencia de las notas: campos opcionales del frontmatter.

| campo                         | significado                                   |
|-------------------------------|-----------------------------------------------|
| source_session / source_run   | de donde salio                                |
| recorded_at                   | cuando se registro                            |
| status                        | propuesta, vigente o reemplazada              |
| supersedes / superseded_by    | que reemplaza o que la reemplazo              |
| verification                  | tests_passed, revisado, sin_verificar o       |
|                               | afirmado_por_agente                           |

La procedencia dice QUIEN lo afirmo; la verificacion, QUE lo respalda: "el
agente dijo que termino" (afirmado_por_agente) no es "los tests lo comprobaron"
(tests_passed). Una nota reemplazada no se borra (regla aditiva): sale del
contexto y su historia queda en la cadena supersedes. Una nota sin estos campos
sigue valiendo: status vacio = vigente. `pinned` sigue siendo solo proteccion
contra sobrescritura.

Capa base: funciones puras sobre dicts de nota, sin E/S."""

import datetime as dt
import re

ESTADOS = ("propuesta", "vigente", "reemplazada")
_ALIAS_ESTADO = {"proposed": "propuesta", "active": "vigente", "current": "vigente",
                 "superseded": "reemplazada", "reemplazado": "reemplazada"}
VERIFICACIONES = ("tests_passed", "revisado", "sin_verificar", "afirmado_por_agente")
# etiqueta que acompana al fragmento en el contexto de un agente: que sepa que
# lo que lee no esta comprobado. sin_verificar (casi toda sesion humana) no se
# marca: seria ruido en cada nota.
ETIQUETAS = {"afirmado_por_agente": "[sin comprobar] ", "propuesta": "[propuesta] "}


def ahora() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def lista(valor) -> list:
    """Nombres de un campo de enlaces: "[a, b]", "a, b", "[[a]]" o una lista."""
    if valor in (None, ""):
        return []
    partes = valor if isinstance(valor, list) else re.split(r",", str(valor).strip().strip("[]"))
    out = []
    for p in partes:
        p = str(p).strip().strip("[]").strip().strip("\"'")
        if p and p not in out:
            out.append(p)
    return out


def estado(n: dict) -> str:
    """Estado normalizado; vacio = vigente. Un valor desconocido se devuelve tal cual."""
    crudo = str((n.get("meta") or {}).get("status", "")).strip().lower()
    if not crudo:
        return "vigente"
    return _ALIAS_ESTADO.get(crudo, crudo)


def verificacion(n: dict) -> str:
    return str((n.get("meta") or {}).get("verification", "")).strip().lower()


def etiqueta(n: dict) -> str:
    return ETIQUETAS.get(verificacion(n)) or ETIQUETAS.get(estado(n)) or ""


def reemplazos(notas: list) -> dict:
    """{nombre: quien la reemplaza}. Cuenta las dos direcciones: `superseded_by`
    en la nota vieja y `supersedes` en la nueva (si la nueva no fue reemplazada
    a su vez). Sin la segunda, una nota vieja seguia entrando al contexto cuando
    solo la nueva declaraba el reemplazo."""
    out = {}
    for n in notas:
        por = lista((n.get("meta") or {}).get("superseded_by"))
        if por:
            out[n["name"]] = por[0]
        elif estado(n) == "reemplazada":
            out.setdefault(n["name"], "")
    for n in notas:
        if estado(n) == "reemplazada":
            continue
        for viejo in lista((n.get("meta") or {}).get("supersedes")):
            out.setdefault(viejo, n["name"])
    return out


def motivo(nombre: str, mapa: dict) -> str | None:
    if nombre not in mapa:
        return None
    return f"reemplazada por {mapa[nombre]}" if mapa[nombre] else "reemplazada"


def revisar(notas: list) -> list:
    """Problemas de vigencia para el lint: [{"nota", "problema"}]. Solo mira la
    estructura declarada; no infiere contradicciones del texto (eso sigue siendo
    registrar-no-inferir)."""
    por_nombre = {n["name"]: n for n in notas}
    problemas = []

    def mal(nota, texto):
        problemas.append({"nota": nota, "problema": texto})

    reemplazantes = {}                       # viejo -> [vigentes que dicen reemplazarlo]
    for n in notas:
        nm, m = n["name"], n.get("meta") or {}
        est = estado(n)
        if est not in ESTADOS:
            mal(nm, f"status desconocido '{m.get('status')}' (validos: {', '.join(ESTADOS)})")
        ver = verificacion(n)
        if ver and ver not in VERIFICACIONES:
            mal(nm, f"verification desconocida '{m.get('verification')}' "
                    f"(validas: {', '.join(VERIFICACIONES)})")
        for campo in ("supersedes", "superseded_by"):
            for otro in lista(m.get(campo)):
                if otro == nm:
                    mal(nm, f"{campo} se apunta a si misma")
                elif otro not in por_nombre:
                    mal(nm, f"cadena rota: {campo} -> [[{otro}]] no existe")
        por = lista(m.get("superseded_by"))
        if por and est == "vigente" and str(m.get("status", "")).strip():
            mal(nm, f"marcada vigente pero superseded_by [[{por[0]}]]")
        for nuevo in por:
            if nuevo in por_nombre and nm not in lista(por_nombre[nuevo]["meta"].get("supersedes")):
                mal(nm, f"cadena incompleta: [[{nuevo}]] no declara supersedes: {nm}")
        if est != "reemplazada":
            for viejo in lista(m.get("supersedes")):
                if viejo == nm or viejo not in por_nombre:
                    continue
                reemplazantes.setdefault(viejo, []).append(nm)
                v = por_nombre[viejo]
                if estado(v) != "reemplazada" and not lista(v["meta"].get("superseded_by")):
                    mal(viejo, f"sigue vigente aunque [[{nm}]] la reemplaza "
                               "(marcala status: reemplazada y superseded_by)")
    for viejo, nuevos in sorted(reemplazantes.items()):
        if len(nuevos) > 1:
            mal(viejo, "dos vigentes la reemplazan y se contradicen: "
                       + ", ".join(f"[[{x}]]" for x in sorted(nuevos)))
    # ciclos en superseded_by (A -> B -> A)
    for n in notas:
        visto, actual = [], n["name"]
        while actual in por_nombre and actual not in visto:
            visto.append(actual)
            sig = lista(por_nombre[actual]["meta"].get("superseded_by"))
            actual = sig[0] if sig else None
        if actual in visto and actual == n["name"] and len(visto) > 1:
            mal(n["name"], "ciclo de reemplazos: " + " -> ".join(visto + [actual]))
    return problemas


# --------------------------------------------------------------- sesiones/runs

def de_veredicto(veredicto: str | None) -> str:
    """Verificacion de un run segun sus validadores. Solo una prueba funcional
    aprobada vale tests_passed; todo lo demas es lo que el agente afirmo."""
    return "tests_passed" if veredicto == "funcional" else "afirmado_por_agente"


def de_cabecera(texto: str) -> dict:
    """Procedencia declarada en la cabecera de una sesion cruda (las que escribe
    la captura de runs: `Run:` y `Verificacion:`). Una sesion sin cabecera es una
    conversacion: nadie la comprobo (sin_verificar)."""
    cab = texto[:600]
    out = {}
    m = re.search(r"^Run:\s*(\S+)\s*$", cab, re.M)
    if m:
        out["source_run"] = m.group(1)
    m = re.search(r"^Verificacion:\s*([a-z_]+)\s*$", cab, re.M)
    if m and m.group(1) in VERIFICACIONES:
        out["verification"] = m.group(1)
    elif "source_run" in out or re.search(r"^Fuente:\s*soou run", cab, re.M):
        out["verification"] = "afirmado_por_agente"
    else:
        out["verification"] = "sin_verificar"
    return out
