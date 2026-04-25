# start-onshape-stack.ps1 -- launch all four processes for the
# Onshape Custom App in separate PowerShell windows so each is
# independently observable + killable.
#
# Window layout (each runs in its own window so you can watch logs):
#   1. ARIA backend          : python dashboard\aria_server.py    (port 8000)
#   2. Onshape app server    : python scripts\onshape_app_server.py (port 8765)
#   3. Frontend dev server   : npm run dev                        (port 5173)
#   4. Cloudflared tunnel    : cloudflared --url http://localhost:8765
#
# ASCII-only by design (PowerShell 5.1 reads .ps1 as cp1252 -- unicode
# glyphs break parsing). Same lesson as cad-plugins\rhino\AriaPanel\build.ps1.
#
# Usage:
#   .\scripts\start-onshape-stack.ps1
#   .\scripts\start-onshape-stack.ps1 -SkipFrontend  (if Vite already running)
#   .\scripts\start-onshape-stack.ps1 -SkipTunnel    (Mode A: local only)

[CmdletBinding()]
param(
    [switch]$SkipFrontend,
    [switch]$SkipTunnel
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $env:USERPROFILE "miniforge3\python.exe"
if (-not (Test-Path $python)) {
    Write-Host "ERR Python not found at $python" -ForegroundColor Red
    Write-Host "    Edit this script with the correct python.exe path."
    exit 1
}
$cloudflared = Join-Path $env:USERPROFILE "bin\cloudflared.exe"

function Start-InNewWindow {
    param([string]$Title, [string]$WorkDir, [string]$Cmd)
    $ps = "Set-Location -LiteralPath '$WorkDir'; " +
          "`$Host.UI.RawUI.WindowTitle = '$Title'; " +
          "Write-Host '>> $Title' -ForegroundColor Cyan; " +
          $Cmd
    Start-Process powershell -ArgumentList "-NoExit", "-Command", $ps
}

Write-Host ">> Launching ARIA Onshape stack..." -ForegroundColor Cyan
Write-Host "   Repo: $repoRoot"

# 1. ARIA backend (planner -- emits native_op events on /api/generate)
Start-InNewWindow -Title "ARIA backend (8000)" -WorkDir $repoRoot `
    -Cmd "& '$python' dashboard\aria_server.py"

Start-Sleep -Seconds 1

# 2. Onshape app server (proxy + bridge -- port 8765)
Start-InNewWindow -Title "Onshape app server (8765)" -WorkDir $repoRoot `
    -Cmd "& '$python' scripts\onshape_app_server.py"

# 3. Frontend dev server (Vite -- port 5173)
if (-not $SkipFrontend) {
    Start-InNewWindow -Title "Frontend dev server (5173)" `
        -WorkDir (Join-Path $repoRoot "frontend") `
        -Cmd "npm run dev"
} else {
    Write-Host "   (skipping frontend dev server -- assume already running)" -ForegroundColor DarkGray
}

# 4. Cloudflared tunnel
if (-not $SkipTunnel) {
    if (Test-Path $cloudflared) {
        Start-Sleep -Seconds 2  # give the app server a head start
        Start-InNewWindow -Title "cloudflared tunnel" -WorkDir $repoRoot `
            -Cmd "& '$cloudflared' tunnel --url http://localhost:8765"
        Write-Host ""
        Write-Host "   IMPORTANT: cloudflared rotates the URL on every restart." -ForegroundColor Yellow
        Write-Host "   Watch its window for the new https://....trycloudflare.com URL," -ForegroundColor Yellow
        Write-Host "   then update the Action URL in dev-portal.onshape.com -> your" -ForegroundColor Yellow
        Write-Host "   ARIA Generate app -> Extensions -> Element tab." -ForegroundColor Yellow
    } else {
        Write-Host "   WARNING cloudflared not at $cloudflared -- skipping tunnel" -ForegroundColor Yellow
    }
} else {
    Write-Host "   (skipping cloudflared -- Mode A: open http://127.0.0.1:8765/panel)" -ForegroundColor DarkGray
}

Write-Host ""
Write-Host "OK Stack launching. Four windows opening now." -ForegroundColor Green
Write-Host ""
Write-Host "   Mode A (browser tab next to Onshape):" -ForegroundColor White
Write-Host "     http://127.0.0.1:8765/panel"
Write-Host ""
Write-Host "   Mode B (Onshape tab via cloudflared):"
Write-Host "     watch the cloudflared window for the trycloudflare.com URL"
Write-Host "     update Action URL in dev portal -- then reload Onshape doc"
Write-Host ""
Write-Host "   Stop everything: close the four PS windows, or:"
Write-Host "     Get-Process python,node,cloudflared | Stop-Process"
