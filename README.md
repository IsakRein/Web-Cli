# Web-Cli / Chrome Debugger

Retired repo. The one thing that came out of this work — an always-on
**Chrome Debugger.app** that keeps the daily-driver Chrome reachable over the
Chrome DevTools Protocol on `127.0.0.1:9222` — is now built and maintained
entirely by the dotfiles bootstrap.

See the `Chrome Debugger.app` section (2.5) of `~/.dotfiles/bootstrap.sh`. It
builds the app bundle, seeds the relocated `Chrome-Debug` profile, hides the
normal Chrome, and is idempotent (re-running repairs the app). Nothing
operational lives here anymore.
