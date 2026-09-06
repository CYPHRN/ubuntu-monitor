#!/usr/bin/env python3
"""sysmon - one dashboard for GPU usage and system temperatures.

Zero third-party dependencies: reads `nvidia-smi` and /sys/class/hwmon directly.

Keys:  q quit   +/- refresh rate   r reset peaks
"""

from __future__ import annotations

import argparse
import curses
import glob
import locale
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

# --- thresholds: percent for utilisation, degrees C for temperatures ---
GPU_TEMP_WARN, GPU_TEMP_CRIT = 80.0, 87.0
CPU_TEMP_WARN, CPU_TEMP_CRIT = 80.0, 90.0
UTIL_WARN, UTIL_CRIT = 80.0, 95.0
MEM_WARN, MEM_CRIT = 80.0, 92.0

MIN_INTERVAL, MAX_INTERVAL = 0.25, 5.0

BLOCK_FULL, BLOCK_EMPTY = "█", "░"
DEG = "°"

GPU_QUERY = (
    "index,name,utilization.gpu,temperature.gpu,memory.used,memory.total,"
    "power.draw,power.limit,fan.speed,clocks.gr,clocks.mem"
)


def _num(text):
    """Parse an nvidia-smi field, tolerating [N/A] and [Not Supported]."""
    text = (text or "").strip()
    if not text or text.startswith("[") or text.lower() in ("n/a", "unknown"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


@dataclass
class Gpu:
    index: int
    name: str
    util: float | None
    temp: float | None
    mem_used: float | None
    mem_total: float | None
    power: float | None
    power_limit: float | None
    fan: float | None
    clk_gr: float | None
    clk_mem: float | None


def query_gpus():
    """Return (list_of_gpus, error_message). Either may be None."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None, "nvidia-smi not found"
    try:
        proc = subprocess.run(
            [exe, "--query-gpu=" + GPU_QUERY, "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return None, "nvidia-smi: %s" % exc.__class__.__name__
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        return None, detail[0] if detail else "nvidia-smi exit %d" % proc.returncode

    gpus = []
    for line in proc.stdout.strip().splitlines():
        f = [p.strip() for p in line.split(",")]
        if len(f) < 11:
            continue
        gpus.append(Gpu(
            index=int(_num(f[0]) or 0),
            name=f[1],
            util=_num(f[2]), temp=_num(f[3]),
            mem_used=_num(f[4]), mem_total=_num(f[5]),
            power=_num(f[6]), power_limit=_num(f[7]),
            fan=_num(f[8]), clk_gr=_num(f[9]), clk_mem=_num(f[10]),
        ))
    return gpus, None


class GpuSampler(threading.Thread):
    """Polls nvidia-smi off the UI thread so a slow call never stalls redraws."""

    def __init__(self, interval):
        super().__init__(daemon=True)
        self.interval = interval
        self._lock = threading.Lock()
        self._gpus = []
        self._err = None
        self._stop = threading.Event()
        self._wake = threading.Event()

    def run(self):
        while not self._stop.is_set():
            gpus, err = query_gpus()
            with self._lock:
                if gpus is not None:
                    self._gpus = gpus
                self._err = err
            self._wake.wait(max(self.interval, MIN_INTERVAL))
            self._wake.clear()

    def snapshot(self):
        with self._lock:
            return list(self._gpus), self._err

    def poke(self):
        self._wake.set()

    def stop(self):
        self._stop.set()
        self._wake.set()


def read_cpu_times():
    times = {}
    try:
        with open("/proc/stat") as fh:
            for line in fh:
                if not line.startswith("cpu"):
                    break
                parts = line.split()
                vals = [int(v) for v in parts[1:]]
                idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
                times[parts[0]] = (sum(vals), idle)
    except (OSError, ValueError):
        pass
    return times


def cpu_percent(prev, cur):
    out = {}
    for key, (total, idle) in cur.items():
        if key not in prev:
            continue
        ptotal, pidle = prev[key]
        d_total, d_idle = total - ptotal, idle - pidle
        if d_total > 0:
            out[key] = max(0.0, min(100.0, 100.0 * (d_total - d_idle) / d_total))
        else:
            out[key] = 0.0
    return out


def read_hwmon():
    """Map chip name -> [(label, celsius), ...] for every hwmon temp input."""
    chips = {}
    for hw in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        try:
            with open(os.path.join(hw, "name")) as fh:
                chip = fh.read().strip()
        except OSError:
            continue
        entries = []
        for inp in sorted(glob.glob(os.path.join(hw, "temp*_input"))):
            base = inp[: -len("_input")]
            label = os.path.basename(base)
            try:
                with open(base + "_label") as fh:
                    label = fh.read().strip() or label
            except OSError:
                pass
            try:
                with open(inp) as fh:
                    entries.append((label, int(fh.read().strip()) / 1000.0))
            except (OSError, ValueError):
                continue
        if entries:
            chips.setdefault(chip, []).extend(entries)
    return chips


CPU_CHIP_PREF = ("k10temp", "zenpower", "coretemp")
CPU_LABEL_PREF = ("Tctl", "Tdie", "Package id 0", "CPU")


def pick_cpu_temp(chips):
    """Best-guess package temperature, plus the sensor it came from."""
    for chip in CPU_CHIP_PREF:
        entries = chips.get(chip)
        if not entries:
            continue
        for want in CPU_LABEL_PREF:
            for label, val in entries:
                if label == want:
                    return val, "%s/%s" % (chip, label)
        label, val = max(entries, key=lambda e: e[1])
        return val, "%s/%s" % (chip, label)
    for chip, entries in chips.items():
        label, val = max(entries, key=lambda e: e[1])
        return val, "%s/%s" % (chip, label)
    return None, None


def other_temps(chips):
    """Non-CPU, non-GPU sensors worth showing (drives, chipset, board)."""
    skip = set(CPU_CHIP_PREF) | {"amdgpu", "nouveau"}
    out = []
    for chip, entries in sorted(chips.items()):
        if chip in skip:
            continue
        for label, val in entries:
            out.append(("%s %s" % (chip, label), val))
    return out


def read_meminfo():
    info = {}
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                parts = rest.strip().split()
                if parts:
                    info[key] = float(parts[0]) * 1024
    except OSError:
        return None, None
    total = info.get("MemTotal")
    avail = info.get("MemAvailable", info.get("MemFree"))
    if total is None or avail is None:
        return None, None
    return total - avail, total


def read_loadavg():
    try:
        with open("/proc/loadavg") as fh:
            p = fh.read().split()
        return float(p[0]), float(p[1]), float(p[2])
    except (OSError, ValueError, IndexError):
        return None


def cpu_model():
    try:
        with open("/proc/cpuinfo") as fh:
            for line in fh:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return "CPU"


def gib(n):
    return n / (1024.0 ** 3)


def make_bar(frac, width):
    width = max(1, width)
    frac = 0.0 if frac is None else max(0.0, min(1.0, frac))
    filled = int(round(frac * width))
    return BLOCK_FULL * filled + BLOCK_EMPTY * (width - filled)


# --- curses colour pairs ---
C_OK, C_WARN, C_CRIT, C_DIM, C_HEAD, C_LABEL = 1, 2, 3, 4, 5, 6


def init_colors():
    if not curses.has_colors():
        return False
    curses.start_color()
    try:
        curses.use_default_colors()
        bg = -1
    except curses.error:
        bg = curses.COLOR_BLACK
    curses.init_pair(C_OK, curses.COLOR_GREEN, bg)
    curses.init_pair(C_WARN, curses.COLOR_YELLOW, bg)
    curses.init_pair(C_CRIT, curses.COLOR_RED, bg)
    curses.init_pair(C_DIM, curses.COLOR_BLUE, bg)
    curses.init_pair(C_HEAD, curses.COLOR_CYAN, bg)
    curses.init_pair(C_LABEL, curses.COLOR_WHITE, bg)
    return True


class Theme:
    def __init__(self, colored):
        self.colored = colored

    def _p(self, pair, bold=False):
        if not self.colored:
            return curses.A_BOLD if bold else curses.A_NORMAL
        attr = curses.color_pair(pair)
        return attr | curses.A_BOLD if bold else attr

    def level(self, value, warn, crit):
        if value is None:
            return self.dim
        if value >= crit:
            return self._p(C_CRIT, True)
        if value >= warn:
            return self._p(C_WARN)
        return self._p(C_OK)

    @property
    def ok(self):
        return self._p(C_OK)

    @property
    def crit(self):
        return self._p(C_CRIT, True)

    @property
    def dim(self):
        return self._p(C_DIM)

    @property
    def head(self):
        return self._p(C_HEAD, True)

    @property
    def label(self):
        return self._p(C_LABEL)


def put(win, y, x, text, attr, max_x):
    """Clipped addstr; returns the new x. Never raises on edges."""
    if y < 0 or x < 0 or x >= max_x or not text:
        return x
    text = text[: max_x - x]
    try:
        win.addstr(y, x, text, attr)
    except curses.error:
        pass
    return x + len(text)


LABEL_W, VALUE_W = 6, 12


def meter(theme, label, frac, value, attr, inner_w, note=None):
    """One labelled bar row, sized so the optional trailing note still fits."""
    note_text = ("  " + note) if note else ""
    fixed = 2 + LABEL_W + 2 + VALUE_W + len(note_text)
    bar_w = max(4, inner_w - fixed)
    segs = [
        ("  " + label.ljust(LABEL_W), theme.label),
        (make_bar(frac, bar_w), attr),
        ("  " + value.ljust(VALUE_W), attr),
    ]
    if note_text:
        segs.append((note_text, theme.dim))
    return segs


def build_rows(theme, state, inner_w, inner_h):
    """Core rows first, then optional detail while vertical space remains."""
    gpus, gpu_err = state["gpus"], state["gpu_err"]
    rows, optional = [], []

    if not gpus:
        rows.append([("GPU", theme.head), ("  " + (gpu_err or "no NVIDIA GPU detected"), theme.dim)])
        rows.append(None)
    for g in gpus:
        tag = "GPU %d" % g.index if len(gpus) > 1 else "GPU"
        rows.append([(tag.ljust(6), theme.head), (g.name, theme.label)])
        if g.util is not None:
            rows.append(meter(theme, "Util", g.util / 100.0, "%.0f%%" % g.util,
                              theme.level(g.util, UTIL_WARN, UTIL_CRIT), inner_w))
        if g.temp is not None:
            peak = state["peaks"].get("gpu%d" % g.index)
            note = ("peak %.0f%s" % (peak, DEG)) if peak and inner_w >= 62 else None
            hot = "  HOT" if g.temp >= GPU_TEMP_CRIT else ""
            rows.append(meter(theme, "Temp", g.temp / 100.0, "%.0f%sC%s" % (g.temp, DEG, hot),
                              theme.level(g.temp, GPU_TEMP_WARN, GPU_TEMP_CRIT), inner_w, note))
        if g.mem_used is not None and g.mem_total:
            pct = 100.0 * g.mem_used / g.mem_total
            rows.append(meter(theme, "VRAM", g.mem_used / g.mem_total,
                              "%.1f/%.1fG" % (g.mem_used / 1024.0, g.mem_total / 1024.0),
                              theme.level(pct, MEM_WARN, MEM_CRIT), inner_w))
        if g.power is not None:
            lim = g.power_limit or 0.0
            frac = (g.power / lim) if lim else None
            rows.append(meter(theme, "Power", frac,
                              "%.0f/%.0fW" % (g.power, lim) if lim else "%.0fW" % g.power,
                              theme.ok, inner_w))
        extras = []
        if g.fan is not None:
            extras.append("fan %.0f%%" % g.fan)
        if g.clk_gr is not None:
            extras.append("core %.0fMHz" % g.clk_gr)
        if g.clk_mem is not None:
            extras.append("mem %.0fMHz" % g.clk_mem)
        # keep clocks with their GPU block, but only when height allows
        if extras and inner_h >= 16:
            rows.append([("  " + "   ".join(extras), theme.dim)])
        rows.append(None)

    rows.append([("CPU".ljust(6), theme.head), (state["cpu_model"], theme.label)])
    total = state["cpu_pct"].get("cpu")
    if total is not None:
        rows.append(meter(theme, "Util", total / 100.0, "%.0f%%" % total,
                          theme.level(total, UTIL_WARN, UTIL_CRIT), inner_w))
    ctemp, csensor = state["cpu_temp"], state["cpu_sensor"]
    if ctemp is not None:
        peak = state["peaks"].get("cpu")
        note = ("peak %.0f%s" % (peak, DEG)) if peak and inner_w >= 62 else None
        hot = "  HOT" if ctemp >= CPU_TEMP_CRIT else ""
        rows.append(meter(theme, "Temp", ctemp / 100.0, "%.0f%sC%s" % (ctemp, DEG, hot),
                          theme.level(ctemp, CPU_TEMP_WARN, CPU_TEMP_CRIT), inner_w, note))
    load = state["load"]
    if load:
        rows.append([("  " + "Load".ljust(LABEL_W), theme.label),
                     ("%.2f  %.2f  %.2f" % load, theme.label),
                     ("   (%d cores)" % state["ncpu"], theme.dim)])
    rows.append(None)

    mem_used, mem_total = state["mem"]
    if mem_used is not None:
        pct = 100.0 * mem_used / mem_total
        rows.append(meter(theme, "MEM", mem_used / mem_total,
                          "%.1f/%.1fG" % (gib(mem_used), gib(mem_total)),
                          theme.level(pct, MEM_WARN, MEM_CRIT), inner_w))

    cores = sorted((k for k in state["cpu_pct"] if k != "cpu"),
                   key=lambda k: int(k[3:]) if k[3:].isdigit() else 0)
    if cores:
        per_line = max(1, inner_w // 19)
        optional.append(None)
        optional.append([("  cores", theme.dim)])
        for i in range(0, len(cores), per_line):
            segs = []
            for key in cores[i:i + per_line]:
                val = state["cpu_pct"][key]
                segs.append(("  %-4s" % key[3:], theme.dim))
                segs.append((make_bar(val / 100.0, 8), theme.level(val, UTIL_WARN, UTIL_CRIT)))
                segs.append((" %3.0f%%" % val, theme.dim))
            optional.append(segs)

    if state["others"]:
        optional.append(None)
        optional.append([("  sensors", theme.dim)])
        for name, val in state["others"][:6]:
            optional.append([("  %-22s" % name[:22], theme.dim),
                             ("%.0f%sC" % (val, DEG), theme.level(val, 60, 75))])
    if csensor:
        optional.append(None)
        optional.append([("  cpu sensor: %s" % csensor, theme.dim)])

    for row in optional:
        if len(rows) + 1 > inner_h:
            break
        rows.append(row)
    return rows[:inner_h]


def draw(stdscr, theme, state):
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    if h < 10 or w < 44:
        put(stdscr, 0, 0, "terminal too small (need 44x10)"[: max(0, w)], theme.crit, w)
        stdscr.noutrefresh()
        curses.doupdate()
        return

    title = " sysmon "
    clock = " %s  %.2fs " % (time.strftime("%H:%M:%S"), state["interval"])
    fill = w - 2 - len(title) - len(clock) - 2
    top = "┌─" + title + "─" * max(0, fill) + clock + "─┐"
    put(stdscr, 0, 0, top[:w], theme.head, w)

    inner_w, inner_h = w - 4, h - 2
    rows = build_rows(theme, state, inner_w, inner_h)
    for i in range(inner_h):
        y = 1 + i
        put(stdscr, y, 0, "│", theme.head, w)
        put(stdscr, y, w - 1, "│", theme.head, w)
        if i < len(rows) and rows[i]:
            x = 1
            for text, attr in rows[i]:
                x = put(stdscr, y, x, text, attr, w - 1)

    keys = " q quit   +/- rate   r reset peaks "
    fill = w - 2 - len(keys)
    left = max(0, fill // 2)
    bottom = "└" + "─" * left + keys + "─" * max(0, fill - left) + "┘"
    put(stdscr, h - 1, 0, bottom[:w], theme.head, w)
    stdscr.noutrefresh()
    curses.doupdate()


def collect(state, sampler, prev_cpu):
    gpus, gpu_err = sampler.snapshot()
    cur_cpu = read_cpu_times()
    chips = read_hwmon()
    cpu_temp, cpu_sensor = pick_cpu_temp(chips)

    state["gpus"], state["gpu_err"] = gpus, gpu_err
    state["cpu_pct"] = cpu_percent(prev_cpu, cur_cpu) if prev_cpu else {}
    state["cpu_temp"], state["cpu_sensor"] = cpu_temp, cpu_sensor
    state["others"] = other_temps(chips)
    state["mem"] = read_meminfo()
    state["load"] = read_loadavg()

    peaks = state["peaks"]
    if cpu_temp is not None:
        peaks["cpu"] = max(peaks.get("cpu", 0.0), cpu_temp)
    for g in gpus:
        if g.temp is not None:
            key = "gpu%d" % g.index
            peaks[key] = max(peaks.get(key, 0.0), g.temp)
    return cur_cpu


def new_state(interval):
    return {
        "gpus": [], "gpu_err": None, "cpu_pct": {}, "cpu_temp": None,
        "cpu_sensor": None, "others": [], "mem": (None, None), "load": None,
        "peaks": {}, "interval": interval, "cpu_model": cpu_model(),
        "ncpu": os.cpu_count() or 1,
    }


def run_tui(stdscr, interval):
    colored = init_colors()
    theme = Theme(colored)
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    stdscr.nodelay(True)
    stdscr.timeout(100)

    state = new_state(interval)
    sampler = GpuSampler(interval)
    sampler.start()

    prev_cpu = read_cpu_times()
    next_draw = 0.0
    try:
        while True:
            now = time.monotonic()
            if now >= next_draw:
                prev_cpu = collect(state, sampler, prev_cpu)
                draw(stdscr, theme, state)
                next_draw = now + state["interval"]

            ch = stdscr.getch()
            if ch == -1:
                continue
            if ch in (ord("q"), ord("Q"), 27):
                break
            if ch == curses.KEY_RESIZE:
                next_draw = 0.0
            elif ch in (ord("+"), ord("=")):
                state["interval"] = max(MIN_INTERVAL, round(state["interval"] - 0.25, 2))
                sampler.interval = state["interval"]
                sampler.poke()
                next_draw = 0.0
            elif ch in (ord("-"), ord("_")):
                state["interval"] = min(MAX_INTERVAL, round(state["interval"] + 0.25, 2))
                sampler.interval = state["interval"]
                next_draw = 0.0
            elif ch in (ord("r"), ord("R")):
                state["peaks"].clear()
                next_draw = 0.0
    finally:
        sampler.stop()


def run_once():
    """Plain-text snapshot: handy for logging or piping."""
    state = new_state(0.0)
    prev = read_cpu_times()
    time.sleep(0.25)
    chips = read_hwmon()
    pct = cpu_percent(prev, read_cpu_times())
    gpus, err = query_gpus()
    cpu_temp, cpu_sensor = pick_cpu_temp(chips)
    mem_used, mem_total = read_meminfo()

    print("sysmon %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    if not gpus:
        print("  GPU    %s" % (err or "none detected"))
    for g in gpus:
        print("  GPU %d  %s" % (g.index, g.name))
        print("    util %s  temp %s  vram %s  power %s" % (
            "%.0f%%" % g.util if g.util is not None else "n/a",
            "%.0fC" % g.temp if g.temp is not None else "n/a",
            "%.0f/%.0f MiB" % (g.mem_used, g.mem_total) if g.mem_total else "n/a",
            "%.0f W" % g.power if g.power is not None else "n/a",
        ))
    print("  CPU    %s" % state["cpu_model"])
    print("    util %s  temp %s%s" % (
        "%.0f%%" % pct["cpu"] if "cpu" in pct else "n/a",
        "%.0fC" % cpu_temp if cpu_temp is not None else "n/a",
        "  (%s)" % cpu_sensor if cpu_sensor else "",
    ))
    if mem_total:
        print("  MEM    %.1f/%.1f GiB" % (gib(mem_used), gib(mem_total)))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="sysmon", description="One dashboard for GPU usage and system temperatures.")
    parser.add_argument("-n", "--interval", type=float, default=1.0,
                        help="refresh seconds (default 1.0)")
    parser.add_argument("--once", action="store_true",
                        help="print a single plain-text snapshot and exit")
    args = parser.parse_args(argv)

    if args.once:
        return run_once()

    interval = max(MIN_INTERVAL, min(MAX_INTERVAL, args.interval))
    if not sys.stdout.isatty():
        print("sysmon: not a terminal; use --once for plain output", file=sys.stderr)
        return 2
    locale.setlocale(locale.LC_ALL, "")
    try:
        curses.wrapper(run_tui, interval)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
