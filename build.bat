@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ===== 1/3 安装依赖 =====
if not exist .venv (
    python -m venv .venv
)
call .venv\Scripts\pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
if errorlevel 1 goto :err
echo ===== 2/3 打包 exe =====
call .venv\Scripts\pyinstaller --noconfirm --clean --onefile --windowed --name Arxiver ^
    --icon "assets\arxiver.ico" ^
    --add-data "arxiver\ui\static;arxiver\ui\static" ^
    --add-data "assets;assets" ^
    --hidden-import pystray._win32 ^
    --hidden-import webview.platforms.edgechromium ^
    --hidden-import clr ^
    --collect-all webview ^
    --collect-all winotify ^
    --collect-all certifi ^
    --collect-all apscheduler ^
    --collect-all tzlocal ^
    run.py
if errorlevel 1 goto :err
echo ===== 3/3 完成 =====
echo 产物: dist\Arxiver.exe
pause
exit /b 0
:err
echo 打包失败，请查看上方错误信息
pause
exit /b 1
