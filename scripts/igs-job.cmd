@echo off
rem Run one igs command for Windows Task Scheduler and append its output to
rem logs\COMMAND.log in the repository, e.g.
rem   scripts\igs-job.cmd daily
rem   scripts\igs-job.cmd sources verify
rem Settings come from the repository's .env file (read by igs itself).
setlocal
cd /d "%~dp0.."
if not exist logs mkdir logs
rem uv installs itself to %USERPROFILE%\.local\bin; make sure a scheduled task finds it.
where uv >nul 2>nul || set "PATH=%USERPROFILE%\.local\bin;%PATH%"
rem UTF-8 for output redirected to the log file (company names, filing text).
set PYTHONUTF8=1
echo === %DATE% %TIME% igs %* >> "logs\%1.log"
uv run --all-groups igs %* >> "logs\%1.log" 2>&1
exit /b %ERRORLEVEL%
