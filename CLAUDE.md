# CLAUDE.md

Notes for Claude working in this repo.

## What this is

A single-file Python tool (`tabcount.py`) that polls Safari, Chrome, Firefox,
and DuckDuckGo on macOS for window/tab counts and writes one CSV row per
browser per poll. A LaunchAgent runs `tabcount sample` every 5 minutes.

## Layout

- `tabcount.py` — the entire tool (sampler, plotter, install/uninstall)
- `com.cohm.tabcount.plist.template` — LaunchAgent plist with `{{PYTHON}}`, `{{SCRIPT}}`, `{{LOG}}` placeholders
- Runtime state lives **outside** the repo at `~/.tabcount/`:
  - `~/.tabcount/data/YYYY-MM.csv[.gz]` — the history
  - `~/.tabcount/venv/` — Python venv with `lz4` and `matplotlib`
  - `~/.tabcount/tabcount.log` — launchd stderr
- LaunchAgent: `~/Library/LaunchAgents/com.cohm.tabcount.plist`

The split keeps the repo free of data and lets the install be reproducible.

`tabcount.py` re-execs itself under `~/.tabcount/venv/bin/python` on every
run (see `_maybe_reexec_under_venv`), so any invocation — direct `./tabcount.py`,
shell alias, or `launchd` — uses the venv's interpreter automatically once
`install` has run. Nothing is added to `$PATH`.

## Per-browser polling — how and why

- **Safari, Chrome, DuckDuckGo:** AppleScript via `osascript`. We
  enumerate `windows` and ask each window for `count of tabs`. We never
  launch the app — `pgrep -x <ProcessName>` gates the call.
- **Firefox:** parse `sessionstore.jsonlz4` (Mozilla's `mozLz40\0` header
  + standard LZ4 block) directly. Firefox has no useful AppleScript for
  tabs. Stale files (>1h old) when Firefox isn't running are treated as
  `not_running` rather than reported as current data.
- **DuckDuckGo nuance:** its scripting dictionary support is unknown ahead
  of time. We try the full AppleScript first; if that fails we fall back to
  System Events UI scripting which counts windows but cannot see tabs. In
  that fallback the row records `windows` only and `tabs_per_window` is
  empty.

## TCC / automation permissions

First-time AppleScript calls trigger TCC prompts. These need to fire from
an interactive session — run `tabcount status` from Terminal once after
install. Without that, launchd-spawned samples will record `error` and
errors will accumulate in `~/.tabcount/tabcount.log`.

## Testing changes without polluting live data

Two safe approaches:

1. Run `./tabcount.py status` — gathers samples but writes nothing.
2. Run sample/plot against a scratch data dir: temporarily edit
   `DATA_DIR = ...` at the top of `tabcount.py` to point at `/tmp/tc-test/`
   and revert before committing.

To pause the scheduled job during a debugging session:
`launchctl bootout gui/$UID/com.cohm.tabcount` (re-enable with
`launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.cohm.tabcount.plist`).

## Storage format

CSV with header `ts,browser,status,windows,total_tabs,tabs_per_window`.
`status` is one of `ok | not_running | error`. `tabs_per_window` is
`|`-separated; empty when status ≠ ok or when only an aggregate count was
available. One CSV per calendar month; previous months auto-gzip on the
first sample of the new month.

## Things that should stay simple

- Single file — don't split `tabcount.py` into a package. The whole script
  is small on purpose.
- No `requirements.txt` — deps (`lz4`, `matplotlib`) are installed by the
  `install` subcommand, which is the only entry point users go through.
- Don't add features beyond sample/status/plot/install/uninstall without a
  clear ask. This is a personal tracker, not a framework.
