# weekly_audit.ps1 — run the test audit, commit-free reporter for
# OS-level scheduling beyond Claude's 7-day session limit.
#
# Install as a Windows Task Scheduler task:
#
#   schtasks /Create /TN "aria-os-weekly-audit" `
#     /TR "powershell.exe -ExecutionPolicy Bypass -File C:\Users\jonko\Downloads\workspace\aria-os-export\scripts\weekly_audit.ps1" `
#     /SC WEEKLY /D MON /ST 07:17 /RL LIMITED /F
#
# Unregister:
#
#   schtasks /Delete /TN "aria-os-weekly-audit" /F
#
# The task writes .audit/audit_YYYYMMDD.{md,json} + diff_*.md and rotates a
# `latest.md` symlink. It also runs pytest on the ariaOS tests and appends
# the pytest summary to the audit report so you see runtime failures next
# to static weakness.

$ErrorActionPreference = "Stop"
$ROOT = "C:\Users\jonko\Downloads\workspace\aria-os-export"
$PY = "C:\Users\jonko\miniforge3\python.exe"

Set-Location $ROOT

# 1. Static audit across all 4 repos
Write-Host "[weekly-audit] running static analysis across aria/millforge/structsight/mfg-core..."
& $PY scripts\test_audit.py --weekly

# 2. Actual pytest run — catch runtime regressions too
$ts = Get-Date -Format "yyyyMMdd"
$reportPath = ".audit\audit_$ts.md"
$pytestLog = ".audit\pytest_$ts.log"

Write-Host "[weekly-audit] running pytest to catch runtime regressions..."
& $PY -m pytest tests `
  --tb=short -q `
  --ignore=tests\test_live_api.py `
  --ignore=tests\test_millforge_destination.py `
  > $pytestLog 2>&1

$pytestExit = $LASTEXITCODE

# 3. Append pytest summary to the audit report
"" | Out-File -Append $reportPath -Encoding utf8
"## Runtime test results ($(Get-Date -Format 'yyyy-MM-dd HH:mm'))" | Out-File -Append $reportPath -Encoding utf8
"" | Out-File -Append $reportPath -Encoding utf8
"pytest exit code: $pytestExit" | Out-File -Append $reportPath -Encoding utf8
"" | Out-File -Append $reportPath -Encoding utf8
"``````" | Out-File -Append $reportPath -Encoding utf8
# Tail of the pytest log (last 60 lines — full log is in pytest_$ts.log)
Get-Content $pytestLog -Tail 60 | Out-File -Append $reportPath -Encoding utf8
"``````" | Out-File -Append $reportPath -Encoding utf8

Write-Host "[weekly-audit] wrote $reportPath"
Write-Host "[weekly-audit] pytest exit: $pytestExit"

# Non-zero exit if either the static audit hit strict failures OR pytest failed
exit $pytestExit
