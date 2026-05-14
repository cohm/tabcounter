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
CSV_HEADER = ["ts", "browser", "status", "windows", "total_tabs",
              "tabs_per_window", "rss_kb"]

# Substring matched against each process's executable path in `ps -axo command`.
# Catches all renderer/helper/GPU subprocesses of each browser.
BROWSER_APP_PATH = {
    "safari": "/Safari.app/",
    "chrome": "/Google Chrome.app/",
    "firefox": "/Firefox.app/",
    "duckduckgo": "/DuckDuckGo.app/",
}

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


def system_ram_bytes() -> int:
    try:
        r = subprocess.run(
            ["sysctl", "-n", "hw.memsize"],
            capture_output=True, text=True, timeout=2,
        )
        return int(r.stdout.strip())
    except Exception:
        return 0


def memory_by_browser() -> dict[str, int]:
    """Sum RSS (KB) per browser.

    Chrome / Firefox: walk the process tree from each main-app process and
    sum all descendants (precise — helpers are real children).

    Safari / DuckDuckGo: WebKit content/networking/GPU XPC services are
    launchd-spawned, not children of the browser, so the tree walk misses
    them. We sum the WebKit XPC processes separately and attribute them to
    whichever WebKit-using browser is running; if both, split by ratio of
    main-app RSS. Other WebKit-using apps (Mail, Messages, ...) cause a
    small overcount.
    """
    totals = {b: 0 for b in BROWSERS}
    try:
        r = subprocess.run(
            ["ps", "-axww", "-o", "pid=,ppid=,rss=,command="],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return totals

    info: dict[int, tuple[int, int, str]] = {}
    for line in r.stdout.splitlines():
        s = line.strip()
        if not s:
            continue
        parts = s.split(None, 3)
        if len(parts) < 4:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
            rss = int(parts[2])
        except ValueError:
            continue
        info[pid] = (ppid, rss, parts[3])

    children: dict[int, list[int]] = {}
    for pid, (ppid, _, _) in info.items():
        children.setdefault(ppid, []).append(pid)

    def descendants(roots: list[int]) -> set[int]:
        seen: set[int] = set()
        stack = list(roots)
        while stack:
            pid = stack.pop()
            if pid in seen:
                continue
            seen.add(pid)
            stack.extend(children.get(pid, []))
        return seen

    main_pids: dict[str, list[int]] = {b: [] for b in BROWSERS}
    main_rss: dict[str, int] = {b: 0 for b in BROWSERS}
    for pid, (_, rss, cmd) in info.items():
        for b, marker in BROWSER_APP_PATH.items():
            if marker in cmd:
                main_pids[b].append(pid)
                main_rss[b] += rss
                break

    counted: set[int] = set()
    for b in BROWSERS:
        for pid in descendants(main_pids[b]):
            if pid not in counted:
                totals[b] += info[pid][1]
                counted.add(pid)

    webkit_markers = ("com.apple.WebKit.WebContent",
                      "com.apple.WebKit.Networking",
                      "com.apple.WebKit.GPU")
    webkit_total = 0
    for pid, (_, rss, cmd) in info.items():
        if pid in counted:
            continue
        if any(m in cmd for m in webkit_markers):
            webkit_total += rss
            counted.add(pid)

    safari_run = main_rss["safari"] > 0
    ddg_run = main_rss["duckduckgo"] > 0
    if safari_run and ddg_run:
        denom = main_rss["safari"] + main_rss["duckduckgo"]
        safari_share = int(webkit_total * main_rss["safari"] / denom)
        totals["safari"] += safari_share
        totals["duckduckgo"] += webkit_total - safari_share
    elif safari_run:
        totals["safari"] += webkit_total
    elif ddg_run:
        totals["duckduckgo"] += webkit_total

    return totals


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
    mem = memory_by_browser()
    rows: list[list] = []
    for b in BROWSERS:
        try:
            r = SAMPLERS[b]()
        except Exception as e:
            r = {"status": "error", "error": f"{type(e).__name__}: {e}"}
        rss = mem.get(b, 0)
        if r["status"] == "ok":
            total = r.get("total_tabs")
            rows.append([
                ts, b, "ok",
                r["windows"],
                "" if total is None else total,
                "|".join(str(x) for x in r["tabs_per_window"]),
                rss,
            ])
        elif r["status"] == "not_running":
            rows.append([ts, b, "not_running", "", "", "", rss])
        else:
            print(f"[{b}] error: {r.get('error', '')}", file=sys.stderr)
            rows.append([ts, b, "error", "", "", "", rss])
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


def _migrate_csv(path: Path, new_header: list[str]) -> None:
    """Rewrite a CSV in place to use new_header. Missing columns become ""."""
    with path.open("r", newline="") as f:
        existing = list(csv.DictReader(f))
    tmp = path.with_suffix(".csv.tmp")
    with tmp.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=new_header)
        w.writeheader()
        for row in existing:
            w.writerow({k: row.get(k, "") for k in new_header})
    tmp.replace(path)


def append_rows(rows: list[list]) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    rotate_old_months()
    month = datetime.now().strftime("%Y-%m")
    csv_path = DATA_DIR / f"{month}.csv"
    if csv_path.exists():
        with csv_path.open("r", newline="") as f:
            existing_header = next(csv.reader(f), [])
        if existing_header != CSV_HEADER:
            _migrate_csv(csv_path, CSV_HEADER)
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
                    "status": row.get("status", ""),
                    "windows": int(row["windows"]) if row.get("windows") else None,
                    "total_tabs": int(row["total_tabs"]) if row.get("total_tabs") else None,
                    "rss_kb": int(row["rss_kb"]) if row.get("rss_kb") else None,
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
    for ts, b, status, nwin, ntabs, per, rss in rows:
        if status == "ok":
            if ntabs == "":
                detail = f"{nwin} window(s), tabs unavailable"
            else:
                detail = f"{ntabs} tabs in {nwin} window(s)"
                if per:
                    detail += f"  [{per}]"
        else:
            detail = status
        if isinstance(rss, int) and rss > 0:
            detail += f"  ~{rss / 1024:.0f} MB RSS"
        print(f"{b.ljust(width)}  {detail}")


def _make_metric_figure(metric_field: str, label: str, data: dict,
                        browsers: list[str], ram_bytes: int):
    """Figure for one metric with live checkboxes for log scales and an
    optional memory overlay on a right y-axis (in % of system RAM, or GB)."""
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

    ax = fig.add_axes((0.07, 0.12, 0.68, 0.78))
    mem_ax = ax.twinx()
    mem_ax.set_visible(False)

    cb_ax = fig.add_axes((0.85, 0.38, 0.14, 0.34))
    cb_ax.set_axis_off()

    # mem_mode: None | "percent" | "gb"
    state = {"logy": False, "logx_time": False, "mem_mode": None}
    now = datetime.now(timezone.utc).astimezone()

    def _rss_to_y(rss_kb: int) -> float:
        if state["mem_mode"] == "percent":
            if ram_bytes <= 0:
                return float("nan")
            return rss_kb * 1024.0 / ram_bytes * 100.0
        return rss_kb / (1024.0 * 1024.0)  # GB

    def _pick_legend_loc() -> str:
        """Pick legend placement by binning data points into a 3x3 grid
        across both axes. Considers 4 corners + center-left/right; skips the
        chart middle and top/bottom-center (usually bad spots)."""
        import math

        def to_frac(value: float, lo: float, hi: float, scale: str) -> float:
            if scale == "log" and lo > 0 and hi > 0 and value > 0:
                return ((math.log(value) - math.log(lo))
                        / (math.log(hi) - math.log(lo)))
            if hi == lo:
                return 0.5
            return (value - lo) / (hi - lo)

        x_lo, x_hi = ax.get_xlim()
        if x_lo > x_hi:
            x_lo, x_hi = x_hi, x_lo
        y_lo, y_hi = ax.get_ylim()
        if y_lo > y_hi:
            y_lo, y_hi = y_hi, y_lo
        x_scale = ax.get_xscale()
        y_scale = ax.get_yscale()

        grid = {(c, r): 0 for c in ("L", "C", "R") for r in ("B", "M", "T")}

        def tally(lines, src_lo: float, src_hi: float, src_scale: str) -> None:
            for line in lines:
                for x, y in line.get_xydata():
                    if not (x == x and y == y):
                        continue
                    xf = to_frac(x, x_lo, x_hi, x_scale)
                    yf = to_frac(y, src_lo, src_hi, src_scale)
                    xf = max(0.0, min(1.0, xf))
                    yf = max(0.0, min(1.0, yf))
                    col = "L" if xf < 1 / 3 else ("R" if xf >= 2 / 3 else "C")
                    row = "B" if yf < 1 / 3 else ("T" if yf >= 2 / 3 else "M")
                    grid[(col, row)] += 1

        tally(ax.get_lines(), y_lo, y_hi, y_scale)
        if state["mem_mode"] is not None:
            m_lo, m_hi = mem_ax.get_ylim()
            if m_lo > m_hi:
                m_lo, m_hi = m_hi, m_lo
            tally(mem_ax.get_lines(), m_lo, m_hi, mem_ax.get_yscale())

        candidates = {
            ("L", "T"): "upper left",
            ("R", "T"): "upper right",
            ("L", "B"): "lower left",
            ("R", "B"): "lower right",
            ("L", "M"): "center left",
            ("R", "M"): "center right",
        }
        best = min(candidates.keys(), key=lambda k: grid[k])
        return candidates[best]

    def redraw() -> None:
        ax.clear()
        mem_ax.clear()
        # ax.clear() on a twinx axes resets the right-side configuration;
        # re-apply so ticks and label render on the right, not the left.
        mem_ax.yaxis.tick_right()
        mem_ax.yaxis.set_label_position("right")
        any_data = False

        for b in browsers:
            rows = data.get(b, [])
            if not rows:
                continue
            if state["logx_time"]:
                xs = [max(1.0 / 60.0,
                          (now - r["ts"]).total_seconds() / 3600.0)
                      for r in rows]
            else:
                xs = [r["ts"] for r in rows]

            ys = [
                r[metric_field]
                if r["status"] == "ok" and r[metric_field] is not None
                else float("nan")
                for r in rows
            ]
            if any(y == y for y in ys):
                any_data = True
            ax.plot(xs, ys, label=BROWSER_LABELS.get(b, b),
                    color=BROWSER_COLORS.get(b),
                    linewidth=1.5, marker=".", markersize=3)

            if state["mem_mode"] is not None:
                mem_ys = [
                    _rss_to_y(r["rss_kb"])
                    if r.get("rss_kb") and r["rss_kb"] > 0
                    else float("nan")
                    for r in rows
                ]
                mem_ax.plot(xs, mem_ys, color=BROWSER_COLORS.get(b),
                            linestyle="--", linewidth=1.2, alpha=0.75,
                            label="_nolegend_")

        if state["logx_time"]:
            ax.set_xscale("log")
            ax.invert_xaxis()
            mem_ax.set_xscale("log")
            ax.set_xlabel("Hours since now (log)")
        else:
            ax.set_xscale("linear")
            mem_ax.set_xscale("linear")
            loc = AutoDateLocator()
            ax.xaxis.set_major_locator(loc)
            ax.xaxis.set_major_formatter(AutoDateFormatter(loc))
            ax.set_xlabel("Time")
            fig.autofmt_xdate()

        ax.set_yscale("log" if state["logy"] else "linear")
        ax.set_ylabel(label)
        ax.grid(True, which="both", alpha=0.3)

        if state["mem_mode"] is not None:
            mem_ax.set_visible(True)
            if state["mem_mode"] == "percent":
                mem_ax.set_ylim(0, 100)
                mem_ax.set_ylabel("Memory (% of RAM)")
            else:
                mem_ax.set_ylabel("Memory (GB)")
        else:
            mem_ax.set_visible(False)

        if any_data:
            ax.legend(loc=_pick_legend_loc())
        fig.canvas.draw_idle()

    labels = ["Log Y", "Log X (since now)", "Memory (%)", "Memory (GB)"]
    cb = CheckButtons(cb_ax, labels, [False, False, False, False])

    _busy = [False]

    def on_click(label_clicked: str) -> None:
        if _busy[0]:
            return
        _busy[0] = True
        try:
            status = cb.get_status()
            state["logy"] = status[0]
            state["logx_time"] = status[1]
            pct, gb = status[2], status[3]
            if pct and gb:
                # Mutual exclusion: keep the just-clicked one, turn off the other.
                if label_clicked == "Memory (%)":
                    cb.set_active(3)
                    pct, gb = True, False
                else:
                    cb.set_active(2)
                    pct, gb = False, True
            state["mem_mode"] = "percent" if pct else ("gb" if gb else None)
        finally:
            _busy[0] = False
        redraw()

    cb.on_clicked(on_click)
    fig._tabcount_widgets = cb

    # Hide the checkbox panel during savefig (toolbar save routes through here).
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
    ram_bytes = system_ram_bytes()

    figs = []
    if args.metric in ("tabs", "both"):
        figs.append(_make_metric_figure(
            "total_tabs", "Tabs", data, browsers, ram_bytes))
    if args.metric in ("windows", "both"):
        figs.append(_make_metric_figure(
            "windows", "Windows", data, browsers, ram_bytes))

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
