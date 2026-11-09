@echo off
REM Mantenimiento del cerebro (para una tarea semanal de Windows): estado, lint
REM y reindexado. Corre desde la carpeta de este archivo.
cd /d "%~dp0"
echo ==== soou doctor %DATE% %TIME% ==== > soou_doctor_report.txt
python soou.py doctor >> soou_doctor_report.txt 2>&1
python soou.py reindex >> soou_doctor_report.txt 2>&1
