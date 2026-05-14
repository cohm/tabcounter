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

BROWSER_LABELS = {
    "safari": "Safari",
    "chrome": "Chrome",
    "firefox": "Firefox",
    "duckduckgo": "Duck Duck Go",
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
    Returns (ok, [tabs_per_window], error_message). Fails if the browser's
    scripting dictionary doesn't expose `tabs` on windows."""
    script = f'''
    tell application "{app_name}"
        set counts to {{}}
        repeat with w in windows
            set end of counts to (count of tabs of w) as text
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


def applescript_window_count(app_name: str) -> tuple[bool, int, str]:
    """Direct `count of windows` via the app's own AppleScript dict.
    Used as a fallback when the app exposes windows but not tabs."""
    script = f'tell application "{app_name}" to return count of windows'
    ok, out, err = run_osascript(script)
    if not ok:
        return False, 0, err
    try:
        return True, int(out), ""
    except ValueError:
        return False, 0, f"parse: {out!r}"


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
    if ok and counts:
        return _result_ok(counts)
    # DDG's AppleScript dictionary exposes `windows` but not `tabs`. Fall back
    # to a windows-only count via DDG's own dict (no System Events needed).
    ok2, nwin, err2 = applescript_window_count(APP_NAMES["duckduckgo"])
    if ok2:
        return {
            "status": "ok",
            "windows": nwin,
            "total_tabs": None,
            "tabs_per_window": [],
        }
    # Last resort: UI scripting via System Events (needs separate permission).
    ok3, nwin3, err3 = applescript_window_count_fallback(PROCESS_NAMES["duckduckgo"])
    if ok3:
        return {
            "status": "ok",
            "windows": nwin3,
            "total_tabs": None,
            "tabs_per_window": [],
        }
    return {"status": "error", "error": err or err2 or err3}


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
            total = r.get("total_tabs")
            rows.append([
                ts, b, "ok",
                r["windows"],
                "" if total is None else total,
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
            if ntabs == "":
                detail = f"{nwin} window(s), tabs unavailable"
            else:
                detail = f"{ntabs} tabs in {nwin} window(s)"
                if per:
                    detail += f"  [{per}]"
        else:
            detail = status
        print(f"{b.ljust(width)}  {detail}")


def _make_metric_figure(metric_field: str, label: str, data: dict,
                        browsers: list[str]):
    """Build a figure for a single metric with live log-toggle checkboxes.
    Returns the figure (and keeps the CheckButtons widget alive on it)."""
    import matplotlib.pyplot as plt
    import matplotlib.font_manager as fm
    from matplotlib.dates import AutoDateFormatter, AutoDateLocator
    from matplotlib.widgets import CheckButtons

    plt.rcParams["font.family"] = "sans-serif"
    plt.rcParams["font.sans-serif"] = ["Figtree"] + plt.rcParamsDefault["font.sans-serif"]
    if any(f.name == "Figtree" for f in fm.fontManager.ttflist):
        plt.rcParams["font.size"] = 12
        plt.rcParams["font.weight"] = "medium"
        plt.rcParams["axes.labelweight"] = "medium"

    fig = plt.figure(figsize=(11, 7))
    try:
        fig.canvas.manager.set_window_title(f"tabcount — {label.lower()}")
    except Exception:
        pass

    ax = fig.add_axes((0.07, 0.12, 0.76, 0.78))
    cb_ax = fig.add_axes((0.85, 0.50, 0.13, 0.16))
    cb_ax.set_axis_off()

    state = {"logy": False, "logx_time": False}
    now = datetime.now(timezone.utc).astimezone()

    def redraw() -> None:
        ax.clear()
        any_data = False
        for b in browsers:
            rows = data.get(b, [])
            if not rows:
                continue
            ys = [
                r[metric_field]
                if r["status"] == "ok" and r[metric_field] is not None
                else float("nan")
                for r in rows
            ]
            if state["logx_time"]:
                # 1 minute floor so the most recent sample doesn't go to log(0).
                xs = [max(1.0 / 60.0,
                          (now - r["ts"]).total_seconds() / 3600.0)
                      for r in rows]
            else:
                xs = [r["ts"] for r in rows]
            if any(y == y for y in ys):  # at least one non-NaN
                any_data = True
            ax.plot(xs, ys, label=BROWSER_LABELS.get(b, b),
                    color=BROWSER_COLORS.get(b),
                    linewidth=1.5, marker=".", markersize=3)

        if state["logx_time"]:
            ax.set_xscale("log")
            ax.invert_xaxis()  # newer (smaller dt) on the right
            ax.set_xlabel("Hours since now (log)")
        else:
            ax.set_xscale("linear")
            loc = AutoDateLocator()
            ax.xaxis.set_major_locator(loc)
            ax.xaxis.set_major_formatter(AutoDateFormatter(loc))
            ax.set_xlabel("Time")
            fig.autofmt_xdate()

        ax.set_yscale("log" if state["logy"] else "linear")
        ax.set_ylabel(label)
        ax.grid(True, which="both", alpha=0.3)
        if any_data:
            ax.legend(loc="best")
        fig.canvas.draw_idle()

    cb = CheckButtons(cb_ax,
                      ["Log Y", "Log X (since now)"],
                      [state["logy"], state["logx_time"]])

    def on_click(label_clicked: str) -> None:
        if label_clicked == "Log Y":
            state["logy"] = not state["logy"]
        else:
            state["logx_time"] = not state["logx_time"]
        redraw()

    cb.on_clicked(on_click)
    fig._tabcount_widgets = cb  # keep widget alive

    # Wrap savefig so exports (PNG/PDF/...) don't include the checkbox panel.
    # The matplotlib toolbar's save button routes through fig.savefig().
    original_savefig = fig.savefig

    def savefig_without_widgets(*args, **kwargs):
        kwargs.setdefault("bbox_inches", "tight")
        cb_ax.set_visible(False)
        try:
            return original_savefig(*args, **kwargs)
        finally:
            cb_ax.set_visible(True)
            fig.canvas.draw_idle()

    fig.savefig = savefig_without_widgets

    redraw()
    return fig


def cmd_plot(args) -> None:
    import matplotlib.pyplot as plt

    horizon = parse_range(args.range)
    cutoff = datetime.now(timezone.utc).astimezone() - horizon
    browsers = args.browsers.split(",") if args.browsers else BROWSERS
    data = read_data_since(cutoff)

    figs = []
    if args.metric in ("tabs", "both"):
        figs.append(_make_metric_figure(
            "total_tabs", "Tabs", data, browsers))
    if args.metric in ("windows", "both"):
        figs.append(_make_metric_figure(
            "windows", "Windows", data, browsers))

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
    script_path = REPO_DIR / "tabcount.py"
    print("Installed.")
    print(f"  LaunchAgent: {PLIST_PATH}  (every 5 min)")
    print(f"  Data dir:    {DATA_DIR}")
    print()
    print("Invoke directly:")
    print(f"  {script_path} status")
    print()
    print("Or add a shell alias (paste into ~/.zshrc):")
    print(f"  alias tabcount='{script_path}'")
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
    print(f"Uninstalled launchd job.")
    print(f"Data preserved at {DATA_DIR}.")
    print(f"To fully remove: rm -rf {RUNTIME_DIR}")


# ---- main -----------------------------------------------------------------

def _maybe_reexec_under_venv() -> None:
    """If the venv exists and we're not already running under it, re-exec.
    This means `./tabcount.py` works regardless of which python invoked it,
    once `install` has set up ~/.tabcount/venv/.

    Compares sys.prefix (the venv dir when running under the venv) to
    VENV_DIR — comparing sys.executable to venv/bin/python would falsely
    match because venv/bin/python is a symlink to the base interpreter.
    """
    venv_python = VENV_DIR / "bin" / "python"
    if not venv_python.exists():
        return
    try:
        if Path(sys.prefix).resolve() == VENV_DIR.resolve():
            return
    except OSError:
        return
    os.execv(str(venv_python), [str(venv_python), str(Path(__file__).resolve()),
                                *sys.argv[1:]])


def main() -> None:
    _maybe_reexec_under_venv()
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
