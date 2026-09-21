@echo off
setlocal EnableExtensions
rem ===========================================================================
rem  Pantograph launcher (Windows)
rem
rem  Starts the app. On the first run it fetches a private copy of uv, which
rem  then fetches Python and the libraries; after that it runs offline.
rem
rem  Everything it downloads lives under %LOCALAPPDATA%\Pantograph\runtime\ and
rem  belongs to this app alone: nothing is added to PATH, no system Python is
rem  touched, and uninstalling is deleting folders.
rem
rem  Arguments are passed through to the app (--port, --open FILE, ...).
rem ===========================================================================

rem -- Where the app is. Always from this file's own location: a double-clicked
rem -- launcher can start with any working directory at all.
for %%I in ("%~dp0..") do set "ROOT=%%~fI"

set "RUNTIME=%LOCALAPPDATA%\Pantograph\runtime"
set "UV_DIR=%RUNTIME%\uv"
set "UV=%UV_DIR%\uv.exe"
set "LOGDIR=%LOCALAPPDATA%\Pantograph\Logs"

rem -- The version of uv this release was tested against. Pinned on purpose:
rem -- an unattended upgrade is a way for a working install to stop working.
set "UV_VERSION=0.12.17"

rem ---------------------------------------------------------------------------
rem  Running from inside the zip viewer (G-7). Windows unpacks it to a temp
rem  folder, so the app appears to run and then can't write anything.
rem ---------------------------------------------------------------------------
echo "%ROOT%" | findstr /i /c:"\\Temp\\" >nul && goto :in_zip
echo "%ROOT%" | findstr /i /c:".zip\\" >nul && goto :in_zip

if not exist "%ROOT%\listen_to_idraw.py" (
  echo.
  echo   Pantograph is missing its own files.
  echo.
  echo   This launcher expects to sit in the PantographApp folder, inside the
  echo   folder it was extracted to. Looked in:
  echo     %ROOT%
  echo.
  goto :held
)

rem ---------------------------------------------------------------------------
rem  Keep every downloaded thing inside the runtime folder. Without this, uv
rem  uses profile folders that OneDrive syncs -- gigabytes uploaded, and file
rem  locks that break builds.
rem ---------------------------------------------------------------------------
set "UV_CACHE_DIR=%RUNTIME%\cache"
set "UV_PYTHON_INSTALL_DIR=%RUNTIME%\python"
set "UV_PROJECT_ENVIRONMENT=%RUNTIME%\venv"
set "UV_PYTHON_PREFERENCE=only-managed"

rem ---------------------------------------------------------------------------
rem  Fetch uv if it isn't here yet.
rem ---------------------------------------------------------------------------
if exist "%UV%" goto :have_uv

if /i "%PROCESSOR_ARCHITECTURE%"=="ARM64" (set "UV_ARCH=aarch64") else (set "UV_ARCH=x86_64")
set "UV_ASSET=uv-%UV_ARCH%-pc-windows-msvc.zip"
set "UV_URL=https://github.com/astral-sh/uv/releases/download/%UV_VERSION%/%UV_ASSET%"

echo.
echo   First launch: setting up. This downloads Python and the libraries
echo   (about 150 MB) and happens once. Later launches start straight away.
echo.
echo   Getting uv %UV_VERSION% ...

if not exist "%UV_DIR%" mkdir "%UV_DIR%" 2>nul
rem -- curl and tar both ship with Windows 10 and 11. We use them rather than
rem -- uv's own install script: that one edits PATH, and it is the shape of
rem -- thing antivirus and IT policy block.
rem --
rem -- Called by full path on purpose. Git for Windows puts its own curl and a
rem -- GNU tar on PATH, and GNU tar cannot read a zip at all -- it reads the
rem -- "C:" in the path as a remote host and fails with "resolve failed".
set "SYSCURL=%SystemRoot%\System32\curl.exe"
set "SYSTAR=%SystemRoot%\System32\tar.exe"
if not exist "%SYSCURL%" set "SYSCURL=curl"

"%SYSCURL%" -fsSL --retry 3 -o "%UV_DIR%\uv.zip" "%UV_URL%"
if errorlevel 1 goto :no_download

if exist "%SYSTAR%" (
  "%SYSTAR%" -xf "%UV_DIR%\uv.zip" -C "%UV_DIR%"
) else (
  rem -- Windows 10 before build 17063 has no tar; PowerShell can unpack a zip.
  powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "Expand-Archive -LiteralPath '%UV_DIR%\uv.zip' -DestinationPath '%UV_DIR%' -Force"
)
if errorlevel 1 goto :no_unpack
del "%UV_DIR%\uv.zip" 2>nul

rem -- Some builds unpack into a subfolder named after the asset.
if not exist "%UV%" if exist "%UV_DIR%\%UV_ASSET:~0,-4%\uv.exe" (
  move /y "%UV_DIR%\%UV_ASSET:~0,-4%\uv.exe" "%UV_DIR%\" >nul
)
if not exist "%UV%" goto :no_unpack

:have_uv

rem ---------------------------------------------------------------------------
rem  Run it.
rem    --locked  the libraries are exactly what this release was built against;
rem              it fails rather than quietly resolving something else.
rem    --no-dev  skip pytest and playwright. They're for developing Pantograph,
rem              and playwright alone is a 37 MB download.
rem ---------------------------------------------------------------------------
"%UV%" run --locked --no-dev --project "%ROOT%" python "%ROOT%\listen_to_idraw.py" %*
set "RC=%ERRORLEVEL%"

rem -- 0 is a clean quit. Ctrl+C gives 130 (and asks "Terminate batch job?",
rem -- which is harmless). Anything else is worth showing.
if "%RC%"=="0" goto :eof
if "%RC%"=="130" goto :eof

echo.
echo   Pantograph stopped with an error (code %RC%).
echo.
echo   The log is at:
echo     %LOGDIR%\pantograph.log
echo.
goto :held

rem ---------------------------------------------------------------------------
:in_zip
echo.
echo   Pantograph is running from inside the zip file.
echo.
echo   Windows opens a zip as if it were a folder, but the app can't install
echo   itself there. Right-click the zip, choose "Extract All...", and run
echo   this launcher from the extracted folder.
echo.
goto :held

:no_download
echo.
echo   Couldn't download uv.
echo.
echo   Pantograph needs the internet once, to set itself up. Check the
echo   connection and try again. On a school or work machine the download may
echo   be blocked; a home network or a phone hotspot is the quickest test.
echo.
echo   It was fetching:
echo     %UV_URL%
echo.
goto :held

:no_unpack
echo.
echo   Downloaded uv, but couldn't unpack it.
echo.
echo   Delete this folder and run the launcher again:
echo     %UV_DIR%
echo.
goto :held

:held
echo   Press any key to close.
pause >nul
exit /b 1
