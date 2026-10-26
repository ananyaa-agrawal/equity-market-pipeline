#!/usr/bin/env bash
# run_daily.sh - the morning job. cron runs it at 08:30 IST, Monday to Friday,
# before the NSE opens at 09:15.
#
# What it does, in order:
#   1. goes to the project folder and activates the virtual environment
#   2. takes a lock, so two runs can never overlap
#   3. exits straight away at weekends (exchange holidays are handled in Python,
#      which knows the real trading calendar)
#   4. runs the daily pipeline, appending everything it prints to logs/cron.log
#   5. on failure, writes a clear FAILED line and shows a desktop notification
#
# Usage:
#   ./run_daily.sh               normal run, for today
#   ./run_daily.sh 2026-10-26    dry run, as if it were the morning of that date
#
# Exit code: 0 = success (or nothing to do at the weekend), anything else = failure.

# -e: stop at the first failing command, -u: treat unset variables as errors,
# -o pipefail: a pipeline fails if any command in it fails.
set -euo pipefail

# cron starts jobs with a minimal PATH. Add Homebrew's folders (where flock lives on a Mac).
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

# 1. The project folder is wherever this script lives, whichever folder cron starts us in.
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"
mkdir -p logs
LOG="$PROJECT_DIR/logs/cron.log"
RUN_DATE="${1:-$(date +%Y-%m-%d)}"

timestamp() { date '+%Y-%m-%d %H:%M:%S'; }

notify() {  # best effort: a missing or failing notifier must never hide the real error
    local message="$1"
    if command -v notify-send > /dev/null; then          # Linux desktops
        notify-send "Market pipeline FAILED" "$message" || true
    elif command -v osascript > /dev/null; then          # macOS
        osascript -e "display notification \"$message\" with title \"Market pipeline FAILED\"" || true
    fi
}

fail() {  # write the FAILED line, notify, and exit with the given code
    local code="$1" reason="$2"
    echo "$(timestamp) FAILED run date $RUN_DATE, exit code $code: $reason" >> "$LOG"
    notify "Run for $RUN_DATE failed (exit code $code). See logs/cron.log"
    exit "$code"
}

# shellcheck disable=SC1091
source .venv/bin/activate

# 2. The lock. File descriptor 9 is opened on a lock file; `flock -n` either takes
#    the lock immediately or fails if another run is holding it. The lock is
#    released automatically when this script exits, even if it crashes.
command -v flock > /dev/null || fail 1 "flock not found (on a Mac: brew install flock)"
exec 9> "$PROJECT_DIR/logs/run_daily.lock"
if ! flock -n 9; then
    fail 1 "another run is still in progress, so this one did not start"
fi

# 3. Weekends. %u is the day of the week: 1 = Monday ... 7 = Sunday.
#    Linux's `date` reads a date with -d; the macOS version needs -j -f instead.
day_of_week=$(date -d "$RUN_DATE" +%u 2> /dev/null || date -j -f %Y-%m-%d "$RUN_DATE" +%u 2> /dev/null) \
    || fail 1 "not a valid YYYY-MM-DD date: $RUN_DATE"
if [ "$day_of_week" -ge 6 ]; then
    echo "$(timestamp) SKIPPED run date $RUN_DATE: weekend, the market is closed" >> "$LOG"
    exit 0
fi

# 4. The run itself. "|| status=$?" records a failure instead of letting set -e
#    end the script before we can report it.
echo "$(timestamp) START run date $RUN_DATE" >> "$LOG"
status=0
python -m pipeline.main daily --date "$RUN_DATE" >> "$LOG" 2>&1 || status=$?

# 5. The verdict.
if [ "$status" -ne 0 ]; then
    fail "$status" "see the lines above in logs/cron.log, or logs/pipeline.log"
fi
echo "$(timestamp) SUCCESS run date $RUN_DATE, report: reports/$RUN_DATE.html" >> "$LOG"
