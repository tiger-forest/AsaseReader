rem Launch the Asase Reader GUI (浅濑阅读器)
rem Requires Python 3.11+ on PATH, or set ASASE_PYTHON to your interpreter.
rem A local .venv\Scripts\python.exe is used automatically when present.
cd /d "%~dp0"
set PYTHONPATH=src
if defined ASASE_PYTHON (
  "%ASASE_PYTHON%" -m asase_reader --gui
) else (
  if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -m asase_reader --gui
  ) else (
    python -m asase_reader --gui
  )
)
