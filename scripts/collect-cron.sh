#!/usr/bin/env bash
# Runs one collector cycle (snapshot → ingest → item sync → prune). Meant for cron:
#   5 * * * * /path/to/wow-forever-gold/scripts/collect-cron.sh
# Output is appended to data/collect.log. A lock skips a run if the previous one is still going.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"
mkdir -p data

# cron runs with a minimal PATH; make sure uv is found.
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

exec 9>data/collect.lock
if ! flock -n 9; then
    echo "$(date -Is) previous collect still running, skipping" >> data/collect.log
    exit 0
fi

status=0
{
    echo "=== $(date -Is) collect --once ==="
    uv run --frozen wowgold collect --once || status=$?
    echo "=== $(date -Is) finished (exit $status) ==="
} >> data/collect.log 2>&1
exit "$status"
