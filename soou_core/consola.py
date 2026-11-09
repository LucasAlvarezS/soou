"""Modulo consola de soou (extraido de soou.py).

soou.py lo re-exporta; dentro de soou_core se usa como `consola.nombre`."""

import sys
import time


_MARCA_BLOQUES = (
    "███████╗ ██████╗  ██████╗ ██╗   ██╗",
    "██╔════╝██╔═══██╗██╔═══██╗██║   ██║",
    "███████╗██║   ██║██║   ██║██║   ██║",
    "╚════██║██║   ██║██║   ██║██║   ██║",
    "███████║╚██████╔╝╚██████╔╝╚██████╔╝",
    "╚══════╝ ╚═════╝  ╚═════╝  ╚═════╝ ",
)


_MARCA_ASCII = (
    " ___   ___   ___  _   _ ",
    "/ __| / _ " + chr(92) + " / _ " + chr(92) + "| | | |",
    chr(92) + "__ " + chr(92) + "| (_) | (_) | |_| |",
    "|___/ " + chr(92) + "___/ " + chr(92) + "___/ " + chr(92) + "__,_|",
)


def _soporta_bloques() -> bool:
    """Si la consola no puede codificar los bloques, imprimirlos la hace fallar."""
    try:
        codec = sys.stdout.encoding or "ascii"
        "".join(_MARCA_BLOQUES).encode(codec)
        return True
    except Exception:
        return False


def mostrar_marca(subtitulo: str = "", animar: bool = True) -> None:
    """Dibuja la marca si hay terminal. Nunca lanza: si algo falla, no dibuja."""
    try:
        if not sys.stdout.isatty():
            return
        filas = _MARCA_BLOQUES if _soporta_bloques() else _MARCA_ASCII
        print()
        for fila in filas:
            print("  " + fila)
            if animar:
                time.sleep(0.045)
        if subtitulo:
            print("  " + subtitulo)
        print()
    except Exception:
        pass        # una marca decorativa jamas debe romper un comando


class Girador:
    """Indicador de progreso para las esperas largas del CLI.

    Escribe a stderr y NO a stdout, para que la salida de un comando siga siendo
    canalizable a otro. Y solo si hay terminal de verdad: la tarea programada
    corre sin consola y CI captura la salida, y en esos casos los codigos de
    escape serian basura en un archivo de log.

    Sin dependencias y en ASCII, igual que el resto de la herramienta.
    """

    CUADROS = "|/-" + chr(92)

    def __init__(self, texto: str):
        self.texto = texto
        self.activo = False
        self._hilo = None

    def _visible(self) -> bool:
        try:
            return bool(sys.stderr.isatty())
        except Exception:
            return False

    def _girar(self):
        i = 0
        while self.activo:
            marco = self.CUADROS[i % len(self.CUADROS)]
            sys.stderr.write("\r  " + marco + " " + self.texto + " ")
            sys.stderr.flush()
            i += 1
            time.sleep(0.12)

    def _borrar(self):
        sys.stderr.write("\r" + " " * (len(self.texto) + 6) + "\r")
        sys.stderr.flush()

    def __enter__(self):
        if self._visible():
            import threading
            self.activo = True
            self._hilo = threading.Thread(target=self._girar, daemon=True)
            self._hilo.start()
        return self

    def __exit__(self, *_):
        if self.activo:
            self.activo = False
            self._hilo.join(timeout=1)
            self._borrar()
        return False
