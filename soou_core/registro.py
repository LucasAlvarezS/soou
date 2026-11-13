"""Modulo registro de soou (extraido de soou.py).

soou.py lo re-exporta; dentro de soou_core se usa como `registro.nombre`."""

import json
import os
import shutil
from soou_core import acp, agentes, config, consola, indice, rutas


class HttpAdapter(agentes.AgentAdapter):
    """Agente SOLO-API (sin CLI), p.ej. DeepSeek. Habla el protocolo OpenAI-compatible
    (chat/completions) con stdlib puro (urllib). Devuelve TEXTO: NO aplica cambios en
    disco por sí mismo (no es agéntico), así que sirve para LECTURA/PLAN y verificación
    cruzada (dual-agent), no para --write. La API key sale de una env var (o .env)."""
    can_write = False
    restriccion_lectura = "texto"        # solo devuelve texto: no toca el disco

    def __init__(self, name, *, url, model, api_key_env, system=None):
        self.name = name
        self._url = url
        self._model = model
        self._key_env = api_key_env
        self._system = system

    def _key(self):
        return os.environ.get(self._key_env) or indice._env(self._key_env)

    def _bin(self):
        return self.name

    def resolve(self):
        return self._url if self._key() else None

    def available(self) -> bool:
        return bool(self._key())

    def probe(self, timeout: int = 25):
        return (bool(self._key()),
                f"{self._model} @ {self._url} · key {self._key_env}: "
                f"{'presente' if self._key() else 'AUSENTE'}")

    def run(self, prompt, allow_write=False, timeout=1200, cwd=None, model=None,
            reasoning=None) -> dict:
        key = self._key()
        if not key:
            return {"ok": False, "output": f"falta la API key en {self._key_env}"}
        if allow_write:
            prompt = ("No puedes escribir archivos directamente. Devuelve un PLAN concreto "
                      "y los cambios propuestos (rutas + diffs) en texto.\n\n" + prompt)
        msgs = ([{"role": "system", "content": self._system}] if self._system else []) \
            + [{"role": "user", "content": prompt}]
        selected_model = model or self._model
        body = json.dumps({"model": selected_model, "messages": msgs, "stream": False}).encode()
        import urllib.request   # perezoso: solo los agentes http lo usan
        req = urllib.request.Request(
            self._url, data=body, method="POST",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=min(timeout, 300)) as resp:
                d = json.loads(resp.read().decode("utf-8"))
            return {"ok": True, "output": d["choices"][0]["message"]["content"]}
        except Exception as e:
            return {"ok": False, "output": f"error API {self.name}: {e}"}


_ADAPTER_TYPES = {"claude": agentes.ClaudeAdapter, "codex": agentes.CodexAdapter}

# ------------------------------------------------------------------ manifiestos
# Un agente se define con el mismo objeto en soou.config.json ("agents") o en un
# archivo <datos>/agents/NOMBRE.json. Se valida al cargar: uno invalido no se
# construye, y `soou agents` dice por que.

_LECTURAS = set(agentes.RESTRICCIONES_LECTURA) | {"no_verificada"}
_LISTA_STR = lambda v: isinstance(v, list) and all(isinstance(x, str) for x in v)  # noqa: E731
_COMANDO = lambda v: (isinstance(v, str) and v.strip()) or (_LISTA_STR(v) and v)  # noqa: E731
_CAMPOS = {   # tipo -> {campo: (obligatorio, validador, descripcion)}
    "cmd": {"command": (True, lambda v: isinstance(v, str) and v.strip(), "texto"),
            "args": (False, _LISTA_STR, "lista de textos"),
            "probe_args": (False, _LISTA_STR, "lista de textos"),
            "lectura": (False, lambda v: v in _LECTURAS, "una restriccion conocida")},
    "acp": {"command": (True, _COMANDO, "texto o lista de textos"),
            "lectura": (False, lambda v: v in _LECTURAS, "una restriccion conocida")},
    "http": {"url": (True, lambda v: isinstance(v, str) and v.startswith(("http://", "https://")),
                     "una URL http(s)"),
             "model": (False, lambda v: isinstance(v, str), "texto"),
             "api_key_env": (False, lambda v: isinstance(v, str), "texto"),
             "system": (False, lambda v: isinstance(v, str), "texto")},
    "receta": {"write": (False, lambda v: v is None or _LISTA_STR(v), "lista de textos"),
               "read": (False, lambda v: v is None or _LISTA_STR(v), "lista de textos o null"),
               "bins": (False, _LISTA_STR, "lista de textos"),
               "probe": (False, _LISTA_STR, "lista de textos"),
               "lectura": (False, lambda v: v in _LECTURAS, "una restriccion conocida"),
               "prompt_via": (False, lambda v: v in ("argv", "stdin"), "argv o stdin")},
}
_CAMPOS["deepseek"] = {**_CAMPOS["http"], "url": (False, _CAMPOS["http"]["url"][1], "una URL")}
_COMUNES = {"name", "type", "description", "model"}


def validar_spec(spec) -> list:
    """Errores de un agente declarado (lista vacia = valido). Un campo
    desconocido no es error (ver `campos_desconocidos`)."""
    if not isinstance(spec, dict):
        return ["no es un objeto JSON"]
    t = spec.get("type")
    if t in _ADAPTER_TYPES:
        campos = {}
    elif t in agentes.RECIPE_REGISTRY:
        campos = _CAMPOS["receta"]
    elif t in _CAMPOS:
        campos = _CAMPOS[t]
    else:
        tipos = sorted({*_ADAPTER_TYPES, *agentes.RECIPE_REGISTRY, "cmd", "acp", "http", "deepseek"})
        return [f"type '{t}' desconocido (validos: {', '.join(tipos)})"]
    errores = []
    if "name" in spec and not (isinstance(spec["name"], str) and spec["name"].strip()):
        errores.append("name tiene que ser un texto no vacio")
    for campo, (oblig, ok, desc) in campos.items():
        if campo not in spec:
            if oblig:
                errores.append(f"falta '{campo}' ({desc})")
        elif not ok(spec[campo]):
            errores.append(f"'{campo}' tiene que ser {desc}")
    return errores


def campos_desconocidos(spec) -> list:
    """Campos que soou no usa: casi siempre un nombre mal escrito (p.ej. "lecura").
    Solo se avisa; los que empiezan con "_" son comentarios."""
    if not isinstance(spec, dict):
        return []
    t = spec.get("type")
    campos = (_CAMPOS["receta"] if t in agentes.RECIPE_REGISTRY else _CAMPOS.get(t, {}))
    return sorted(k for k in set(spec) - set(campos) - _COMUNES if not str(k).startswith("_"))


def _dir_manifiestos():
    return rutas.ROOT / "agents" if rutas.ROOT else None


def cargar_manifiestos() -> tuple:
    """(specs validas, [(archivo, errores)]) de <datos>/agents/*.json."""
    d = _dir_manifiestos()
    if not d or not d.is_dir():
        return [], []
    validas, malas = [], []
    for f in sorted(d.glob("*.json")):
        try:
            spec = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            malas.append((f.name, [f"JSON invalido: {e}"]))
            continue
        if isinstance(spec, dict):
            spec.setdefault("name", f.stem)
        errores = validar_spec(spec)
        (malas.append((f.name, errores)) if errores else validas.append(spec))
    return validas, malas


def _especificaciones() -> tuple:
    """Agentes declarados en orden (config y luego manifiestos), sus errores y
    sus avisos (campos desconocidos)."""
    validas, malas = [], []
    for i, spec in enumerate(config.load_config()["agents"]):
        errores = validar_spec(spec)
        if errores:
            malas.append((f"soou.config.json agents[{i}]", errores))
        else:
            validas.append(spec)
    de_archivos, malas_archivos = cargar_manifiestos()
    nombres = {s.get("name", s.get("type")) for s in validas}
    for spec in de_archivos:
        if spec["name"] in nombres:
            malas_archivos.append((spec["name"] + ".json",
                                   ["ya hay un agente con ese nombre en soou.config.json"]))
        else:
            validas.append(spec)
            nombres.add(spec["name"])
    avisos = [(s.get("name", s.get("type")), x) for s in validas if (x := campos_desconocidos(s))]
    return validas, malas + malas_archivos, avisos


def build_adapters() -> list:
    """Construye la lista de agentes (orden = prioridad de fallback) desde config
    y los manifiestos. Los invalidos se omiten (ver `soou agents`)."""
    out = []
    for spec in _especificaciones()[0]:
        t = spec.get("type")
        if t in _ADAPTER_TYPES:
            out.append(_ADAPTER_TYPES[t]())
        elif t in agentes.RECIPE_REGISTRY:                 # receta conocida (gemini, aider, …)
            out.append(agentes.RecipeAdapter(spec.get("name", t),
                                     {**agentes.RECIPE_REGISTRY[t], **{k: v for k, v in spec.items()
                                      if k in ("write", "read", "bins", "probe", "lectura", "prompt_via")}}))
        elif t == "cmd" and spec.get("command"):
            out.append(agentes.GenericAdapter(spec.get("name", spec["command"]),
                                      spec["command"], spec.get("args"),
                                      spec.get("probe_args"), spec.get("lectura")))
        elif t == "acp" and spec.get("command"):    # cualquier agente ACP por stdio
            out.append(acp.AcpAdapter(spec.get("name", "acp"), spec["command"],
                                      spec.get("lectura", "cliente_acp")))
        elif t == "deepseek":                      # atajo: DeepSeek con defaults
            out.append(HttpAdapter(spec.get("name", "deepseek"),
                                   url="https://api.deepseek.com/v1/chat/completions",
                                   model=spec.get("model", "deepseek-chat"),
                                   api_key_env=spec.get("api_key_env", "DEEPSEEK_API_KEY"),
                                   system=spec.get("system")))
        elif t == "http" and spec.get("url"):      # cualquier API OpenAI-compatible
            out.append(HttpAdapter(spec.get("name", "http"), url=spec["url"],
                                   model=spec.get("model", "deepseek-chat"),
                                   api_key_env=spec.get("api_key_env", "OPENAI_API_KEY"),
                                   system=spec.get("system")))
    return out or [agentes.ClaudeAdapter(), agentes.CodexAdapter()]


def _agent_status(ad):
    """(disponible, detalle) legible. Distingue una CLI ausente del PATH de un
    agente solo-API al que unicamente le falta la clave: son fallos distintos y
    el consejo de 'revisa el PATH' no aplica al segundo."""
    env = getattr(ad, "_key_env", None)
    if env is not None:                      # agente solo-API (HttpAdapter)
        return (ad.available(),
                f"key {env} presente" if ad.available() else f"falta la variable {env}")
    exe = ad.resolve()
    return (exe is not None, exe or "no instalado (CLI fuera del PATH)")


def cmd_agents(args):
    """Preflight de agentes: resuelve cada ejecutable y, con --probe, lo LANZA de verdad
    (--version) para confirmar que se puede ejecutar. Con --detect, autodescubre qué IAs
    agénticas conocidas están instaladas (registro de recetas) y sugiere el config."""
    if getattr(args, "detect", False):
        print("Autodetección de IAs agénticas de código instaladas:\n")
        configured = {s.get("type") for s in config.load_config()["agents"]}
        known = {"claude": ["claude"], "codex": ["codex"],
                 **{k: v["bins"] for k, v in agentes.RECIPE_REGISTRY.items()}}
        found = []
        for typ, bins in known.items():
            hit = next((b for b in bins if shutil.which(b)), None)
            if hit:
                found.append(typ)
                mark = "ya en config" if typ in configured else "NO en config"
                print(f"  ✓ {typ:14s} → {shutil.which(hit)}   ({mark})")
        if not found:
            print("  (ninguna conocida en el PATH)")
        add = [t for t in found if t not in configured]
        if add:
            print("\nPara sumarlas, añade a soou.config.json → \"agents\":")
            for t in add:
                print(f'    {{ "name": "{t}", "type": "{t}" }},')
        print(f"\nRegistro cubre: {', '.join(sorted(known))}.")
        print("(valida el launch con: soou.py agents --probe)")
        return
    print("Agentes del harness (orden = prioridad de fallback):\n")
    ads = build_adapters()
    _, malas, avisos = _especificaciones()
    ancho = max((len(a.name) for a in ads), default=12)
    vivos = apis = apis_vivas = 0
    for ad in ads:
        ok, detalle = _agent_status(ad)
        es_api = getattr(ad, "_key_env", None) is not None
        apis += 1 if es_api else 0
        apis_vivas += 1 if (es_api and ok) else 0
        vivos += 1 if ok else 0
        rol = "escribe     " if getattr(ad, "can_write", True) else "solo-lectura"
        marca = "[si]" if ok else "[--]"
        print(f"  {marca} {ad.name:<{ancho}}  {rol}  {detalle}")
        if getattr(args, "probe", False):
            if not ok:
                print(f"       {'':<{ancho}}  probe : no disponible")
            else:
                with consola.Girador("probando " + ad.name + "..."):
                    pok, pdet = ad.probe()
                print(f"       {'':<{ancho}}  probe : {'OK ' if pok else 'FALLO '} {pdet}")
    for origen, errores in malas:
        print(f"  [!!] {origen}: no se cargo: " + "; ".join(errores))
    for nombre, extra in avisos:
        print(f"  [aviso] {nombre}: campo(s) que soou no usa: {', '.join(extra)}")
    clis, clis_vivas = len(ads) - apis, vivos - apis_vivas
    print(f"\n{vivos} de {len(ads)} disponibles  ·  CLIs {clis_vivas}/{clis}  ·  APIs {apis_vivas}/{apis}")
    if not getattr(args, "probe", False):
        print("(usa 'soou.py agents --probe' para lanzar cada agente y validar el launch)")
