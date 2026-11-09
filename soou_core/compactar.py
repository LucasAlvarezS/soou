"""Modulo compactar de soou (extraido de soou.py).

soou.py lo re-exporta; dentro de soou_core se usa como `compactar.nombre`."""

from pathlib import Path
import datetime as dt
import json
import re
import shutil
import time
from soou_core import indice, rutas, sesiones


COMPACT_RAW_DIAS = 30          # crudos ya sintetizados por IA que se pueden archivar


def _es_pinned(nota: dict) -> bool:
    return str(nota["meta"].get("pinned", "")).lower() == "true"


def _estado_crudo(ruta: Path) -> str:
    """'yes' (sintetizada por IA), 'lite' (solo Nivel A) o 'no'."""
    try:
        cabecera = ruta.read_text(encoding="utf-8", errors="ignore")[:60]
    except OSError:
        return "no"
    for estado in ("yes", "lite"):
        if "distilled: " + estado in cabecera:
            return estado
    return "no"


def _nota_de_crudo(ruta: Path) -> str:
    """Nombre de la nota-sesion que el Nivel A genera para este crudo."""
    return "sesion-" + sesiones._source_of_session(ruta.name) + "-" + sesiones._sid_of_session(ruta.name)


def _nota_legacy_de_crudo(ruta: Path) -> str:
    """Nombre con el que el Nivel A ANTERIOR (sid completo, hasta 64) nombraba la nota.

    El cambio a sid recortado+hash es correcto para sesiones nuevas, pero las ~200
    notas ya escritas conservan el nombre viejo. Sin este respaldo el compact las
    daba por perdidas y '--recuperar' las habria duplicado."""
    m = re.search(r"session-(?:codex-)?(.+)\.md$", ruta.name)
    sid = re.sub(r"[^A-Za-z0-9_-]", "", m.group(1) if m else ruta.stem)[:64]
    src = "codex" if "codex" in ruta.name else "claude-code"
    return f"sesion-{src}-{sid}"


def _buscar_nota_de_crudo(ruta: Path, por_nombre: dict):
    return por_nombre.get(_nota_de_crudo(ruta)) or por_nombre.get(_nota_legacy_de_crudo(ruta))


def _stub(nota: dict) -> bool:
    """Nota sin contenido real: placeholder o cuerpo casi vacio."""
    return indice.note_placeholder(nota) or len(" ".join(nota["body"].split())) < 200


def _reapuntar_enlaces(notas: list, viejo: str, nuevo: str, aplicar: bool) -> int:
    """Reescribe [[viejo]] por [[nuevo]] en todas las notas. Devuelve cuantas tocaria."""
    patron = re.compile(r"\[\[" + re.escape(viejo) + r"(\|[^\]]*)?\]\]")
    tocadas = 0
    for n in notas:
        if n["name"] in (viejo, nuevo):
            continue
        original = n["path"].read_text(encoding="utf-8", errors="ignore")
        nuevo_txt = patron.sub(lambda m: "[[" + nuevo + (m.group(1) or "") + "]]", original)
        if nuevo_txt != original:
            tocadas += 1
            if aplicar:
                n["path"].write_text(nuevo_txt, encoding="utf-8")
    return tocadas


def _desenlazar_archivadas(notas: list, retiradas: set, aplicar: bool) -> int:
    """[[x]] hacia una nota ya archivada pasa a texto plano marcado: sin enlaces
    rotos y sin perder la pista de donde estaba. Devuelve cuantas notas toca."""
    vivas = {n["name"] for n in notas if Path(n.get("path") or "").exists()}
    muertas = {x for x in retiradas if x not in vivas}
    if not muertas:
        return 0
    patron = re.compile(r"\[\[(" + "|".join(re.escape(x) for x in sorted(muertas)) +
                        r")(\|([^\]]*))?\]\]")
    tocadas = 0
    for n in notas:
        p = Path(n.get("path") or "")
        if not p.exists() or rutas.ARCHIVO in p.parents:
            continue
        original = p.read_text(encoding="utf-8", errors="ignore")
        nuevo = patron.sub(lambda m: (m.group(3) or m.group(1)) + " (archivada)", original)
        if nuevo != original:
            tocadas += 1
            if aplicar:
                p.write_text(nuevo, encoding="utf-8")
    return tocadas


def _notas_en_archivo() -> set:
    base = rutas.ARCHIVO / "wiki"
    if not base.exists():
        return set()
    return {f.name[:-len(".md.gz")] for f in base.rglob("*.md.gz")}


def plan_de_compactacion(dias_crudos: int = COMPACT_RAW_DIAS) -> dict:
    """Que se archivaria y que queda para revisar. No toca nada."""
    notas = indice.load_notes()
    por_nombre = {n["name"]: n for n in notas}
    acciones, revisar = [], []

    # A) notas-sesion cuyo crudo YA fue sintetizado por IA: su contenido vive en
    #    las notas-concepto que produjo el Nivel B, la nota-sesion es redundante.
    # C) y esos mismos crudos, pasada la ventana, tampoco hacen falta en disco.
    ahora = time.time()
    huerfanos = []          # crudo marcado como destilado cuya nota NO existe
    pendientes_b = 0
    for crudo in sorted(rutas.SESSIONS.rglob("session-*.md")) if rutas.SESSIONS.exists() else []:
        estado = _estado_crudo(crudo)
        nota_esperada = _buscar_nota_de_crudo(crudo, por_nombre)
        if estado in ("lite", "yes") and nota_esperada is None:
            # el crudo dice destilado pero su nota no esta: paso con el bug de
            # colision de identificadores, que hacia que varias sesiones
            # escribieran la MISMA nota. Se puede recuperar reseteando la marca.
            huerfanos.append(crudo)
        if estado == "lite" and nota_esperada is not None:
            pendientes_b += 1
        if estado != "yes":
            continue
        nota = nota_esperada
        if nota is not None and not _es_pinned(nota):
            acciones.append({"tipo": "nota-sesion", "ruta": nota["path"],
                             "motivo": "su sesion ya fue sintetizada por IA"})
        try:
            edad = (ahora - crudo.stat().st_mtime) / 86400
        except OSError:
            continue
        if edad >= dias_crudos:
            acciones.append({"tipo": "crudo", "ruta": crudo,
                             "motivo": f"sintetizado y con {int(edad)} dias"})

    # B) duplicados donde uno de los dos es un stub: se retira el stub y sus
    #    enlaces se reapuntan al que si tiene contenido. Un duplicado REAL (los
    #    dos con cuerpo) no se fusiona solo: eso necesita criterio, va a revisar.
    grupos = {}
    for n in notas:
        grupos.setdefault(re.sub(r"[-_\s]", "", n["name"].lower()), []).append(n)
    fusiones = []
    for miembros in grupos.values():
        if len(miembros) < 2:
            continue
        stubs = [n for n in miembros if _stub(n) and not _es_pinned(n)]
        enteras = [n for n in miembros if not _stub(n)]
        if stubs and enteras:
            for s in stubs:
                fusiones.append({"stub": s, "superviviente": enteras[0]})
        else:
            revisar.append("duplicado que necesita criterio: "
                           + " ~ ".join(n["name"] for n in miembros))

    # lo que NO se toca pero conviene mirar
    if huerfanos:
        revisar.append(f"{len(huerfanos)} sesion(es) marcadas como destiladas pero sin "
                       "su nota (se perdieron por la colision de identificadores ya "
                       "corregida): recuperables con 'soou.py compact --recuperar --apply'")
    if pendientes_b:
        revisar.append(f"{pendientes_b} sesion(es) solo con Nivel A: su nota-sesion no se "
                       "retira hasta que el Nivel B las sintetice (soou.py sync --deep)")
    referenciadas = {l for n in notas for l in indice.note_links(n)}
    huerfanas = [n["name"] for n in notas
                 if n["name"] not in referenciadas
                 and n["meta"].get("type") in indice.CONCEPT_TYPES]
    if huerfanas:
        revisar.append(f"{len(huerfanas)} nota(s)-concepto que nadie enlaza: "
                       + ", ".join(sorted(huerfanas)[:5]))
    return {"acciones": acciones, "fusiones": fusiones, "revisar": revisar,
            "huerfanos": huerfanos,
            "notas": notas, "total_notas": len(notas)}


def _mover_al_archivo(ruta: Path, aplicar: bool) -> str:
    """Mueve preservando la ruta relativa. Devuelve el destino."""
    try:
        relativa = ruta.relative_to(rutas.ROOT)
    except ValueError:
        relativa = Path(ruta.name)
    # Comprimido: el archivo es frio (solo se consulta para revertir), y el texto
    # de sesiones/notas baja ~70-80% con gzip. Se recupera con `gzip -d`.
    destino = rutas.ARCHIVO / relativa.with_name(relativa.name + ".gz")
    if aplicar:
        import gzip
        destino.parent.mkdir(parents=True, exist_ok=True)
        if destino.exists():
            destino = destino.with_name(ruta.stem + "-" + str(int(time.time()))
                                        + ruta.suffix + ".gz")
        with open(ruta, "rb") as src, gzip.open(destino, "wb", compresslevel=9) as dst:
            shutil.copyfileobj(src, dst)
        ruta.unlink()
    return destino.relative_to(rutas.ROOT).as_posix()


LOG_MAX_LINEAS = 3000           # router-log y trazas: se conserva lo reciente


DISTILL_LOG_DIAS = 30           # logs de destilacion mas viejos -> comprimidos


def _rotar_logs(aplicar: bool) -> list:
    """Acota los logs que crecen sin limite. Lo viejo no se pierde: va comprimido
    a archivo/logs/. Devuelve descripciones de lo que se hizo (o haria)."""
    import gzip
    hechos = []
    for log in (rutas.ROOT / "router-log.jsonl", sesiones._runs_dir() / "trazas.jsonl"):
        try:
            lineas = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
        except OSError:
            continue
        if len(lineas) <= LOG_MAX_LINEAS:
            continue
        viejas, nuevas = lineas[:-LOG_MAX_LINEAS], lineas[-LOG_MAX_LINEAS:]
        destino = rutas.ARCHIVO / "logs" / f"{log.stem}-{dt.date.today():%Y%m%d}.jsonl.gz"
        hechos.append(f"{log.name}: {len(viejas)} linea(s) viejas -> "
                      f"{destino.relative_to(rutas.ROOT).as_posix()}")
        if aplicar:
            destino.parent.mkdir(parents=True, exist_ok=True)
            with gzip.open(destino, "at", encoding="utf-8") as f:
                f.write("\n".join(viejas) + "\n")
            log.write_text("\n".join(nuevas) + "\n", encoding="utf-8")
    dlog = rutas.ROOT / "sessions" / "distill-log"
    if dlog.exists():
        limite = time.time() - DISTILL_LOG_DIAS * 86400
        viejos = [f for f in dlog.rglob("*") if f.is_file() and f.suffix != ".gz"
                  and f.stat().st_mtime < limite]
        if viejos:
            hechos.append(f"distill-log: {len(viejos)} log(s) de mas de "
                          f"{DISTILL_LOG_DIAS} dias -> comprimidos en archivo/")
            for f in viejos if aplicar else []:
                _mover_al_archivo(f, True)
    return hechos


def cmd_compact(args):
    """Retira del cerebro lo que ya esta absorbido. Simula salvo que pases --apply."""
    aplicar = getattr(args, "apply", False)
    plan = plan_de_compactacion(getattr(args, "dias", COMPACT_RAW_DIAS) or COMPACT_RAW_DIAS)
    print("== soou compact ==" + ("" if aplicar else "  (simulacion)") + "\n")
    print(f"Cerebro: {plan['total_notas']} nota(s).\n")

    movidos = []
    manifiesto = rutas.ARCHIVO / "manifiesto.jsonl"

    def registrar(origen: Path, destino: str, motivo: str):
        # el manifiesto se escribe A MEDIDA que se mueve: si algo corta la
        # corrida a mitad, lo ya archivado sigue registrado y es reversible.
        m = {"origen": origen.relative_to(rutas.ROOT).as_posix(), "destino": destino,
             "motivo": motivo}
        movidos.append(m)
        if aplicar:
            manifiesto.parent.mkdir(parents=True, exist_ok=True)
            with manifiesto.open("a", encoding="utf-8") as f:
                f.write(json.dumps({**m, "ts": dt.datetime.now().isoformat(timespec="seconds")},
                                   ensure_ascii=False) + "\n")

    if not plan["acciones"] and not plan["fusiones"]:
        print("  No hay nada que compactar.")
    vistos = set()
    for accion in plan["acciones"]:
        # dos crudos (p.ej. la misma sesion capturada en dos fechas) pueden
        # apuntar a la MISMA nota: se archiva una sola vez.
        if accion["ruta"] in vistos or not Path(accion["ruta"]).exists():
            continue
        vistos.add(accion["ruta"])
        destino = _mover_al_archivo(accion["ruta"], aplicar)
        registrar(accion["ruta"], destino, accion["motivo"])
        print(f"  [{accion['tipo']}] {accion['ruta'].name}")
        print(f"      {accion['motivo']} -> {destino}")
    for f in plan["fusiones"]:
        stub, vivo = f["stub"], f["superviviente"]
        if stub["path"] in vistos or not Path(stub["path"]).exists():
            continue
        vistos.add(stub["path"])
        tocadas = _reapuntar_enlaces(plan["notas"], stub["name"], vivo["name"], aplicar)
        destino = _mover_al_archivo(stub["path"], aplicar)
        registrar(stub["path"], destino, "stub duplicado de " + vivo["name"])
        print(f"  [stub] {stub['name']}  duplica a  {vivo['name']}")
        print(f"      se retira y {tocadas} nota(s) reapuntan sus enlaces -> {destino}")

    if getattr(args, "recuperar", False) and plan["huerfanos"]:
        print(f"\n  [recuperar] {len(plan['huerfanos'])} sesion(es) vuelven a la cola "
              "del Nivel A (solo se cambia la marca; el crudo no se toca):")
        for crudo in plan["huerfanos"]:
            print("      " + crudo.relative_to(rutas.ROOT).as_posix())
            if aplicar:
                sesiones._mark_raw(crudo, "no")

    retiradas = {Path(m["origen"]).stem for m in movidos if m["origen"].startswith("wiki/")}
    retiradas |= _notas_en_archivo()      # tambien lo archivado en corridas anteriores
    if not aplicar:
        # en simulacion las notas siguen en disco: se calcula contra lo que se retiraria
        notas_sim = [n for n in plan["notas"] if n["name"] not in retiradas]
        n_enl = _desenlazar_archivadas(notas_sim, retiradas, False) if retiradas else 0
    else:
        n_enl = _desenlazar_archivadas(plan["notas"], retiradas, True)
    if n_enl:
        print(f"  [enlaces] {n_enl} nota(s) con enlaces a notas archivadas -> texto "
              "plano '(archivada)'")

    for hecho in _rotar_logs(aplicar):
        print(f"  [log] {hecho}")

    if plan["revisar"]:
        print("\nPara mirar vos (no se toco nada de esto):")
        for x in plan["revisar"]:
            print("  · " + x)

    if not aplicar:
        print("\nSimulacion: no se movio nada. Corre con --apply para aplicarlo.")
        return
    if movidos:
        print(f"\n{len(movidos)} archivo(s) movido(s) a {rutas.ARCHIVO.name}/ "
              f"(registro en {manifiesto.relative_to(rutas.ROOT).as_posix()}).")
        print("Nada se borro: para revertir, copia de vuelta lo que quieras.")
    if movidos or n_enl:
        indice.build_index()
        print("Indice reconstruido.")
