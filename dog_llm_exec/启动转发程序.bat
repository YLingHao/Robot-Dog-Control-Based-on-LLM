@echo off
chcp 65001 >nul
echo ========================================
echo 大模型输出转发程序
echo ========================================
echo.

REM 检查Python是否安装
python --version >nul 2>&1
if errorlevel 1 (
    echo 错误: 未找到Python，请先安装Python
    pause
    exit /b 1
)

REM 检查requests库
python -c "import requests" >nul 2>&1
if errorlevel 1 (
    echo 正在安装依赖库...
    pip install requests
    if errorlevel 1 (
        echo 错误: 无法安装requests库
        pause
        exit /b 1
    )
)

REM 设置默认参数
set DOG_IP=192.168.1.100
set DOG_USER=root

REM 允许用户输入IP
set /p DOG_IP="请输入机器狗IP地址 (默认: %DOG_IP%): "
if "%DOG_IP%"=="" set DOG_IP=192.168.1.100

REM 允许用户输入用户名
set /p DOG_USER="请输入SSH用户名 (默认: %DOG_USER%): "
if "%DOG_USER%"=="" set DOG_USER=root

echo.
echo 正在启动转发程序...
echo 机器狗IP: %DOG_IP%
echo SSH用户: %DOG_USER%
echo.
echo 提示: 按 Ctrl+C 停止程序
echo ========================================
echo.

python llm_forwarder.py --dog-ip %DOG_IP% --dog-user %DOG_USER%

pause
