# build.ps1 — convenience wrapper that uses the user-scope .NET 7 SDK.
#
# The .NET 7 SDK was sideloaded to %USERPROFILE%\.dotnet\ when winget
# was blocked. The system PATH may resolve a different `dotnet.exe`
# that only has the runtime (no `build` command), so we resolve the
# SDK explicitly here.
#
# Usage:
#   .\build.ps1            (Release config — default)
#   .\build.ps1 -Debug     (Debug config — for IDE breakpoints)

[CmdletBinding()]
param(
    [switch]$Debug,
    [switch]$Clean
)

$ErrorActionPreference = "Stop"

# Locate the user-scope SDK first; fall back to system if it has SDKs.
$userDotnet = Join-Path $env:USERPROFILE ".dotnet\dotnet.exe"
if (Test-Path $userDotnet) {
    $dotnet = $userDotnet
} else {
    $dotnet = (Get-Command dotnet.exe -ErrorAction SilentlyContinue).Source
    if (-not $dotnet) {
        Write-Error "No .NET SDK found. Install at https://aka.ms/dotnet/download or sideload via dotnet-install.ps1."
        exit 1
    }
}

$config = if ($Debug) { "Debug" } else { "Release" }
Write-Host "→ dotnet build -c $config (SDK at $dotnet)" -ForegroundColor Cyan

if ($Clean) {
    & $dotnet clean -c $config
}

# `& $dotnet build -c $config` — note the call operator since $dotnet
# may contain spaces in some install paths.
& $dotnet build -c $config

if ($LASTEXITCODE -eq 0) {
    Write-Host "✓ Build succeeded." -ForegroundColor Green
    Write-Host "  Plug-in installed at: $env:LOCALAPPDATA\AriaPanel\Rhino8\AriaPanel.rhp" -ForegroundColor Green
} else {
    Write-Host "✗ Build failed (exit $LASTEXITCODE)." -ForegroundColor Red
    exit $LASTEXITCODE
}
