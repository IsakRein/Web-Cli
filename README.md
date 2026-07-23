# Chrome Debugger - always-on CDP against your daily-driver Chrome

A setup that makes your everyday Chrome permanently reachable over the Chrome
DevTools Protocol (CDP) on `127.0.0.1:9222`, so any local tool can read and
drive it using your real profile and logins.

You launch Chrome through a small **Chrome Debugger.app** instead of the normal
Chrome icon. It looks and behaves like Chrome, but always adds the debug flags.
This repo holds what you need to rebuild and keep that app running:
`launcher.sh` (the app's executable) and this doc.

## Why this is needed (the Chrome 136+ gotcha)

Since **Chrome 136** (May 2025), Chrome **silently ignores
`--remote-debugging-port` when it runs against the default user-data-dir.** The
flag is accepted on the command line but no debug socket is ever opened. This
was verified empirically on Chrome 150/152: even passing `--user-data-dir`
pointed at the *real default path* still opens no port - Chrome compares the
resolved dir against the default and refuses.

The fix is mandatory: run against a **relocated, non-default profile
directory**. Here that dir is:

    ~/Library/Application Support/Google/Chrome-Debug

It was created as a one-time copy of the real profile
(`~/Library/Application Support/Google/Chrome`), so all logins, extensions,
bookmarks, and history carried over. Chrome Sync keeps it current with the
Google account. The original default profile is left untouched as a frozen
backup.

## Components

- **/Applications/Chrome Debugger.app** - a minimal app bundle whose executable
  is a bash script that `exec`s Chrome (`launcher.sh`). Because it uses `exec`,
  the app process is *replaced by* Chrome, so it stays a single app - no extra
  launcher process hanging around. It is not a wrapper of Chrome.app, so
  Chrome's code signature stays intact (no Keychain breakage).

### What the app runs (`launcher.sh`)

    exec arch -arm64 "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
      --remote-debugging-port=9222 \
      --user-data-dir="$HOME/Library/Application Support/Google/Chrome-Debug" \
      "$@"

`arch -arm64` forces the native arm64 binary - a bash-launched Chrome can
otherwise wrongly select the x86_64 slice and run under Rosetta.

## Build the app from scratch

```sh
# 1. Relocate the profile (quit Chrome first for a clean copy)
osascript -e 'quit app "Google Chrome"'
ditto "$HOME/Library/Application Support/Google/Chrome" \
      "$HOME/Library/Application Support/Google/Chrome-Debug"

# 2. Compile the AppleScript launcher into an app bundle
osacompile -o "/Applications/Chrome Debugger.app" ChromeDebugger.applescript

# 3. Make it registerable as a browser: add a bundle id + http/https handlers
P="/Applications/Chrome Debugger.app/Contents/Info.plist"
PB=/usr/libexec/PlistBuddy
$PB -c "Add :CFBundleIdentifier string com.isak.chrome-debugger" "$P"
$PB -c "Add :CFBundleURLTypes array" "$P"
$PB -c "Add :CFBundleURLTypes:0 dict" "$P"
$PB -c "Add :CFBundleURLTypes:0:CFBundleURLName string 'Web browser'" "$P"
$PB -c "Add :CFBundleURLTypes:0:CFBundleTypeRole string Viewer" "$P"
$PB -c "Add :CFBundleURLTypes:0:CFBundleURLSchemes array" "$P"
$PB -c "Add :CFBundleURLTypes:0:CFBundleURLSchemes:0 string http" "$P"
$PB -c "Add :CFBundleURLTypes:0:CFBundleURLSchemes:1 string https" "$P"

# 4. Give it Chrome's icon, re-sign (editing invalidated the signature), register
cp "/Applications/Google Chrome.app/Contents/Resources/app.icns" \
   "/Applications/Chrome Debugger.app/Contents/Resources/droplet.icns"
codesign --force -s - "/Applications/Chrome Debugger.app"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
   -f "/Applications/Chrome Debugger.app"
```

## Make it the only Chrome you use

Two steps are gated by macOS and must be done by hand (one click each):

1. **Default browser:** System Settings > Desktop & Dock > Default web browser
   -> choose **Chrome Debugger**. Links from other apps then open in the debug
   profile.
2. **Dock:** drag Google Chrome out of the Dock, drag Chrome Debugger in.

To push the normal Chrome further out of sight (reversible):

```sh
chflags hidden "/Applications/Google Chrome.app"   # hide from Finder
chflags nohidden "/Applications/Google Chrome.app" # undo
```

The app still launches Chrome fine while hidden - it execs the binary by full
path, which works regardless of the hidden flag. Chrome auto-updates normally;
the binary path stays constant across updates.

## Verify it's reachable

```sh
curl http://127.0.0.1:9222/json/version   # browser + protocol version
curl http://127.0.0.1:9222/json           # list all targets (tabs)
```

Any CDP client can then connect over `127.0.0.1:9222`.

## Troubleshooting

- **Port 9222 not responding:** make sure Chrome was launched via Chrome
  Debugger.app (not the normal icon), and that only one instance is running -
  two Chrome instances can't both bind 9222.
- **No page target / scripts error:** the only open tab may be a `chrome://`
  page (profile picker, new tab). Open a normal page first.
- **Wrong profile / missing logins:** confirm the running process shows
  `--user-data-dir=.../Chrome-Debug` (`ps aux | grep user-data-dir`). If it
  shows the default dir, you launched the wrong Chrome.
- **First launch is blocked by Gatekeeper:** the app is locally built and
  ad-hoc signed - right-click > Open once to approve.

## Security note

CDP on `127.0.0.1:9222` grants full read/control over this Chrome, including any
site you are logged into and its auth cookies. It is loopback-only (not exposed
to the network), but any local process can connect. Be deliberate about what you
have tools pull from authenticated sessions.
