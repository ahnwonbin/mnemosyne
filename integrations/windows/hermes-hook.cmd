@echo off
rem Hermes Second-Brain Hook Wrapper for Windows
set PYTHONPATH=%~dp0..\..;%PYTHONPATH%
python -m bridge.cli hook %1 %2
exit /b 0
