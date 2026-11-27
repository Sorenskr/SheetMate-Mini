@echo off
chcp 65001 >nul
title 暗号表助手·迷你版 (SheetMate-Mini)
cd /d "%~dp0"
python -c "import playwright, openpyxl, PIL, uiautomation" 2>nul
if errorlevel 1 (
  echo 正在安装必要依赖，请稍候...
  pip install -r requirements.txt
  python -m playwright install chromium
)
start "" pythonw auto_report.py
