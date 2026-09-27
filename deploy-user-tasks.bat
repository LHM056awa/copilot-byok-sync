@echo off
rem ============================================================
rem  Double-click wrapper for deploy_user_tasks.py
rem
rem  Merges the workspace .vscode\tasks.json into the user-level
rem  VS Code tasks.json:
rem    - same label  -> updated in place
rem    - new label   -> appended
rem    - other user tasks / fields -> preserved
rem
rem  All logic lives in deploy_user_tasks.py; this file is only
rem  a launcher so it can be double-clicked.
rem
rem  Usage: double-click, or:  deploy-user-tasks.bat
rem ============================================================
setlocal

set "PY=%~dp0deploy_user_tasks.py"

if not exist "%PY%" (
    echo [ERROR] script not found: %PY%
    echo This .bat must sit next to deploy_user_tasks.py in the repo root.
    goto :end
)

python "%PY%"
set "RC=%ERRORLEVEL%"
if %RC% neq 0 echo [ERROR] deploy_user_tasks.py exited with code %RC%

:end
endlocal
pause
exit /b 0
