@echo off
rem AI Chrome: one dedicated browser profile shared by the Gemini MCP transport
rem and the chrome-devtools MCP. Idempotent - exits if the debug port is up.
setlocal
set "PROF=%LOCALAPPDATA%\gemini-mcp-profile"
set "PORT=9223"

netstat -ano | findstr ":%PORT% " | findstr LISTENING >nul 2>&1
if %errorlevel%==0 exit /b 0

start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" ^
  --user-data-dir="%PROF%" ^
  --remote-debugging-port=%PORT% ^
  --no-first-run ^
  --no-default-browser-check ^
  --disable-background-timer-throttling ^
  --disable-renderer-backgrounding ^
  --disable-backgrounding-occluded-windows ^
  about:blank
exit /b 0
