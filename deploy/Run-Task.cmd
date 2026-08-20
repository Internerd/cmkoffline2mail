@echo off
rem ---------------------------------------------------------------------------
rem Startskript fuer die Aufgabenplanung.
rem
rem Protokolliert ALLES nach <Basisordner>\logs\task-run.log - auch Fehler, die
rem auftreten BEVOR Python laeuft (fehlende venv, falsche Rechte, falsches
rem Arbeitsverzeichnis). Genau diese Ausgaben verschluckt die Aufgabenplanung.
rem
rem Das Skript wechselt selbst in den Basisordner und setzt PYTHONPATH - es
rem funktioniert daher unabhaengig davon, welches Arbeitsverzeichnis die
rem Aufgabe mitgibt.
rem
rem Parameter werden durchgereicht, z. B.:  Run-Task.cmd --dry-run
rem ---------------------------------------------------------------------------
setlocal

cd /d "%~dp0.." || exit /b 9
set "BASE=%CD%"
set "LOGDIR=%BASE%\logs"
set "LOGFILE=%LOGDIR%\task-run.log"
set "PYEXE=%BASE%\.venv\Scripts\python.exe"

rem Paket auch dann findbar, wenn das Arbeitsverzeichnis abweicht
set "PYTHONPATH=%BASE%"

if not exist "%LOGDIR%" mkdir "%LOGDIR%" 2>nul
if not exist "%LOGDIR%" exit /b 9

rem Einfache Rotation: ab 2 MB wird die alte Datei beiseite gelegt
if exist "%LOGFILE%" (
    for %%A in ("%LOGFILE%") do (
        if %%~zA GTR 2097152 move /y "%LOGFILE%" "%LOGFILE%.old" >nul 2>&1
    )
)

>>"%LOGFILE%" echo.
>>"%LOGFILE%" echo ===== %DATE% %TIME% ^| Start ^| Benutzer: %USERDOMAIN%\%USERNAME% ^| Ordner: %BASE%

if not exist "%PYEXE%" (
    >>"%LOGFILE%" echo FEHLER: Die virtuelle Umgebung fehlt: %PYEXE%
    >>"%LOGFILE%" echo         Bitte deploy\Install-ScheduledTask.ps1 erneut ausfuehren.
    >>"%LOGFILE%" echo ===== Ende ^| Rueckgabewert 9
    exit /b 9
)

"%PYEXE%" -m cmkoffline2mail %* >>"%LOGFILE%" 2>&1
set "RC=%ERRORLEVEL%"

>>"%LOGFILE%" echo ===== %DATE% %TIME% ^| Ende ^| Rueckgabewert %RC%
exit /b %RC%
