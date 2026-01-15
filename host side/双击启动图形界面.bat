@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8

echo 正在启动机器狗 LLM 监听转发程序 (图形界面)...

python llm_forwarder_gui.py

pause
