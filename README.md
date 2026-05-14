# tabcount

A tiny macOS tool that polls **Safari**, **Google Chrome**, **Firefox**, and the
**DuckDuckGo** browser every 5 minutes, records how many windows and tabs each
has open (including per-window tab counts), and lets you view the history as
interactive curves.

## Why

To answer: "Am I actually closing tabs, or has my tab debt been growing for
months?" — across all the browsers you use, in one place, with negligible
overhead.

## Resource footprint

- **Idle** (between samples): zero. The sampler is not a daemon; it exits
  after each run. The LaunchAgent definition adds essentially nothing.
- **Sampling** (every 5 min, ~1 second): ~100–300 ms of single-core CPU,
  ~30 MB RAM peak — both released on exit. Disk I/O is a single ~1 MB
  Firefox file read plus a ~250-byte CSV append.
- **Plotting** (`tabcount plot`, on demand): a few hundred MB and 1–3 s
  while matplotlib loads, then idle until you close the window.

## How it polls

| Browser | Method |
|---|---|
| Safari, Chrome, DuckDuckGo | AppleScript via `osascript` (no app launch — only queries running browsers) |
| Firefox | Read `sessionstore.jsonlz4` from the profile directly (`mozLz40` + LZ4 block) |

The sampler **never launches a browser**. If a browser isn't running, the row
is recorded as `not_running`.

## Install

Requires Python 3 (the macOS-shipped `/usr/bin/python3` is fine).

```sh
cd /Users/cohm/Documents/Work/Computing/Projects/tabcounter
./tabcount.py install
```

This creates a venv at `~/.tabcount/venv/` with `lz4` and `matplotlib` and
writes the LaunchAgent plist to `~/Library/LaunchAgents/com.cohm.tabcount.plist`.
Nothing is added to `$PATH`.

The script re-execs itself under the venv python on every run, so invoking
`./tabcount.py …` from any python works once `install` has completed.

### Optional: a shell alias

To call it `tabcount` from anywhere without changing `$PATH`, add this line
to `~/.zshrc`:

```sh
alias tabcount='/Users/cohm/Documents/Work/Computing/Projects/tabcounter/tabcount.py'
```

### First-run permission grants

The first time the sampler queries Safari, Chrome, or DuckDuckGo, macOS will
prompt to allow automation. Run it from a Terminal once so the prompts appear
visibly and you can click *OK*:

```sh
./tabcount.py status
```

After that the scheduled samples will inherit the grant.

## Usage

(Examples assume the `tabcount` alias; otherwise substitute the full path.)

```sh
tabcount status                          # print current counts, no write
tabcount sample                          # poll once and append to today's CSV
tabcount plot                            # interactive window, last 30 days
tabcount plot --range 7d --metric tabs
tabcount plot --range 24h --browsers safari,chrome
tabcount uninstall                       # remove LaunchAgent (keeps data)
```

## Data

CSV under `~/.tabcount/data/YYYY-MM.csv`, one file per calendar month,
gzipped after the month rolls over.

```
ts,browser,status,windows,total_tabs,tabs_per_window
2026-05-14T10:05:00+02:00,safari,ok,3,17,8|6|3
2026-05-14T10:05:00+02:00,chrome,ok,1,4,4
2026-05-14T10:05:00+02:00,firefox,not_running,,,
2026-05-14T10:05:00+02:00,duckduckgo,ok,2,9,5|4
```

`tabs_per_window` preserves the per-window breakdown so you can reconstruct
the full distribution; `windows`/`total_tabs` are kept as explicit columns
for grep/awk convenience.

Expect ≈22 MB/year raw, ≈2–3 MB/year after gzip rollover.

## Troubleshooting

- **Empty CSV after install** — the launchd-spawned process may not have
  automation permission. Run `tabcount status` from Terminal, click *OK* on
  prompts. Check `~/.tabcount/tabcount.log` for errors.
- **Firefox always `not_running`** — the script ignores stale
  `sessionstore.jsonlz4` files older than an hour when Firefox isn't running.
- **DuckDuckGo shows `0 tabs in N window(s)`** — DDG's AppleScript surface
  doesn't always expose tabs; the script falls back to UI scripting which
  only gives a window count. The data still records windows correctly.

## License

MIT
