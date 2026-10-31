@echo off
chcp 65001 >nul
rem AgentHub 打包脚本：产出 dist\AgentHub.exe（约49MB），并复制到桌面
rem 排除清单是实测瘦身的关键：分析期会把 site-packages 无关全家桶收进 PYZ，
rem 不排除则 exe 达 361MB；排除后 49MB。新增依赖若运行时报缺模块，从清单摘掉对应项。
rem ⚠ win32print 绝不能排除：qframelesswindow（qfluentwidgets 依赖）运行时必需，
rem   误排会导致启动崩（且 windowed 模式进程仍存活，需用 MainWindowTitle 验证）。
cd /d "%~dp0"
rem ⚠ 禁用 UPX（--noupx）：UPX 压缩 Qt 官方 DLL 会破坏图形管线——实测表现为
rem   窗口首次启动不渲染内容（全透明）、文字模糊发虚（2026-10-09 用户报「接入页字看不清」）。
rem 构建清单（R3）：start 必须在 PyInstaller 之前——mtime 判不出「构建中段改的源码」，
rem 只有构建前快照的源码哈希才能证明 exe 冻结的是哪一版。
echo [1/4] 构建清单 start（快照源码哈希）...
D:\python311\python.exe cli.py build-manifest start
if errorlevel 1 (
  echo 构建清单 start 失败
  if not defined AGENTHUB_NOPAUSE pause
  exit /b 1
)
rem 随包运行时文件：MCP 服务端 + 钩子脚本。exe 的 __file__ 在临时解包目录，
rem 不 --add-data 进去，桌面版的部署服务端/安装钩子会复制 0 个文件而静默失效。
echo [1.5/4] 打包运行时文件到 build\bundle...
D:\python311\python.exe cli.py bundle
if errorlevel 1 (
  echo 运行时文件打包失败
  if not defined AGENTHUB_NOPAUSE pause
  exit /b 1
)
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
  main.py
if errorlevel 1 (
  echo 打包失败
  if not defined AGENTHUB_NOPAUSE pause
  exit /b 1
)
echo [2/4] 构建清单 finish（记录 exe sha256）...
D:\python311\python.exe cli.py build-manifest finish
if errorlevel 1 (
  echo 构建清单 finish 失败
  if not defined AGENTHUB_NOPAUSE pause
  exit /b 1
)
copy /y dist\AgentHub.exe "%USERPROFILE%\Desktop\AgentHub.exe"
if errorlevel 1 (
  echo [ABORT] copy to Desktop failed - close Desktop AgentHub.exe if locked or readonly, rerun
  if not defined AGENTHUB_NOPAUSE pause
  exit /b 1
)
echo [3/4] 发布一致性检查（清单/源码/桌面副本哈希）...
D:\python311\python.exe cli.py release
if errorlevel 1 (
  echo 发布检查未通过：exe 与源码或桌面副本不一致
  if not defined AGENTHUB_NOPAUSE pause
  exit /b 1
)
echo [4/4] 完成：桌面 AgentHub.exe
if not defined AGENTHUB_NOPAUSE pause