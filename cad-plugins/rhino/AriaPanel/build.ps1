# build.ps1 - convenience wrapper that uses the user-scope .NET 7 SDK.
#
# The .NET 7 SDK was sideloaded to %USERPROFILE%\.dotnet\ when winget
# was blocked. The system PATH may resolve a different `dotnet.exe`
# that only has the runtime (no `build` command), so we resolve the
# SDK explicitly here.
#
# ASCII-only by design: PowerShell 5.1 reads .ps1 files as cp1252
# and unicode glyphs (checkmarks, arrows) break string parsing.
#
# Usage:
#   .\build.ps1            (Release config -- default)
#   .\build.ps1 -Debug     (Debug config -- for IDE breakpoints)
#   .\build.ps1 -Clean     (clean before build)

[CmdletBinding()]
param(
    [Alias("d")]
    [switch]$DebugConfig,
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
        Write-Error "No .NET SDK found. Install at https://aka.ms/dotnet/download"
        exit 1
    }
}

$config = if ($DebugConfig) { "Debug" } else { "Release" }
Write-Host ">> dotnet build -c $config (SDK at $dotnet)" -ForegroundColor Cyan

if ($Clean) {
    & $dotnet clean -c $config
}

& $dotnet build -c $config

if ($LASTEXITCODE -eq 0) {
    Write-Host "OK Build succeeded." -ForegroundColor Green
    Write-Host "   Plug-in installed at: $env:LOCALAPPDATA\AriaPanel\Rhino8\AriaPanel.rhp" -ForegroundColor Green
} else {
    Write-Host "ERR Build failed (exit $LASTEXITCODE)." -ForegroundColor Red
    exit $LASTEXITCODE
}
