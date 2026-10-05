@echo off
setlocal
set "HERE=%~dp0"
if not exist "%HERE%.venv\Scripts\python.exe" (
  echo eval\.venv not found. Follow the install steps in eval\README.md. 1>&2
  exit /b 1
)
set PYTHONIOENCODING=utf-8
"%HERE%.venv\Scripts\python.exe" "%HERE%cli.py" %*
exit /b %ERRORLEVEL%
