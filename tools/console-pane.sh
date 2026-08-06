#!/usr/bin/env bash
# Open the read-only Meridian console in a second pane, next to Claude Code.
#
# The copilot can run this for the player instead of making them open a terminal
# by hand. It only ever launches `meridian.py console`, which never advances the
# clock, so a stray pane can't hurt the voyage.
#
# Exits non-zero (with a printable reason on stdout) when there's no terminal we
# know how to split — the caller should fall back to telling the player to open a
# second window themselves.

set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cmd="cd $(printf '%q' "$repo") && exec python3 engine/meridian.py console"

if [[ -n "${TMUX:-}" ]]; then
  tmux split-window -h -c "$repo" "python3 engine/meridian.py console"
  tmux select-pane -l
  echo "opened: tmux pane (Ctrl-B x to close)"
  exit 0
fi

case "${TERM_PROGRAM:-}" in
  iTerm.app)
    osascript - "$cmd" >/dev/null <<'APPLESCRIPT'
      on run argv
        tell application "iTerm2"
          tell current session of current window
            set pane to (split vertically with default profile)
          end tell
          tell pane to write text (item 1 of argv)
        end tell
      end run
APPLESCRIPT
    echo "opened: iTerm2 pane to the right (Ctrl-C then Cmd-W to close)"
    exit 0
    ;;
  Apple_Terminal)
    # Terminal.app has no panes; a tab is the closest thing.
    osascript - "$cmd" >/dev/null <<'APPLESCRIPT'
      on run argv
        tell application "Terminal"
          do script (item 1 of argv)
          activate
        end tell
      end run
APPLESCRIPT
    echo "opened: Terminal.app window (Ctrl-C then Cmd-W to close)"
    exit 0
    ;;
esac

echo "no splittable terminal detected (TERM_PROGRAM=${TERM_PROGRAM:-unset})"
exit 1
