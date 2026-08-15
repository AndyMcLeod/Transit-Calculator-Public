@echo off
REM Transit Calculator — start the local server; it opens the browser itself.
REM
REM THE BROWSER IS **NOT** OPENED HERE. An earlier version of this file ran
REM   start "" http://127.0.0.1:8078/
REM before launching python, which is a race the browser always wins: it connects
REM before the socket exists, shows "can't reach this page", and the operator sees a
REM failure for a server that comes up a second later. server.py now opens the
REM browser from a timer AFTER it is listening. Pass --no-browser to suppress it.
cd /d "%~dp0"
python server.py %*
if errorlevel 1 (
  echo.
  echo   The server exited with an error. Read the message above.
  pause
)
