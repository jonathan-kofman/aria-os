#!/usr/bin/env bash
# weekly_audit.sh — cross-platform equivalent of weekly_audit.ps1 for
# macOS / Linux dev boxes. Install via crontab:
#
#   crontab -e
#   # aria-os weekly test audit — Mondays at 07:17
#   17 7 * * 1 bash /path/to/aria-os-export/scripts/weekly_audit.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")"/.. && pwd)"
PY="${PY:-python3}"
cd "$ROOT"

echo "[weekly-audit] running static analysis across 4 repos..."
"$PY" scripts/test_audit.py --weekly

ts=$(date +%Y%m%d)
report=".audit/audit_${ts}.md"
pytest_log=".audit/pytest_${ts}.log"

echo "[weekly-audit] running pytest to catch runtime regressions..."
set +e
"$PY" -m pytest tests \
  --tb=short -q \
  --ignore=tests/test_live_api.py \
  > "$pytest_log" 2>&1
pytest_exit=$?
set -e

{
  echo ""
  echo "## Runtime test results ($(date -u '+%Y-%m-%d %H:%MZ'))"
  echo ""
  echo "pytest exit code: $pytest_exit"
  echo ""
  echo '```'
  tail -60 "$pytest_log"
  echo '```'
} >> "$report"

echo "[weekly-audit] wrote $report"
echo "[weekly-audit] pytest exit: $pytest_exit"
exit "$pytest_exit"
