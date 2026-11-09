@echo off
REM Destilacion del cerebro (para una tarea programada de Windows).
REM Convierte las sesiones L0 pendientes en notas. Cada corrida deja log en
REM sessions\distill-log\. Corre desde la carpeta de este archivo.
cd /d "%~dp0"
if not exist "sessions\distill-log" mkdir "sessions\distill-log"
echo ==== distill %DATE% %TIME% ==== >> "sessions\distill-log\cron.log"
python soou.py distill >> "sessions\distill-log\cron.log" 2>&1
