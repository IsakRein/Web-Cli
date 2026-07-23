#!/usr/bin/env bash
# Chrome Debugger launcher: exec-replaces this process with Chrome so it stays a
# single app, launched with remote debugging on the relocated debug profile.
exec arch -arm64 "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9222 \
  --user-data-dir="$HOME/Library/Application Support/Google/Chrome-Debug" \
  "$@"
