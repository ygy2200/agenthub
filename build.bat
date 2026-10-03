@echo off
chcp 65001 >nul
rem AgentHub 打包脚本：产出 dist\AgentHub.exe（约49MB），并复制到桌面
rem 排除清单是实测瘦身的关键：分析期会把 site-packages 无关全家桶收进 PYZ，
rem 不排除则 exe 达 361MB；排除后 49MB。新增依赖若运行时报缺模块，从清单摘掉对应项。
rem ⚠ win32print 绝不能排除：qframelesswindow（qfluentwidgets 依赖）运行时必需，
rem   误排会导致启动崩（且 windowed 模式进程仍存活，需用 MainWindowTitle 验证）。
cd /d "%~dp0"
D:\python311\python.exe -m PyInstaller --noconfirm --windowed --onefile --name AgentHub ^
  --icon assets\agenthub.ico --add-data "assets\agenthub.ico;." ^
  --exclude-module aiohttp --exclude-module aiohappyeyeballs --exclude-module aiosignal --exclude-module yarl --exclude-module multidict --exclude-module frozenlist --exclude-module propcache ^
  --exclude-module PIL --exclude-module numpy --exclude-module pandas --exclude-module matplotlib --exclude-module scipy --exclude-module sklearn ^
  --exclude-module sphinx --exclude-module alabaster --exclude-module babel --exclude-module snowballstemmer --exclude-module sphinxcontrib --exclude-module docutils ^
  --exclude-module tkinter --exclude-module _tkinter --exclude-module tcl ^
  --exclude-module setuptools --exclude-module pkg_resources --exclude-module pip --exclude-module wheel ^
  --exclude-module test --exclude-module tests --exclude-module cryptography --exclude-module cffi --exclude-module _cffi_backend ^
  --exclude-module ujson --exclude-module tzdata --exclude-module mypyc ^
  --exclude-module pythonwin --exclude-module win32trace --exclude-module win32pdh --exclude-module win32evtlog ^
  --exclude-module torch --exclude-module tensorflow --exclude-module jupyter --exclude-module IPython --exclude-module notebook ^
  main.py
if errorlevel 1 (
  echo 打包失败
  pause
  exit /b 1
)
copy /y dist\AgentHub.exe "%USERPROFILE%\Desktop\AgentHub.exe"
echo 完成：桌面 AgentHub.exe
pause
