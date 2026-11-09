"""Saneado de transcripciones antes de escribirlas en disco: quita los bloques
que inyectan los clientes y redacta secretos. Funciones puras.

Extraido de soou.py (que lo re-exporta: `soou.<nombre>` sigue valiendo)."""

import re


_RUIDO_BLOQUES = re.compile(
    r"<(system-reminder|environment_context|user_instructions|command-message|"
    r"command-name|command-args|local-command-stdout|local-command-stderr|"
    r"local-command-caveat)\b[^>]*>.*?</\1>",
    re.S | re.I)


_SECRETOS = [
    (re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_\-]{16,}"), "sk-…"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "gh_…"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), "github_pat_…"),
    (re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), "AKIA…"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"), "xox…"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"), "AIza…"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"), "jwt…"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
     "[clave privada]"),
    (re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key)"
                r"(\s*[:=]\s*)(['\"]?)[^\s'\"]{6,}\3"), r"\1\2\3…\3"),
    (re.compile(r"(?i)\b(postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://([^:/\s]+):[^@\s]+@"),
     r"\1://\2:…@"),
]


SESSION_MSG_MAX = 2000          # tope por mensaje (como antes)


SESSION_MAX_CHARS = 40000       # tope por sesion: L0 acotado en disco


def _limpiar_texto(texto: str) -> str:
    """Quita bloques inyectados por el cliente y redacta secretos."""
    if not texto:
        return ""
    t = _RUIDO_BLOQUES.sub("", texto)
    # bloque inyectado que quedo SIN cierre (recortado a 2000 en capturas viejas)
    t = re.sub(r"<(system-reminder|environment_context|user_instructions)\b[^>]*>.*\Z",
               "", t, flags=re.S | re.I)
    for patron, reemplazo in _SECRETOS:
        t = patron.sub(reemplazo, t)
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def _linea_sesion(rol: str, texto: str):
    """'**rol:** texto' saneado y recortado, o None si no queda nada util."""
    limpio = _limpiar_texto(texto)
    if not limpio or rol not in ("user", "assistant"):
        return None
    return f"**{rol}:** {limpio[:SESSION_MSG_MAX]}"
