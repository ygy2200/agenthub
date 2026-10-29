@echo off
rem ============================================================
rem  AgentHub one-shot release. Run after editing source:
rem    build exe (+copy to Desktop) -> deploy MCP -> refresh bootstrap
rem    -> release consistency check -> agents status
rem  R720: every step's exit code is checked. Before this, a failed
rem  build still ended with EXIT=0 (the trailing `pause` swallowed it),
rem  so CI/automation believed "released" while the desktop exe was stale.
rem  NOTE: keep this file ASCII-only. Non-ASCII on an EXECUTED line
rem  breaks under cmd's OEM codepage (garbled -> run as command).
rem ============================================================
cd /d "%~dp0"

echo [1/5] build exe + copy to Desktop  (build.bat, ~1-2 min)
call build.bat
if errorlevel 1 (
  echo [ABORT] build failed - release stopped, desktop exe NOT refreshed
  exit /b 1
)

echo [2/5] deploy MCP server to ~/.agenthub/mcp_server/
D:\python311\python.exe cli.py deploy
if errorlevel 1 (
  echo [ABORT] deploy failed - release stopped
  exit /b 1
)

echo [3/5] refresh agents' bootstrap to current version
D:\python311\python.exe cli.py refresh
if errorlevel 1 (
  echo [ABORT] refresh failed - release stopped
  exit /b 1
)

echo [4/5] release consistency check (exe fresh? desktop in sync?)
D:\python311\python.exe cli.py release
if errorlevel 1 (
  echo [ABORT] exe is stale or desktop copy differs - release stopped
  exit /b 1
)

echo [5/5] agents status  (informational - onboarding gaps do not fail release)
rem nofail: status only reports onboarding gaps; it must not fail the release (R721)
D:\python311\python.exe cli.py status

echo.
echo Done.
if not defined AGENTHUB_NOPAUSE pause
exit /b 0
