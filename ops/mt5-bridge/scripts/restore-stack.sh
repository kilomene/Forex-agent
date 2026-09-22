#!/usr/bin/env bash
# restore-stack.sh -- one-command recovery of the Nova MT5 trading stack
# after a platform container restart (or any wipe).
#
# Safe to run at any time: every step is idempotent. On a healthy machine
# this is a no-op that prints a health summary.
#
# Steps:
#   1. best-effort repo sync: git fetch origin + reset --hard origin/main on
#      ~/workspace/forex-migration/repo (via the github-forex deploy-key
#      alias; falls back to plain https; warns and continues on failure)
#   2. run the unified self-healing supervisor (wine -> Xvfb -> MT5 terminal
#      -> feed -> daemons). It only STARTS missing components; the live
#      trade_executor.py is never killed or restarted.
#   3. print a concise health summary.
#
# Stdlib-only: bash + git + python3 + ssh. No network writes beyond fetch.
set -u

REPO_DIR="$HOME/workspace/forex-migration/repo"
SUPERVISOR="$HOME/workspace/mt5/supervise_stack.py"
KEY="$HOME/.ssh/forex_deploy"

echo "=== [1/3] repo sync (best-effort) ==="
if [ ! -f "$KEY" ]; then
    echo "WARN: deploy key $KEY absent -- skipping repo sync, continuing."
elif [ ! -d "$REPO_DIR/.git" ]; then
    echo "WARN: $REPO_DIR is not a git clone -- skipping repo sync, continuing."
else
    SYNCED=0
    # Prefer the deploy-key SSH alias; rewrite only for this one command.
    if timeout 90 git -c url."git@github-forex:".insteadOf="https://github.com/" \
            -C "$REPO_DIR" fetch origin 2>/tmp/restore-git-ssh.err; then
        SYNCED=1
        echo "fetched via github-forex deploy key."
    else
        echo "WARN: ssh fetch via github-forex failed ($(head -c 120 /tmp/restore-git-ssh.err)); trying plain https."
        if timeout 90 git -C "$REPO_DIR" fetch origin 2>/tmp/restore-git-https.err; then
            SYNCED=1
            echo "fetched via https."
        else
            echo "WARN: repo fetch failed ($(head -c 120 /tmp/restore-git-https.err)) -- continuing with local checkout."
        fi
    fi
    if [ "$SYNCED" = 1 ]; then
        if git -C "$REPO_DIR" reset --hard origin/main -q; then
            echo "repo at $(git -C "$REPO_DIR" rev-parse --short HEAD) (origin/main)."
        else
            echo "WARN: git reset --hard failed -- continuing."
        fi
    fi
fi

echo
echo "=== [2/3] stack supervisor ==="
python3 "$SUPERVISOR"
SUP_RC=$?
if [ "$SUP_RC" -eq 2 ]; then
    echo "HARD FAILURE from supervisor (exit 2) -- human attention needed."
elif [ "$SUP_RC" -ne 0 ]; then
    echo "WARN: supervisor exited $SUP_RC."
fi

echo
echo "=== [3/3] health summary ==="
python3 "$SUPERVISOR" --health

echo
if [ "$SUP_RC" -eq 0 ]; then
    echo "restore-stack: OK (stack healthy or healed)."
else
    echo "restore-stack: ATTENTION NEEDED (supervisor exit $SUP_RC)."
fi
exit "$SUP_RC"
