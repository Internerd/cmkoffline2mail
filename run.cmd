@echo off
rem Startet cmkoffline2mail von Hand mit der venv aus diesem Ordner.
rem Die Ausgabe erscheint direkt in der Konsole. Beispiele:
rem   run.cmd --validate
rem   run.cmd --dry-run
rem   run.cmd --test-mail ich@firma.de
setlocal
cd /d "%~dp0"
set "PYTHONPATH=%CD%"

if exist ".venv\Scripts\python.exe" (
    set "PYEXE=.venv\Scripts\python.exe"
) else (
    echo [Hinweis] Keine virtuelle Umgebung gefunden - es wird das System-Python verwendet.
    set "PYEXE=python.exe"
)

"%PYEXE%" -m cmkoffline2mail %*
exit /b %ERRORLEVEL%
