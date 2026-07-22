@echo off
setlocal
set "PYTHON=C:\Users\ivy\AppData\Local\Programs\Python\Python312\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"
"%PYTHON%" -B "%~dp0variant_bridge.py"
pause
