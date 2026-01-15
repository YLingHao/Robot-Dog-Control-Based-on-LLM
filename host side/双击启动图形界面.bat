@echo off
REM 使用 Anaconda 中的 pytorch 虚拟环境启动 GUI
REM 如路径有变化，请修改下面这一行 python.exe 的路径
set "PYTORCH_PYTHON=E:\Anaconda3\python.exe"

chcp 65001 >nul
echo 使用环境: %PYTORCH_PYTHON%
echo 正在启动机器人 LLM 图形界面...
echo.

"%PYTORCH_PYTHON%" "%~dp0llm_forwarder_gui.py"

echo.
pause