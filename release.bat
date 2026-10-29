@echo off
rem ============================================================
rem  AgentHub one-shot release. Run after editing source:
rem    build exe (+copy to Desktop) -> deploy MCP -> refresh bootstrap
rem    -> release consistency check -> agents status
rem  NOTE: keep this file ASCII-only. Non-ASCII on an EXECUTED line
rem  breaks under cmd's OEM codepage (garbled -> run as command).
rem ============================================================
cd /d "%~dp0"

echo [1/5] build exe + copy to Desktop  (build.bat, ~1-2 min)
call build.bat

echo [2/5] deploy MCP server to ~/.agenthub/mcp_server/
D:\python311\python.exe cli.py deploy

echo [3/5] refresh agents' bootstrap to current version
D:\python311\python.exe cli.py refresh

echo [4/5] release consistency check (exe fresh? desktop in sync?)
D:\python311\python.exe cli.py release

echo [5/5] agents status
D:\python311\python.exe cli.py status

echo.
echo Done.
if not defined AGENTHUB_NOPAUSE pause