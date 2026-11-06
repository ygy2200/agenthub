@echo off
chcp 65001 >nul
rem AgentHub build script: produces dist\AgentHub.exe (~49MB) and copies it to Desktop.
rem NOTE: keep this file ASCII-only. cmd reads batch files by byte blocks under its
rem OEM codepage, so non-ASCII on an EXECUTED line (including rem/echo) gets split
rem and the tail is run as a command. R903: 5 fake "is not recognized" lines showed
rem up in the R901 release log; echo also does NOT reset ERRORLEVEL, which can bias
rem the following `if errorlevel 1` gates. Guard: tests/unit/test_build_bat_ascii_r903.py
rem Exclude list is the key to size: Analysis pulls whole site-packages into PYZ;
rem without it the exe is 361MB, with it 49MB. If a runtime import fails, drop that
rem module from the list.
rem Never exclude win32print: qframelesswindow (qfluentwidgets dep) needs it at
rem runtime; excluding it crashes startup while the windowed process stays alive,
rem so verify with MainWindowTitle instead of process existence.
cd /d "%~dp0"
rem Never use UPX (--noupx): it breaks Qt DLLs - first launch renders nothing
rem (fully transparent) and text looks blurry (user report 2026-10-09).
rem Build manifest (R3): start must run BEFORE PyInstaller. mtime cannot detect a
rem source file edited mid-build; only a pre-build source hash proves what the exe froze.
echo [1/4] build manifest start (snapshot source hashes)...
D:\python311\python.exe cli.py build-manifest start
if errorlevel 1 (
  echo build manifest start failed
  if not defined AGENTHUB_NOPAUSE pause
  exit /b 1
)
rem Bundled runtime files: MCP server + hook scripts. Inside the exe __file__ points
rem to a temp unpack dir; without --add-data the desktop deploy/install would copy
rem 0 files and fail silently.
echo [1.5/4] bundling runtime files into build\bundle...
D:\python311\python.exe cli.py bundle
if errorlevel 1 (
  echo runtime bundle failed
  if not defined AGENTHUB_NOPAUSE pause
  exit /b 1
)
rem R861: the ONNX encoder is NOT bundled - numpy is excluded and weights are not
rem packed; shipping it would only add 14MB.
D:\python311\python.exe -m PyInstaller --noconfirm --windowed --onefile --noupx --name AgentHub ^
  --icon assets\agenthub.ico --add-data "assets\agenthub.ico;." --add-data "build\bundle;." ^
  --exclude-module aiohttp --exclude-module aiohappyeyeballs --exclude-module aiosignal --exclude-module yarl --exclude-module multidict --exclude-module frozenlist --exclude-module propcache ^
  --exclude-module PIL --exclude-module numpy --exclude-module pandas --exclude-module matplotlib --exclude-module scipy --exclude-module sklearn ^
  --exclude-module sphinx --exclude-module alabaster --exclude-module babel --exclude-module snowballstemmer --exclude-module sphinxcontrib --exclude-module docutils ^
  --exclude-module tkinter --exclude-module _tkinter --exclude-module tcl ^
  --exclude-module setuptools --exclude-module pkg_resources --exclude-module pip --exclude-module wheel ^
  --exclude-module test --exclude-module tests --exclude-module cryptography --exclude-module cffi --exclude-module _cffi_backend ^
  --exclude-module ujson --exclude-module tzdata --exclude-module mypyc ^
  --exclude-module pythonwin --exclude-module win32trace --exclude-module win32pdh --exclude-module win32evtlog ^
  --exclude-module torch --exclude-module tensorflow --exclude-module jupyter --exclude-module IPython --exclude-module notebook ^
  --exclude-module torchvision --exclude-module torchaudio --exclude-module cv2 --exclude-module hf_xet ^
  --exclude-module onnxruntime --exclude-module tokenizers --exclude-module sentence_transformers ^
  main.py
if errorlevel 1 (
  echo packaging failed
  if not defined AGENTHUB_NOPAUSE pause
  exit /b 1
)
echo [2/4] build manifest finish (record exe sha256)...
D:\python311\python.exe cli.py build-manifest finish
if errorlevel 1 (
  echo build manifest finish failed
  if not defined AGENTHUB_NOPAUSE pause
  exit /b 1
)
copy /y dist\AgentHub.exe "%USERPROFILE%\Desktop\AgentHub.exe"
if errorlevel 1 (
  echo [ABORT] copy to Desktop failed - close Desktop AgentHub.exe if locked or readonly, rerun
  if not defined AGENTHUB_NOPAUSE pause
  exit /b 1
)
echo [3/4] release consistency check (manifest / source / desktop hash)...
D:\python311\python.exe cli.py release
if errorlevel 1 (
  echo release check failed: exe differs from source or desktop copy
  if not defined AGENTHUB_NOPAUSE pause
  exit /b 1
)
echo [4/4] done: Desktop AgentHub.exe
if not defined AGENTHUB_NOPAUSE pause
rem B057: explicit zero - without it the exit code is whatever the last real
rem command left behind (release.bat gates `call build.bat` on errorlevel)
exit /b 0
