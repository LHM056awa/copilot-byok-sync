@echo off
rem ============================================================
rem  Auto-sync the GLOBAL chatLanguageModels.json
rem  Equivalent to the VS Code "Sync Custom Endpoints" task
rem  (.vscode/tasks.json):  python -m clm_sync.cli --config <global> --all
rem
rem  - Syncs every vendor=customendpoint provider's model list
rem  - Auto-fetches account balance for known vendors (shown in the report)
rem  - Deletes stale models (this task runs WITHOUT --no-delete)
rem
rem  Double-click to run, or:  sync-global-models.bat
rem ============================================================
setlocal
set "CONFIG=%APPDATA%\Code\User\chatLanguageModels.json"

echo === clm-sync: global model list ===
echo Config : %CONFIG%
echo Time   : %date% %time%
echo.

python -m clm_sync.cli --config "%CONFIG%" --all
set "RC=%ERRORLEVEL%"

echo.
echo === done (exit code %RC%) ===
rem exit code: 0 = all ok, 1 = one or more endpoints failed
pause
endlocal
