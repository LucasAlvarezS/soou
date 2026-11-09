"""Agente falso para probar el ejecutor sin agentes reales (ni red, ni costo).

    python agente_falso.py eco TEXTO              -> imprime TEXTO
    python agente_falso.py hijo-lento ARCHIVO S   -> lanza un hijo que escribe ARCHIVO
                                                     a los S segundos, y espera mucho
    python agente_falso.py escribe ARCHIVO        -> crea ARCHIVO en el cwd e imprime "listo"
    python agente_falso.py obediente PROMPT       -> hace lo que dice el prompt de
                                                     conformidad, sin mirar el modo
"""

import subprocess
import sys
import time
from pathlib import Path

modo = sys.argv[1]
if modo == "eco":
    sys.stdout.write(sys.argv[2])
elif modo == "escribe":
    Path(sys.argv[2]).write_text("hecho", encoding="utf-8")
    print("listo")
elif modo == "obediente":
    prompt = sys.argv[2]
    if "conformidad-timeout" in prompt:
        time.sleep(60)
    elif "Crea el archivo" in prompt:
        Path(prompt.split("Crea el archivo", 1)[1].split()[0]).write_text("ok", encoding="utf-8")
        print("listo")
    else:
        print(Path("README.md").read_text(encoding="utf-8").splitlines()[0])
elif modo == "hijo-lento":
    archivo, segundos = sys.argv[2], float(sys.argv[3])
    codigo = (f"import time, pathlib; time.sleep({segundos}); "
              f"pathlib.Path({archivo!r}).write_text('el hijo sobrevivio')")
    subprocess.Popen([sys.executable, "-c", codigo])
    time.sleep(60)
elif modo == "hijo-de-hijo":
    # un segundo nivel: el hijo lanza al nieto, que es el que escribe
    archivo, segundos = sys.argv[2], float(sys.argv[3])
    subprocess.Popen([sys.executable, __file__, "hijo-lento", archivo, str(segundos)])
    time.sleep(60)
