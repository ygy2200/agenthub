@echo off
rem ============================================================
rem  AgentHub one-shot release. Run after editing source:
rem    build exe (+copy to Desktop) -> deploy MCP -> refresh bootstrap
rem    -> release consistency check -> publish GitHub Release + push
rem    -> agents status
rem  R720: every step's exit code is checked. Before this, a failed
rem  build still ended with EXIT=0 (the trailing `pause` swallowed it),
rem  so CI/automation believed "released" while the desktop exe was stale.
rem  R755: publish.py writes all output to a log file as well, because a
rem  piped tail/head eats the real exit code. R759: that log is always
rem  produced here, and the log path is printed on failure.
rem  R759: publish --push pushes BEFORE creating the Release - GitHub
rem  pins a new tag to the main it sees at creation time, so publishing
rem  first leaves the tag one commit behind the shipped exe.
rem  NOTE: keep this file ASCII-only. Non-ASCII on an EXECUTED line
rem  breaks under cmd's OEM codepage (garbled -> run as command).
rem ============================================================
cd /d "%~dp0"
set "PUBLOG=output\publish-last.log"

echo [1/6] build exe + copy to Desktop  (build.bat, ~1-2 min)
call build.bat
if errorlevel 1 (
  echo [ABORT] build failed - release stopped, desktop exe NOT refreshed
  exit /b 1
)

echo [2/6] deploy MCP server to ~/.agenthub/mcp_server/
D:\python311\python.exe cli.py deploy
if errorlevel 1 (
  echo [ABORT] deploy failed - release stopped
  exit /b 1
)

echo [3/6] refresh agents' bootstrap to current version
D:\python311\python.exe cli.py refresh
if errorlevel 1 (
  echo [ABORT] refresh failed - release stopped
  exit /b 1
)

echo [4/6] release consistency check (exe fresh? desktop in sync?)
D:\python311\python.exe cli.py release
if errorlevel 1 (
  echo [ABORT] exe is stale or desktop copy differs - release stopped
  exit /b 1
)

echo [5/6] publish GitHub Release + push  (log: %PUBLOG%)
if not exist output mkdir output
D:\python311\python.exe publish.py --yes --push --log-file "%PUBLOG%"
if errorlevel 1 (
  echo [ABORT] publish failed - GitHub state may be a DRAFT (invisible).
  echo         full output: %PUBLOG%
  echo         recover upload only: D:\python311\python.exe publish.py --upload-only --yes
  exit /b 1
)

echo [6/6] agents status  (informational - onboarding gaps do not fail release)
rem nofail: status only reports onboarding gaps; it must not fail the release (R721)
D:\python311\python.exe cli.py status

echo.
echo Done. Log: %PUBLOG%
if not defined AGENTHUB_NOPAUSE pause
exit /b 0
