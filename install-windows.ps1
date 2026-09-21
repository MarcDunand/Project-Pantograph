<#
.SYNOPSIS
  Install Pantograph on Windows. Run it again later to update.

.DESCRIPTION
  Downloads the latest release, unpacks it to %LOCALAPPDATA%\Pantograph\app\,
  and makes Start-menu and Desktop shortcuts to the launcher.

  No admin rights, nothing added to PATH, nothing installed system-wide.
  Drawings, settings and the downloaded Python live outside the app folder, so
  re-running this to update never touches them.

  Usual way to run it:
    powershell -ExecutionPolicy Bypass -c "irm https://github.com/MarcDunand/Project-Pantograph/releases/latest/download/install-windows.ps1 | iex"

.PARAMETER Source
  A release zip to install instead of the latest one: a URL, or a path to a
  local file. Used for testing a build before it is released.

.PARAMETER NoLaunch
  Install, but don't start Pantograph afterwards.
#>
[CmdletBinding()]
param(
    [string] $Source,
    [switch] $NoLaunch
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# The repository the release comes from.
$Repo       = 'MarcDunand/Project-Pantograph'
$AppRoot    = Join-Path $env:LOCALAPPDATA 'Pantograph'
$InstallDir = Join-Path $AppRoot 'app'
$Launcher   = 'PantographApp\Pantograph.bat'

function Say  ($m) { Write-Host "  $m" }
function Step ($m) { Write-Host ""; Write-Host "  $m" -ForegroundColor Cyan }
function Die  ($m) { Write-Host ""; Write-Host "  $m" -ForegroundColor Red; Write-Host ""; exit 1 }

Write-Host ""
Write-Host "  Pantograph" -ForegroundColor White
Write-Host "  Draw on an iPad, plot live on an AxiDraw."

# ── get the release zip ──────────────────────────────────────────────────────
$tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("pantograph-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $tmp -Force | Out-Null
$zip = Join-Path $tmp 'pantograph.zip'

try {
    if ($Source -and (Test-Path -LiteralPath $Source)) {
        Step "Using $Source"
        Copy-Item -LiteralPath $Source -Destination $zip -Force
    } else {
        $url = if ($Source) { $Source }
               else { "https://github.com/$Repo/releases/latest/download/pantograph-windows.zip" }
        Step "Downloading Pantograph..."
        try {
            Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
        } catch {
            Die @"
Couldn't download Pantograph.

  Check the internet connection and try again. On a school or work
  machine the download may be blocked -- if so, download the zip in a
  browser instead and extract it yourself.

  It was fetching:
    $url
"@
        }
    }

    # ── unpack ───────────────────────────────────────────────────────────────
    Step "Installing to $InstallDir"
    $unpacked = Join-Path $tmp 'unpacked'
    Expand-Archive -LiteralPath $zip -DestinationPath $unpacked -Force

    # A GitHub zip has one folder inside it; a hand-made one may not.
    $inner = @(Get-ChildItem -LiteralPath $unpacked)
    $newApp = if ($inner.Count -eq 1 -and $inner[0].PSIsContainer) { $inner[0].FullName } else { $unpacked }

    if (-not (Test-Path (Join-Path $newApp 'listen_to_idraw.py'))) {
        Die "That zip doesn't look like Pantograph -- listen_to_idraw.py isn't in it."
    }

    # Replacing the folder wholesale is what makes re-running this an update.
    # Only the app is replaced: drawings, settings and the downloaded Python
    # live elsewhere under $AppRoot and are left alone.
    if (Test-Path -LiteralPath $InstallDir) {
        try {
            Remove-Item -LiteralPath $InstallDir -Recurse -Force
        } catch {
            Die @"
Couldn't replace the previous version.

  Pantograph is probably still running. Quit it and run this again.

  $InstallDir
"@
        }
    }
    New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
    Copy-Item -Path (Join-Path $newApp '*') -Destination $InstallDir -Recurse -Force
} finally {
    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
}

$launcherPath = Join-Path $InstallDir $Launcher
if (-not (Test-Path -LiteralPath $launcherPath)) {
    Die "The launcher is missing from the release: $Launcher"
}

# ── shortcuts ────────────────────────────────────────────────────────────────
Step "Making shortcuts"
$icon = Join-Path $InstallDir 'PantographApp\ui\icon.ico'
$shell = New-Object -ComObject WScript.Shell

function New-Shortcut($path) {
    $s = $shell.CreateShortcut($path)
    $s.TargetPath       = $launcherPath
    $s.WorkingDirectory = $InstallDir
    $s.Description      = 'Draw on an iPad, plot live on an AxiDraw.'
    if (Test-Path -LiteralPath $icon) { $s.IconLocation = $icon }
    $s.Save()
}

$startMenu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
try {
    New-Shortcut (Join-Path $startMenu 'Pantograph.lnk')
    Say "Start menu"
} catch { Say "Couldn't make a Start-menu shortcut (not fatal)." }

try {
    New-Shortcut (Join-Path ([Environment]::GetFolderPath('Desktop')) 'Pantograph.lnk')
    Say "Desktop"
} catch { Say "Couldn't make a Desktop shortcut (not fatal)." }

# ── done ─────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "  Installed." -ForegroundColor Green
Say "Pantograph is in your Start menu and on your Desktop."
Say ""
Say "The first launch downloads Python and the libraries (about 150 MB)."
Say "That happens once; after it, Pantograph starts straight away and"
Say "works offline."
Write-Host ""

if (-not $NoLaunch) {
    Say "Starting Pantograph..."
    Start-Process -FilePath $launcherPath -WorkingDirectory $InstallDir
}
