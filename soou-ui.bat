@echo off
rem Lanza la UI de escritorio de soou sin ventana de consola.
rem Doble clic para abrir. Requiere Python con pywebview instalado (pip install pywebview).
start "" pythonw "%~dp0soou.py" ui
