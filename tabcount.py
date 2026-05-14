#!/usr/bin/env python3
"""tabcount — poll Safari, Chrome, Firefox, DuckDuckGo for window/tab counts
and track over time. Runs as a launchd-scheduled sampler; plot subcommand
renders an interactive matplotlib window.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOME = Path.home()
REPO_DIR = Path(__file__).resolve().parent

RUNTIME_DIR = HOME / ".tabcount"
DATA_DIR = RUNTIME_DIR / "data"
VENV_DIR = RUNTIME_DIR / "venv"
LOG_PATH = RUNTIME_DIR / "tabcount.log"

PLIST_PATH = HOME / "Library" / "LaunchAgents" / "com.cohm.tabcount.plist"
PLIST_TEMPLATE = REPO_DIR / "com.cohm.tabcount.plist.template"
PLIST_LABEL = "com.cohm.tabcount"

BIN_PATH = HOME / "bin" / "tabcount"

BROWSERS = ["safari", "chrome", "firefox", "duckduckgo"]
CSV_HEADER = ["ts", "browser", "status", "windows", "total_tabs", "tabs_per_window"]

# ProcessName as it appears in `ps`/pgrep -x.
PROCESS_NAMES = {
    "safari": "Safari",
    "chrome": "Google Chrome",
    "firefox": "firefox",
    "duckduckgo": "DuckDuckGo",
}

# AppleScript application names.
APP_NAMES = {
    "safari": "Safari",
    "chrome": "Google Chrome",
    "duckduckgo": "DuckDuckGo",
}

BROWSER_COLORS = {
    "safari": "#1a73e8",
    "chrome": "#fbbc04",
    "firefox": "#ff7139",
    "duckduckgo": "#de5833",
}


# ---- process detection ----------------------------------------------------

def is_running(process_name: str) -> bool:
    try:
        r = subprocess.run(
            ["pgrep", "-x", process_name],
            capture_output=True, text=True, timeout=3,
        )
        return r.returncode == 0
    except Exception:
        return False


# ---- AppleScript helpers --------------------------------------------------

def run_osascript(script: str, timeout: float = 8.0) -> tuple[bool, str, str]:
    """Returns (ok, stdout, stderr_or_error)."""
    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, "", "timeout"
    except Exception as e:
        return False, "", str(e)
    return (r.returncode == 0), r.stdout.strip(), r.stderr.strip()


def applescript_tab_counts(app_name: str) -> tuple[bool, list[int], str]:
    """Ask a browser via AppleScript for tab counts per window.
    Returns (ok, [tabs_per_window], error_message)."""
    script = f'''
    tell application "{app_name}"
        set counts to {{}}
        repeat with w in windows
            try
                set end of counts to (count of tabs of w) as text
            on error
                set end of counts to "0"
            end try
        end repeat
        set AppleScript's text item delimiters to "|"
        return counts as text
    end tell
    '''
    ok, out, err = run_osascript(script)
    if not ok:
        return False, [], err
    if not out:
        return True, [], ""
    try:
        return True, [int(x) for x in out.split("|") if x != ""], ""
    except ValueError:
        return False, [], f"parse: {out!r}"


def applescript_window_count_fallback(process_name: str) -> tuple[bool, int, str]:
    """UI-scripting fallback: count windows of a running process via System Events."""
    script = f'''
    tell application "System Events"
        if exists process "{process_name}" then
            tell process "{process_name}"
                return (count of windows) as text
            end tell
        else
            return "0"
        end if
    end tell
    '''
    ok, out, err = run_osascript(script)
    if not ok:
        return False, 0, err
    try:
        return True, int(out), ""
    except ValueError:
        return False, 0, f"parse: {out!r}"


# ---- per-browser samplers -------------------------------------------------

def _result_ok(per_window: list[int]) -> dict:
    return {
        "status": "ok",
        "windows": len(per_window),
        "total_tabs": sum(per_window),
        "tabs_per_window": per_window,
    }


def sample_via_applescript(browser: str) -> dict:
    if not is_running(PROCESS_NAMES[browser]):
        return {"status": "not_running"}
    ok, counts, err = applescript_tab_counts(APP_NAMES[browser])
    if ok:
        return _result_ok(counts)
    return {"status": "error", "error": err}


def sample_safari() -> dict:
    return sample_via_applescript("safari")


def sample_chrome() -> dict:
    return sample_via_applescript("chrome")


def sample_duckduckgo() -> dict:
    if not is_running(PROCESS_NAMES["duckduckgo"]):
        return {"status": "not_running"}
    ok, counts, err = applescript_tab_counts(APP_NAMES["duckduckgo"])
    if ok:
        return _result_ok(counts)
    # AppleScript dictionary may not expose tabs — try UI fallback for at
    # least a window count. tabs_per_window stays empty in this case.
    ok2, nwin, err2 = applescript_window_count_fallback(PROCESS_NAMES["duckduckgo"])
    if ok2 and nwin > 0:
        return {
            "status": "ok",
            "windows": nwin,
            "total_tabs": 0,
            "tabs_per_window": [],
        }
    return {"status": "error", "error": err or err2}


def _decompress_mozlz4(blob: bytes) -> bytes:
    import lz4.block
    if blob[:8] != b"mozLz40\x00":
        raise ValueError(f"not mozLz40 (got {blob[:8]!r})")
    return lz4.block.decompress(blob[8:])


def sample_firefox() -> dict:
    profiles = HOME / "Library" / "Application Support" / "Firefox" / "Profiles"
    if not profiles.exists():
        return {"status": "not_running"}
    candidates: list[Path] = []
    for p in profiles.iterdir():
        for rel in ("sessionstore.jsonlz4",
                    "sessionstore-backups/recovery.jsonlz4"):
            f = p / rel
            if f.exists():
                candidates.append(f)
    if not candidates:
        return {"status": "not_running"}
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    path = candidates[0]
    ff_running = is_running(PROCESS_NAMES["firefox"])
    age = time.time() - path.stat().st_mtime
    # If FF isn't running and the session file is older than an hour,
    # treat as not_running (otherwise we'd report stale data forever).
    if not ff_running and age > 3600:
        return {"status": "not_running"}
    try:
        raw = path.read_bytes()
        decompressed = _decompress_mozlz4(raw)
        ss = json.loads(decompressed)
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}
    per_window: list[int] = []
    for w in ss.get("windows", []):
        per_window.append(len(w.get("tabs", [])))
    return _result_ok(per_window)


SAMPLERS = {
    "safari": sample_safari,
    "chrome": sample_chrome,
    "firefox": sample_firefox,
    "duckduckgo": sample_duckduckgo,
}


# ---- gather + write -------------------------------------------------------

def gather_samples() -> list[list]:
    ts = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    rows: list[list] = []
    for b in BROWSERS:
        try:
            r = SAMPLERS[b]()
        except Exception as e:
            r = {"status": "error", "error": f"{type(e).__name__}: {e}"}
        if r["status"] == "ok":
            rows.append([
                ts, b, "ok",
                r["windows"], r["total_tabs"],
                "|".join(str(x) for x in r["tabs_per_window"]),
            ])
        elif r["status"] == "not_running":
            rows.append([ts, b, "not_running", "", "", ""])
        else:
            print(f"[{b}] error: {r.get('error', '')}", file=sys.stderr)
            rows.append([ts, b, "error", "", "", ""])
    return rows


def rotate_old_months() -> None:
    if not DATA_DIR.exists():
        return
    current = datetime.now().strftime("%Y-%m") + ".csv"
    for p in DATA_DIR.glob("*.csv"):
        if p.name == current:
            continue
        gz_path = p.with_suffix(".csv.gz")
        if gz_path.exists():
            continue
        with p.open("rb") as fin, gzip.open(gz_path, "wb") as fout:
            shutil.copyfileobj(fin, fout)
        p.unlink()


def append_rows(rows: list[list]) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    rotate_old_months()
    month = datetime.now().strftime("%Y-%m")
    csv_path = DATA_DIR / f"{month}.csv"
    new = not csv_path.exists()
    with csv_path.open("a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(CSV_HEADER)
        w.writerows(rows)
    return csv_path


# ---- read for plotting ----------------------------------------------------

def parse_range(s: str) -> timedelta:
    s = s.strip().lower()
    unit = s[-1]
    n = int(s[:-1])
    units = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}
    if unit not in units:
        raise ValueError(f"unknown range unit {unit!r}; use m/h/d/w")
    return timedelta(**{units[unit]: n})


def read_data_since(cutoff: datetime) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = {b: [] for b in BROWSERS}
    if not DATA_DIR.exists():
        return result
    files = sorted(list(DATA_DIR.glob("*.csv")) + list(DATA_DIR.glob("*.csv.gz")))
    for path in files:
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    ts = datetime.fromisoformat(row["ts"])
                except ValueError:
                    continue
                if ts < cutoff:
                    continue
                b = row["browser"]
                if b not in result:
                    continue
                result[b].append({
                    "ts": ts,
                    "status": row["status"],
                    "windows": int(row["windows"]) if row["windows"] else None,
                    "total_tabs": int(row["total_tabs"]) if row["total_tabs"] else None,
                })
    return result


# ---- subcommands ----------------------------------------------------------

def cmd_sample(args) -> None:
    rows = gather_samples()
    path = append_rows(rows)
    if args.print:
        for row in rows:
            print(",".join(str(c) for c in row))
        print(f"-> {path}", file=sys.stderr)


def cmd_status(args) -> None:
    rows = gather_samples()
    width = max(len(b) for b in BROWSERS)
    for ts, b, status, nwin, ntabs, per in rows:
        if status == "ok":
            detail = f"{ntabs} tabs in {nwin} window(s)"
            if per:
                detail += f"  [{per}]"
        else:
            detail = status
        print(f"{b.ljust(width)}  {detail}")


def cmd_plot(args) -> None:
    import matplotlib.pyplot as plt
    from matplotlib.dates import AutoDateFormatter, AutoDateLocator

    horizon = parse_range(args.range)
    cutoff = datetime.now(timezone.utc).astimezone() - horizon
    browsers = args.browsers.split(",") if args.browsers else BROWSERS
    data = read_data_since(cutoff)

    def plot_field(ax, field: str, label: str) -> None:
        any_data = False
        for b in browsers:
            rows = data.get(b, [])
            if not rows:
                continue
            xs = [r["ts"] for r in rows]
            ys = [
                r[field] if r["status"] == "ok" and r[field] is not None
                else float("nan")
                for r in rows
            ]
            if any(y == y for y in ys):  # NaN check
                any_data = True
            ax.plot(xs, ys, label=b, color=BROWSER_COLORS.get(b),
                    linewidth=1.5, marker=".", markersize=3)
        ax.set_ylabel(label)
        ax.grid(True, alpha=0.3)
        if any_data:
            ax.legend(loc="upper left")
        loc = AutoDateLocator()
        ax.xaxis.set_major_locator(loc)
        ax.xaxis.set_major_formatter(AutoDateFormatter(loc))

    if args.metric == "both":
        fig, (a1, a2) = plt.subplots(2, 1, sharex=True, figsize=(11, 6.5))
        plot_field(a1, "total_tabs", "Tabs")
        plot_field(a2, "windows", "Windows")
        a2.set_xlabel("Time")
    else:
        fig, ax = plt.subplots(figsize=(11, 4))
        if args.metric == "tabs":
            plot_field(ax, "total_tabs", "Tabs")
        else:
            plot_field(ax, "windows", "Windows")
        ax.set_xlabel("Time")

    fig.suptitle(f"tabcount — last {args.range}")
    fig.autofmt_xdate()
    fig.tight_layout()
    plt.show()


def _write_plist() -> None:
    if not PLIST_TEMPLATE.exists():
        sys.exit(f"missing template: {PLIST_TEMPLATE}")
    template = PLIST_TEMPLATE.read_text()
    plist = (template
             .replace("{{PYTHON}}", str(VENV_DIR / "bin" / "python"))
             .replace("{{SCRIPT}}", str(REPO_DIR / "tabcount.py"))
             .replace("{{LOG}}", str(LOG_PATH)))
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    PLIST_PATH.write_text(plist)


def _setup_venv() -> None:
    if VENV_DIR.exists():
        return
    print(f"Creating venv at {VENV_DIR} ...")
    subprocess.run([sys.executable, "-m", "venv", str(VENV_DIR)], check=True)
    pip = VENV_DIR / "bin" / "pip"
    subprocess.run([str(pip), "install", "--quiet",
                    "--upgrade", "pip"], check=True)
    subprocess.run([str(pip), "install", "--quiet",
                    "lz4", "matplotlib"], check=True)


def _write_wrapper() -> None:
    BIN_PATH.parent.mkdir(parents=True, exist_ok=True)
    wrapper = (
        "#!/bin/sh\n"
        f'exec "{VENV_DIR / "bin" / "python"}" '
        f'"{REPO_DIR / "tabcount.py"}" "$@"\n'
    )
    BIN_PATH.write_text(wrapper)
    BIN_PATH.chmod(0o755)


def cmd_install(args) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _setup_venv()
    _write_plist()
    uid = os.getuid()
    # Boot out any prior load, then bootstrap fresh.
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{PLIST_LABEL}"],
                   capture_output=True)
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(PLIST_PATH)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"launchctl bootstrap failed: {r.stderr.strip()}")
    _write_wrapper()
    print("Installed.")
    print(f"  LaunchAgent: {PLIST_PATH}  (every 5 min)")
    print(f"  CLI:         {BIN_PATH}")
    print(f"  Data dir:    {DATA_DIR}")
    print()
    print("First-run note: macOS will prompt you to allow automation of")
    print("Safari / Google Chrome / DuckDuckGo. Run `tabcount status` from")
    print("a Terminal once to trigger the prompts in a visible way.")


def cmd_uninstall(args) -> None:
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{PLIST_LABEL}"],
                   capture_output=True)
    if PLIST_PATH.exists():
        PLIST_PATH.unlink()
    if BIN_PATH.exists() or BIN_PATH.is_symlink():
        BIN_PATH.unlink()
    print(f"Uninstalled launchd job and CLI wrapper.")
    print(f"Data preserved at {DATA_DIR}.")
    print(f"To fully remove: rm -rf {RUNTIME_DIR}")


# ---- main -----------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(prog="tabcount", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sample", help="Poll once and append a row per browser.")
    s.add_argument("--print", action="store_true",
                   help="Also print rows to stdout.")

    sub.add_parser("status", help="Print current counts without writing.")

    pl = sub.add_parser("plot", help="Show interactive matplotlib window.")
    pl.add_argument("--range", default="30d",
                    help="Time window: 1d, 7d, 30d, 12w, 6h. Default 30d.")
    pl.add_argument("--metric", default="both",
                    choices=["tabs", "windows", "both"])
    pl.add_argument("--browsers", default=None,
                    help="Comma-separated subset of safari,chrome,firefox,duckduckgo.")

    sub.add_parser("install", help="Set up venv, LaunchAgent, and CLI wrapper.")
    sub.add_parser("uninstall", help="Remove LaunchAgent and CLI wrapper.")

    args = p.parse_args()
    {"sample": cmd_sample, "status": cmd_status, "plot": cmd_plot,
     "install": cmd_install, "uninstall": cmd_uninstall}[args.cmd](args)


if __name__ == "__main__":
    main()
