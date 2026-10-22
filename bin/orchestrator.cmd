@echo off
rem Runs the orchestrator from the repo's mkenv virtual environment without activating it.
setlocal
for %%i in ("%~dp0..") do set "root=%%~fi"
for /f "delims=" %%h in ('hostname') do set "host=%%h"
set "env_dir=%root%\python-env-%host%"
if exist "%env_dir%\Scripts\python.exe" goto run
echo No virtual environment at "%env_dir%"; run: python mkenv.py 1>&2
exit /b 1
:run
if defined PYTHONPATH (set "PYTHONPATH=%root%;%PYTHONPATH%") else (set "PYTHONPATH=%root%")
rem without UTF-8 mode, files and pipes use the Windows code page
set "PYTHONUTF8=1"
"%env_dir%\Scripts\python.exe" -m orchestrator %*
exit /b %ERRORLEVEL%
