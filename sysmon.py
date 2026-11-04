#!/usr/bin/env python3
"""sysmon - live GPU + system thermal dashboard for the terminal.

Zero third-party dependencies: reads `nvidia-smi` and /sys directly.

Thermal limits are discovered from the hardware at runtime -- the NVIDIA driver
reports each card's own target/slowdown/shutdown points, and hwmon exports
tempN_crit / tempN_max where the kernel driver supports it. Published vendor
specs are only used as a fallback (see REFERENCE and `sysmon --limits`).

Keys:  q quit   +/- refresh rate   r reset peaks   g graphs   c cores   d disk
"""

from __future__ import annotations

import argparse
import collections
import curses
import glob
import locale
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field

MIN_INTERVAL, MAX_INTERVAL = 0.25, 5.0
HISTORY = 512

# --- glyphs -----------------------------------------------------------------
BLOCK_FULL, BLOCK_EMPTY = "█", "─"
SPARK = " ▁▂▃▄▅▆▇█"
DEG = "°"
ARROW = "▸"

# Utilisation percentages have no vendor spec; these are the usual "the box is
# busy / the box is saturated" marks and only drive colour, never a warning.
UTIL_WARN, UTIL_CRIT = 80.0, 95.0
MEM_WARN, MEM_CRIT = 80.0, 92.0

# --- researched fallback limits ---------------------------------------------
# Only consulted when the hardware does not report its own limit. Sources are
# listed in README.md; degrees Celsius.
REFERENCE = {
    "nvidia": {
        "limit": 84.0, "shutdown": 95.0,
        "idle": "30-50", "load": "60-75",
        "note": "Ada/Ampere boost eases off near 83-84C; Tjmax 88-93C",
    },
    "amd-zen": {
        "limit": 95.0, "shutdown": 95.0,
        "idle": "35-50", "load": "60-85",
        "note": "Tctl Tjmax 95C; Zen boosts to its thermal budget by design",
    },
    "intel-core": {
        "limit": 100.0, "shutdown": 100.0,
        "idle": "30-50", "load": "60-90",
        "note": "Tjmax typically 100C; throttles rather than degrades",
    },
    "cpu-generic": {
        "limit": 90.0, "shutdown": 95.0,
        "idle": "30-50", "load": "55-80",
        "note": "conservative default for an unrecognised CPU",
    },
    "nvme": {
        "limit": 70.0, "shutdown": 85.0,
        "idle": "30-45", "load": "45-65",
        "note": "most consumer drives are rated to 70C and throttle by 75-85C",
    },
    "board": {
        "limit": 75.0, "shutdown": 90.0,
        "idle": "25-40", "load": "40-60",
        "note": "chipset/VRM/board sensors; sustained >75C is worth a look",
    },
}

# Match on /proc/cpuinfo "model name" to pick a fallback profile.
CPU_PROFILE_HINTS = (
    ("ryzen", "amd-zen"), ("threadripper", "amd-zen"), ("epyc", "amd-zen"),
    ("athlon", "amd-zen"), ("amd", "amd-zen"),
    ("intel", "intel-core"), ("core(tm)", "intel-core"), ("xeon", "intel-core"),
)

# Verdict bands, expressed as degrees below the hardware's own throttle point.
# A card that throttles at 84C and a CPU that throttles at 95C then get bands
# that mean the same thing on both, without hardcoding either number.
BAND_COOL, BAND_NOMINAL, BAND_WARM = 35.0, 15.0, 6.0

LVL_COOL, LVL_NOMINAL, LVL_WARM, LVL_HOT, LVL_CRIT = 0, 1, 2, 3, 4
LVL_NAME = ("COOL", "NOMINAL", "WARM", "HOT", "THERMAL LIMIT")


@dataclass
class Limits:
    """What a sensor is allowed to reach, and where that number came from."""
    limit: float                 # sustained throttle / target point
    shutdown: float | None = None
    source: str = "spec table"
    profile: str = "cpu-generic"

    def level(self, temp):
        if temp is None:
            return None
        margin = self.limit - temp
        if margin <= 0:
            return LVL_CRIT
        if margin < BAND_WARM:
            return LVL_HOT
        if margin < BAND_NOMINAL:
            return LVL_WARM
        if margin < BAND_COOL:
            return LVL_NOMINAL
        return LVL_COOL

    def frac(self, temp):
        """Position on the heat axis: 0 at (limit - 45C), 1 at the limit."""
        if temp is None:
            return None
        floor = self.limit - 45.0
        return max(0.0, min(1.0, (temp - floor) / max(1.0, self.limit - floor)))


def _num(text):
    """Parse a sensor field, tolerating [N/A] and [Not Supported]."""
    text = (text or "").strip()
    if not text or text.startswith("[") or text.lower() in ("n/a", "unknown"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


# --- GPU --------------------------------------------------------------------

GPU_FIELDS = [
    "index", "name", "utilization.gpu", "temperature.gpu", "memory.used",
    "memory.total", "power.draw", "power.limit", "fan.speed", "clocks.gr",
    "clocks.mem",
]
# Supported since the 5xx drivers; degrees remaining before the thermal target.
GPU_FIELDS_EXTRA = ["temperature.gpu.tlimit"]


@dataclass
class Gpu:
    index: int
    name: str
    util: float | None = None
    temp: float | None = None
    mem_used: float | None = None
    mem_total: float | None = None
    power: float | None = None
    power_limit: float | None = None
    fan: float | None = None
    clk_gr: float | None = None
    clk_mem: float | None = None
    headroom: float | None = None       # C to the thermal target, from driver
    limits: Limits = field(default_factory=lambda: Limits(**_ref("nvidia")))


def _ref(profile):
    """Fallback Limits kwargs for a reference profile."""
    r = REFERENCE[profile]
    return {"limit": r["limit"], "shutdown": r["shutdown"],
            "source": "spec table", "profile": profile}


def _nvidia_smi(args, timeout=5):
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None, "nvidia-smi not found"
    try:
        proc = subprocess.run([exe] + args, capture_output=True, text=True,
                              timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return None, "nvidia-smi: %s" % exc.__class__.__name__
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        return None, detail[0] if detail else "nvidia-smi exit %d" % proc.returncode
    return proc.stdout, None


def query_gpus(want_extra=True):
    """Return (gpus, error, extra_ok). Older drivers reject the tlimit field."""
    fields = GPU_FIELDS + (GPU_FIELDS_EXTRA if want_extra else [])
    out, err = _nvidia_smi(["--query-gpu=" + ",".join(fields),
                            "--format=csv,noheader,nounits"])
    if out is None:
        if want_extra:
            gpus, err2, _ = query_gpus(want_extra=False)
            if gpus is not None:
                return gpus, err2, False
        return None, err, False

    gpus = []
    for line in out.strip().splitlines():
        f = [p.strip() for p in line.split(",")]
        if len(f) < len(GPU_FIELDS):
            continue
        g = Gpu(
            index=int(_num(f[0]) or 0), name=f[1],
            util=_num(f[2]), temp=_num(f[3]),
            mem_used=_num(f[4]), mem_total=_num(f[5]),
            power=_num(f[6]), power_limit=_num(f[7]),
            fan=_num(f[8]), clk_gr=_num(f[9]), clk_mem=_num(f[10]),
        )
        if want_extra and len(f) > len(GPU_FIELDS):
            g.headroom = _num(f[len(GPU_FIELDS)])
        gpus.append(g)
    return gpus, None, want_extra


_TLIMIT_RE = re.compile(r"^\s*(GPU|Memory)\s+(.*?)\s*:\s*(-?\d+)\s*C\s*$")


def query_gpu_limits():
    """Ask the driver for each card's own thermal limits.

    Newer drivers report T.Limit values as a *margin* against the card's max
    operating point rather than an absolute temperature, e.g.

        GPU Current Temp                : 82 C
        GPU T.Limit Temp                : 3 C     <- 3C of headroom left
        GPU Slowdown T.Limit Temp       : -2 C    <- 2C above that reference
        GPU Target Temperature          : 84 C    <- absolute

    so the reference point is (current + tlimit) and the other margins are
    subtracted from it. Older drivers report absolutes directly; both are
    handled. Returns {gpu_index: Limits}.
    """
    out, err = _nvidia_smi(["-q", "-d", "TEMPERATURE"])
    if out is None:
        return {}

    result, idx, cur = {}, -1, {}
    blocks = []
    for line in out.splitlines():
        if line.startswith("GPU ") and ":" in line and "Temp" not in line:
            blocks.append(cur := {})
        m = _TLIMIT_RE.match(line)
        if m and blocks:
            blocks[-1][m.group(1) + " " + m.group(2)] = float(m.group(3))

    for idx, vals in enumerate(blocks):
        current = vals.get("GPU Current Temp")
        tlimit = vals.get("GPU T.Limit Temp")
        target = vals.get("GPU Target Temperature")
        limit = shutdown = None
        source = "spec table"

        if current is not None and tlimit is not None:
            ref = current + tlimit          # absolute max operating point
            limit = ref - vals.get("GPU Max Operating T.Limit Temp", 0.0)
            sd = vals.get("GPU Shutdown T.Limit Temp")
            if sd is not None:
                shutdown = ref - sd
            source = "nvidia driver (T.Limit)"
        else:
            sd = vals.get("GPU Shutdown Temp")
            slow = vals.get("GPU Slowdown Temp")
            if slow or sd:
                limit, shutdown = slow or sd, sd
                source = "nvidia driver"

        if target is not None:
            # The target temperature is the point the fan curve defends, which
            # is the number a user actually cares about staying under.
            limit = min(target, limit) if limit else target
            source = "nvidia driver"
        if limit is None:
            continue
        result[idx] = Limits(limit=limit, shutdown=shutdown, source=source,
                             profile="nvidia")
    return result


class GpuSampler(threading.Thread):
    """Polls nvidia-smi off the UI thread so a slow call never stalls redraws."""

    LIMIT_REFRESH = 60.0    # limits are static; re-check occasionally anyway

    def __init__(self, interval):
        super().__init__(daemon=True)
        self.interval = interval
        self._lock = threading.Lock()
        self._gpus = []
        self._err = None
        self._limits = {}
        self._extra = True
        self._stop = threading.Event()
        self._wake = threading.Event()

    def run(self):
        next_limits = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            if now >= next_limits:
                lim = query_gpu_limits()
                with self._lock:
                    self._limits = lim
                next_limits = now + self.LIMIT_REFRESH
            gpus, err, extra = query_gpus(self._extra)
            with self._lock:
                self._extra = extra
                for g in (gpus or []):
                    g.limits = self._limits.get(g.index, g.limits)
                    if g.headroom is None and g.temp is not None:
                        g.headroom = g.limits.limit - g.temp
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


# --- CPU / memory / sensors -------------------------------------------------

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
        out[key] = (max(0.0, min(100.0, 100.0 * (d_total - d_idle) / d_total))
                    if d_total > 0 else 0.0)
    return out


def _read_float(path, scale=1000.0):
    try:
        with open(path) as fh:
            return float(fh.read().strip()) / scale
    except (OSError, ValueError):
        return None


def read_hwmon():
    """chip name -> [(label, celsius, Limits|None), ...] for every temp input.

    The kernel exports tempN_crit / tempN_max / tempN_emergency where the chip
    driver knows them, so a real per-sensor limit is preferred over the table.
    """
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
            val = _read_float(inp)
            if val is None:
                continue
            crit = _read_float(base + "_crit")
            emerg = _read_float(base + "_emergency")
            mx = _read_float(base + "_max")
            limit = mx or crit
            lim = None
            if limit:
                lim = Limits(limit=limit, shutdown=emerg or crit or limit,
                             source="hwmon %s" % os.path.basename(hw),
                             profile=chip)
            entries.append((label, val, lim))
        if entries:
            chips.setdefault(chip, []).extend(entries)
    return chips


CPU_CHIP_PREF = ("k10temp", "zenpower", "coretemp")
CPU_LABEL_PREF = ("Tctl", "Tdie", "Package id 0", "CPU")
GPU_CHIPS = ("amdgpu", "nouveau")
DRIVE_CHIPS = ("nvme", "drivetemp")


def pick_cpu_temp(chips, fallback_profile):
    """Best-guess package temperature, its sensor name, and its limits."""
    def finish(chip, label, val, lim):
        return val, "%s/%s" % (chip, label), lim or Limits(**_ref(fallback_profile))

    for chip in CPU_CHIP_PREF:
        entries = chips.get(chip)
        if not entries:
            continue
        for want in CPU_LABEL_PREF:
            for label, val, lim in entries:
                if label == want:
                    return finish(chip, label, val, lim)
        label, val, lim = max(entries, key=lambda e: e[1])
        return finish(chip, label, val, lim)
    for chip, entries in chips.items():
        if chip in GPU_CHIPS:
            continue
        label, val, lim = max(entries, key=lambda e: e[1])
        return finish(chip, label, val, lim)
    return None, None, Limits(**_ref(fallback_profile))


def cpu_die_temps(chips):
    """Per-CCD / per-core dies, which k10temp exposes alongside the package."""
    out = []
    for chip in CPU_CHIP_PREF:
        for label, val, _lim in chips.get(chip, []):
            if label not in CPU_LABEL_PREF and re.search(r"cc[dx]|core", label, re.I):
                out.append((label, val))
    return out


def other_temps(chips):
    """Non-CPU, non-GPU sensors worth showing (drives, chipset, board)."""
    skip = set(CPU_CHIP_PREF) | set(GPU_CHIPS)
    out = []
    for chip, entries in sorted(chips.items()):
        if chip in skip:
            continue
        profile = "nvme" if any(d in chip for d in DRIVE_CHIPS) else "board"
        for label, val, lim in entries:
            if label in CPU_LABEL_PREF:
                continue
            out.append(("%s %s" % (chip, label), val, lim or Limits(**_ref(profile))))
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
        return None, None, None, None
    total = info.get("MemTotal")
    avail = info.get("MemAvailable", info.get("MemFree"))
    if total is None or avail is None:
        return None, None, None, None
    swap_total = info.get("SwapTotal") or 0.0
    swap_used = swap_total - (info.get("SwapFree") or 0.0)
    return total - avail, total, swap_used, swap_total


# --- disk usage -------------------------------------------------------------
# Fullness has no vendor spec; these are the usual "getting tight / nearly out
# of space" marks and only drive colour, never a thermal warning.
DISK_WARN, DISK_CRIT = 80.0, 92.0

# Pseudo / virtual filesystems whose capacity means nothing on a dashboard.
SKIP_FSTYPES = frozenset((
    "proc", "sysfs", "devtmpfs", "devpts", "tmpfs", "cgroup", "cgroup2",
    "pstore", "securityfs", "debugfs", "tracefs", "configfs", "fusectl",
    "mqueue", "hugetlbfs", "bpf", "autofs", "efivarfs", "ramfs", "squashfs",
    "overlay", "binfmt_misc", "rpc_pipefs", "nsfs", "fuse.gvfsd-fuse",
    "fuse.portal",
))

_OCTAL_RE = re.compile(r"\\([0-7]{3})")


def read_disks():
    """Mounted real filesystems and their usage.

    Returns [(mount, used_bytes, total_bytes, percent), ...], one entry per
    backing device (so a device mounted twice is only counted once), sorted by
    mount point. Pseudo filesystems and anything not backed by /dev are skipped.
    """
    seen, out = set(), []
    try:
        with open("/proc/mounts") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return out
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        dev, mount, fstype = parts[0], parts[1], parts[2]
        if fstype in SKIP_FSTYPES or not dev.startswith("/dev/"):
            continue
        if dev in seen:
            continue
        seen.add(dev)
        # /proc/mounts escapes space/tab/newline in the path as octal \NNN.
        mount = _OCTAL_RE.sub(lambda m: chr(int(m.group(1), 8)), mount)
        try:
            usage = shutil.disk_usage(mount)
        except OSError:
            continue
        if usage.total <= 0:
            continue
        pct = 100.0 * usage.used / usage.total
        out.append((mount, float(usage.used), float(usage.total), pct))
    out.sort(key=lambda d: d[0])
    return out


def read_loadavg():
    try:
        with open("/proc/loadavg") as fh:
            p = fh.read().split()
        return float(p[0]), float(p[1]), float(p[2])
    except (OSError, ValueError, IndexError):
        return None


def read_uptime():
    try:
        with open("/proc/uptime") as fh:
            secs = float(fh.read().split()[0])
    except (OSError, ValueError, IndexError):
        return ""
    d, rem = divmod(int(secs), 86400)
    h, rem = divmod(rem, 3600)
    return ("%dd %dh" % (d, h)) if d else ("%dh %dm" % (h, rem // 60))


def cpu_model():
    try:
        with open("/proc/cpuinfo") as fh:
            for line in fh:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return "CPU"


def cpu_profile(model):
    low = model.lower()
    for hint, profile in CPU_PROFILE_HINTS:
        if hint in low:
            return profile
    return "cpu-generic"


def gib(n):
    return n / (1024.0 ** 3)


# --- colour -----------------------------------------------------------------
# Cyan -> green -> yellow -> orange -> red. Cells are coloured by where they sit
# on the axis, so a bar's tip colour is the reading's actual severity.
RAMP_256 = [51, 45, 39, 44, 43, 49, 48, 46, 82, 118, 154, 190, 226, 220, 214,
            208, 202, 196]
RAMP_8 = [curses.COLOR_CYAN, curses.COLOR_CYAN, curses.COLOR_GREEN,
          curses.COLOR_GREEN, curses.COLOR_GREEN, curses.COLOR_YELLOW,
          curses.COLOR_YELLOW, curses.COLOR_RED, curses.COLOR_RED]

# UI chrome, as (256-colour, 8-colour) pairs.
CHROME = {
    "frame": (24, curses.COLOR_BLUE),
    "title": (51, curses.COLOR_CYAN),
    "label": (252, curses.COLOR_WHITE),
    "dim": (243, curses.COLOR_BLUE),
    "track": (236, curses.COLOR_BLUE),
    "accent": (207, curses.COLOR_MAGENTA),
    "value": (255, curses.COLOR_WHITE),
    "crit": (196, curses.COLOR_RED),
}
LEVEL_RAMP = (0.10, 0.35, 0.62, 0.82, 1.0)   # verdict -> ramp position


class Theme:
    """Allocates colour pairs once and hands out curses attributes."""

    def __init__(self):
        self.colored = False
        self._ramp = []
        self._chrome = {}
        if not curses.has_colors():
            return
        curses.start_color()
        try:
            curses.use_default_colors()
            bg = -1
        except curses.error:
            bg = curses.COLOR_BLACK
        wide = curses.COLORS >= 256
        ramp = RAMP_256 if wide else RAMP_8
        pair = 1
        for color in ramp:
            try:
                curses.init_pair(pair, color, bg)
            except curses.error:
                break
            self._ramp.append(curses.color_pair(pair))
            pair += 1
        for name, (c256, c8) in CHROME.items():
            try:
                curses.init_pair(pair, c256 if wide else c8, bg)
                self._chrome[name] = curses.color_pair(pair)
            except curses.error:
                self._chrome[name] = curses.A_NORMAL
            pair += 1
        self.colored = bool(self._ramp)

    def ramp(self, pos, bold=False):
        """Attribute for a 0..1 position on the heat ramp."""
        if not self._ramp:
            return curses.A_BOLD if (bold or (pos or 0) > 0.8) else curses.A_NORMAL
        pos = 0.0 if pos is None else max(0.0, min(1.0, pos))
        attr = self._ramp[min(len(self._ramp) - 1, int(pos * len(self._ramp)))]
        return attr | curses.A_BOLD if (bold or pos >= 0.8) else attr

    def level(self, lvl, bold=False):
        if lvl is None:
            return self.dim
        return self.ramp(LEVEL_RAMP[lvl], bold or lvl >= LVL_HOT)

    def load(self, pct, bold=False):
        """Utilisation ramp: stays cool until it is genuinely busy."""
        if pct is None:
            return self.dim
        f = max(0.0, min(1.0, pct / 100.0))
        pos = f * 0.625 if f < 0.8 else 0.5 + (f - 0.8) / 0.2 * 0.5
        return self.ramp(pos, bold)

    def _c(self, name, bold=False):
        attr = self._chrome.get(name, curses.A_NORMAL)
        return attr | curses.A_BOLD if bold else attr

    frame = property(lambda s: s._c("frame"))
    title = property(lambda s: s._c("title", True))
    label = property(lambda s: s._c("label"))
    dim = property(lambda s: s._c("dim"))
    track = property(lambda s: s._c("track"))
    accent = property(lambda s: s._c("accent", True))
    value = property(lambda s: s._c("value", True))
    crit = property(lambda s: s._c("crit", True))


# --- render helpers ---------------------------------------------------------

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


def _merge(cells):
    """Collapse (char, attr) cells into the fewest possible segments."""
    segs = []
    for ch, attr in cells:
        if segs and segs[-1][1] == attr:
            segs[-1][0] += ch
        else:
            segs.append([ch, attr])
    return [(t, a) for t, a in segs]


def heat_bar(theme, frac, width, kind="heat", pct=None):
    """Gradient bar: every filled cell is coloured by its own axis position."""
    width = max(1, width)
    frac = 0.0 if frac is None else max(0.0, min(1.0, frac))
    filled = int(round(frac * width))
    cells = []
    for i in range(width):
        if i >= filled:
            cells.append((BLOCK_EMPTY, theme.track))
            continue
        pos = (i + 0.5) / width
        if kind == "load":
            attr = theme.load(pos * 100.0, bold=(i == filled - 1))
        else:
            attr = theme.ramp(pos, bold=(i == filled - 1))
        cells.append((BLOCK_FULL, attr))
    return _merge(cells)


def sparkline(theme, values, width, limits=None, kind="heat"):
    """Recent history as coloured blocks, scaled against the thermal axis."""
    if not values:
        return []
    data = list(values)[-width:]
    if limits is not None:
        lo, hi = limits.limit - 45.0, limits.limit
    else:
        lo, hi = 0.0, 100.0
    span = max(1e-6, hi - lo)
    cells = []
    for v in data:
        f = max(0.0, min(1.0, (v - lo) / span))
        ch = SPARK[max(1, min(len(SPARK) - 1, int(round(f * (len(SPARK) - 1)))))]
        attr = theme.load(f * 100.0) if kind == "load" else theme.ramp(f)
        cells.append((ch, attr))
    pad = width - len(cells)
    return ([(" " * pad, theme.dim)] if pad > 0 else []) + _merge(cells)


LABEL_W, VALUE_W = 5, 11


def meter(theme, label, frac, value, inner_w, kind="heat", note=None,
          note_attr=None, value_attr=None):
    """One labelled gradient bar, sized so the trailing note still fits."""
    note_text = ("  " + note) if note else ""
    fixed = 2 + LABEL_W + 2 + VALUE_W + len(note_text)
    bar_w = max(6, inner_w - fixed)
    segs = [("  " + label.ljust(LABEL_W), theme.label)]
    segs += heat_bar(theme, frac, bar_w, kind)
    segs.append(("  " + value.ljust(VALUE_W), value_attr or theme.value))
    if note_text:
        segs.append((note_text, note_attr or theme.dim))
    return segs


def badge(theme, lvl, phase):
    """Right-hand status chip; the critical one pulses so it is unmissable."""
    text = " %s " % LVL_NAME[lvl]
    attr = theme.level(lvl, bold=True)
    if lvl >= LVL_CRIT and phase % 2:
        attr |= curses.A_REVERSE
    return "[%s]" % text, attr


def right_align(segs, inner_w, tail, tail_attr):
    """Append tail so it ends flush with the right edge of the panel."""
    used = sum(len(t) for t, _ in segs)
    pad = inner_w - used - len(tail)
    if pad < 1:
        return segs
    return segs + [(" " * pad, curses.A_NORMAL), (tail, tail_attr)]


# --- layout -----------------------------------------------------------------

def build_rows(theme, state, inner_w, inner_h):
    """Core rows first, then optional detail while vertical space remains."""
    gpus, gpu_err = state["gpus"], state["gpu_err"]
    hist, phase = state["hist"], state["phase"]
    wide = inner_w >= 72
    rows, optional = [], []

    if not gpus:
        rows.append([("GPU  ", theme.title),
                     (gpu_err or "no NVIDIA GPU detected", theme.dim)])
        rows.append(None)

    for g in gpus:
        key = "gpu%d" % g.index
        tag = "GPU%d" % g.index if len(gpus) > 1 else "GPU"
        lvl = g.limits.level(g.temp)
        head = [(tag.ljust(5), theme.title), (g.name, theme.label)]
        if lvl is not None:
            text, attr = badge(theme, lvl, phase)
            head = right_align(head, inner_w, text, attr)
        rows.append(head)

        if g.temp is not None:
            peak = state["peaks"].get(key)
            note = ("peak %.0f%sC" % (peak, DEG)) if peak and wide else None
            rows.append(meter(theme, "TEMP", g.limits.frac(g.temp),
                              "%.0f%sC" % (g.temp, DEG), inner_w, "heat", note,
                              value_attr=theme.level(lvl, True)))
            if state["graphs"] and hist[key + ".temp"]:
                rows.append(graph_row(theme, hist[key + ".temp"], inner_w,
                                      g.limits, "heat"))
            head_txt = headroom_line(g.limits, g.headroom, wide)
            if head_txt:
                rows.append([("  " + " " * LABEL_W, theme.label),
                             (ARROW + " ", theme.accent), (head_txt, theme.dim)])
        if g.util is not None:
            rows.append(meter(theme, "LOAD", g.util / 100.0, "%.0f%%" % g.util,
                              inner_w, "load",
                              value_attr=theme.load(g.util, True)))
        if g.mem_used is not None and g.mem_total:
            pct = 100.0 * g.mem_used / g.mem_total
            rows.append(meter(theme, "VRAM", g.mem_used / g.mem_total,
                              "%.1f/%.1fG" % (g.mem_used / 1024.0,
                                              g.mem_total / 1024.0),
                              inner_w, "load", note="%.0f%%" % pct if wide else None,
                              value_attr=theme.load(pct, True)))
        if g.power is not None:
            lim = g.power_limit or 0.0
            rows.append(meter(theme, "PWR", (g.power / lim) if lim else None,
                              "%.0f/%.0fW" % (g.power, lim) if lim
                              else "%.0fW" % g.power, inner_w, "load",
                              value_attr=theme.value))
        extras = []
        if g.fan is not None:
            extras.append("fan %.0f%%" % g.fan)
        if g.clk_gr is not None:
            extras.append("core %.0f MHz" % g.clk_gr)
        if g.clk_mem is not None:
            extras.append("vram %.0f MHz" % g.clk_mem)
        if extras and inner_h >= 18:
            rows.append([("  " + "   ".join(extras), theme.dim)])
        rows.append(None)

    # --- CPU ---
    ctemp, csensor, clim = state["cpu_temp"], state["cpu_sensor"], state["cpu_limits"]
    lvl = clim.level(ctemp)
    head = [("CPU".ljust(5), theme.title), (state["cpu_model"], theme.label)]
    if lvl is not None:
        text, attr = badge(theme, lvl, phase)
        head = right_align(head, inner_w, text, attr)
    rows.append(head)

    if ctemp is not None:
        peak = state["peaks"].get("cpu")
        note = ("peak %.0f%sC" % (peak, DEG)) if peak and wide else None
        rows.append(meter(theme, "TEMP", clim.frac(ctemp), "%.0f%sC" % (ctemp, DEG),
                          inner_w, "heat", note, value_attr=theme.level(lvl, True)))
        if state["graphs"] and hist["cpu.temp"]:
            rows.append(graph_row(theme, hist["cpu.temp"], inner_w, clim, "heat"))
        head_txt = headroom_line(clim, clim.limit - ctemp, wide)
        if head_txt:
            rows.append([("  " + " " * LABEL_W, theme.label),
                         (ARROW + " ", theme.accent), (head_txt, theme.dim)])

    total = state["cpu_pct"].get("cpu")
    if total is not None:
        rows.append(meter(theme, "LOAD", total / 100.0, "%.0f%%" % total,
                          inner_w, "load", value_attr=theme.load(total, True)))

    strip = core_strip(theme, state, inner_w)
    if strip:
        rows.append(strip)

    load = state["load"]
    if load:
        per_core = load[0] / max(1, state["ncpu"])
        rows.append([("  " + "AVG".ljust(LABEL_W), theme.label),
                     ("%.2f %.2f %.2f" % load, theme.value),
                     ("   %d cores  %.0f%% saturated" % (state["ncpu"],
                                                         min(999, per_core * 100)),
                      theme.dim)])
    rows.append(None)

    mem_used, mem_total, swap_used, swap_total = state["mem"]
    if mem_used is not None:
        pct = 100.0 * mem_used / mem_total
        rows.append(meter(theme, "RAM", mem_used / mem_total,
                          "%.1f/%.1fG" % (gib(mem_used), gib(mem_total)),
                          inner_w, "load", note="%.0f%%" % pct if wide else None,
                          value_attr=theme.load(pct, True)))
        if swap_total and swap_used > 0:
            optional.append(meter(theme, "SWAP", swap_used / swap_total,
                                  "%.1f/%.1fG" % (gib(swap_used), gib(swap_total)),
                                  inner_w, "load"))

    # --- optional detail, shown while vertical space remains ---
    dies = state["dies"]
    if dies:
        optional.append(None)
        optional.append(rule(theme, "dies", inner_w))
        for label, val in dies[:4]:
            optional.append(sensor_row(theme, label, val, clim, inner_w))

    if state["others"]:
        optional.append(None)
        optional.append(rule(theme, "sensors", inner_w))
        for name, val, lim in state["others"][:8]:
            optional.append(sensor_row(theme, name, val, lim, inner_w))

    if state["show_disk"] and state["disks"]:
        optional.append(None)
        optional.append(rule(theme, "disks", inner_w))
        for mount, used, total, pct in state["disks"][:8]:
            optional.append(disk_row(theme, mount, used, total, pct, inner_w))

    if state["cores"] and state["cpu_pct"]:
        keys = sorted((k for k in state["cpu_pct"] if k != "cpu"),
                      key=lambda k: int(k[3:]) if k[3:].isdigit() else 0)
        per_line = max(1, inner_w // 18)
        optional.append(None)
        optional.append(rule(theme, "cores", inner_w))
        for i in range(0, len(keys), per_line):
            segs = []
            for k in keys[i:i + per_line]:
                val = state["cpu_pct"][k]
                segs.append((" %-3s" % k[3:], theme.dim))
                segs += heat_bar(theme, val / 100.0, 8, "load")
                segs.append((" %3.0f%%" % val, theme.load(val)))
            optional.append(segs)

    if csensor:
        optional.append(None)
        optional.append([("  limits from %s  %s  cpu sensor %s"
                          % (clim.source, ARROW, csensor), theme.dim)])

    for row in optional:
        if len(rows) + 1 > inner_h:
            break
        rows.append(row)
    return rows[:inner_h]


def rule(theme, label, inner_w):
    """A dim section divider, so optional detail reads as its own block."""
    head = "── %s " % label
    return [(head, theme.dim), ("─" * max(0, inner_w - len(head)), theme.frame)]


def headroom_line(limits, headroom, wide):
    """The number that actually matters: how far from throttling we are."""
    if headroom is None:
        return None
    if headroom <= 0.5:
        parts = ["AT the %.0f%sC limit -- expect throttling" % (limits.limit, DEG)]
    else:
        parts = ["%.0f%sC to limit %.0f%sC" % (headroom, DEG, limits.limit, DEG)]
    if wide and limits.shutdown and limits.shutdown > limits.limit:
        parts.append("shutdown %.0f%sC" % (limits.shutdown, DEG))
    if wide:
        parts.append(limits.source)
    return "  ".join(parts)


def headroom_text(limits, temp):
    """Plain-text headroom phrase for --once output."""
    if temp is None:
        return ""
    left = limits.limit - temp
    if left <= 0.5:
        return "at the %.0fC limit" % limits.limit
    return "%.0fC to limit %.0fC" % (left, limits.limit)


def sensor_row(theme, name, val, limits, inner_w):
    lvl = limits.level(val) if limits else None
    segs = [("  %-20s" % name[:20], theme.dim)]
    segs += heat_bar(theme, limits.frac(val) if limits else val / 100.0,
                     max(6, min(24, inner_w - 40)), "heat")
    segs.append(("  %.0f%sC" % (val, DEG), theme.level(lvl, True)))
    return segs


def disk_row(theme, mount, used, total, pct, inner_w):
    """One filesystem as a usage bar, coloured by how full it is."""
    name = mount if len(mount) <= 18 else "…" + mount[-17:]
    bar = max(6, min(24, inner_w - 44))
    segs = [("  %-18s" % name, theme.dim)]
    segs += heat_bar(theme, max(0.0, min(1.0, pct / 100.0)), bar, "load")
    segs.append(("  %5.1f/%-5.0fG" % (gib(used), gib(total)), theme.value))
    segs.append(("  %3.0f%%" % pct, theme.load(pct, True)))
    return segs


def core_strip(theme, state, inner_w):
    """Every logical CPU as one coloured column -- the whole package at a glance."""
    keys = sorted((k for k in state["cpu_pct"] if k != "cpu"),
                  key=lambda k: int(k[3:]) if k[3:].isdigit() else 0)
    if not keys or len(keys) + 3 + LABEL_W + 4 > inner_w:
        return None
    cells = []
    for k in keys:
        v = state["cpu_pct"][k]
        f = max(0.0, min(1.0, v / 100.0))
        ch = SPARK[max(1, min(len(SPARK) - 1, int(round(f * (len(SPARK) - 1)))))]
        cells.append((ch, theme.load(v)))
    return ([("  " + "CORES".ljust(LABEL_W) + " ", theme.label)] + _merge(cells)
            + [("  %d thr" % len(keys), theme.dim)])


def graph_row(theme, values, inner_w, limits, kind):
    """History strip under a temperature meter, on the same axis as its bar."""
    lo = limits.limit - 45.0 if limits else 0.0
    axis = "  %.0f-%.0f%sC" % (lo, limits.limit if limits else 100.0, DEG)
    width = max(8, inner_w - (2 + LABEL_W) - len(axis))
    segs = [("  " + " " * LABEL_W, theme.label)]
    segs += sparkline(theme, values, width, limits, kind)
    return segs + [(axis, theme.dim)]


# --- frame ------------------------------------------------------------------

def draw(stdscr, theme, state):
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    if h < 12 or w < 46:
        put(stdscr, 0, 0, "terminal too small (need 46x12)"[: max(0, w)],
            theme.crit, w)
        stdscr.noutrefresh()
        curses.doupdate()
        return

    host = "%s · %s · up %s" % (state["host"], state["kernel"], state["uptime"])
    clock = "%s · %.2fs" % (time.strftime("%H:%M:%S"), state["interval"])
    segs = [("┌─", theme.frame), ("[ SYSMON ]", theme.title)]
    used = 12 + 2                                   # brand + closing "─┐"
    parts = [(clock, theme.accent)]
    if used + len(host) + len(clock) + 8 <= w:
        parts.insert(0, (host, theme.dim))
    for text, attr in parts:
        if used + len(text) + 4 <= w:
            segs.append(("─" * 2 + " ", theme.frame))
            segs.append((text, attr))
            segs.append((" ", theme.frame))
            used += len(text) + 4
    segs.append(("─" * max(0, w - used) + "─┐", theme.frame))
    # push the trailing chrome right so the clock sits at the edge
    pad = w - sum(len(t) for t, _ in segs)
    if pad > 0:
        segs.insert(2, ("─" * pad, theme.frame))
    x = 0
    for text, attr in segs:
        x = put(stdscr, 0, x, text, attr, w)

    inner_w, inner_h = w - 3, h - 2
    rows = build_rows(theme, state, inner_w, inner_h)
    for i in range(inner_h):
        y = 1 + i
        put(stdscr, y, 0, "│", theme.frame, w)
        put(stdscr, y, w - 1, "│", theme.frame, w)
        if i < len(rows) and rows[i]:
            x = 1
            for text, attr in rows[i]:
                x = put(stdscr, y, x, text, attr, w - 1)

    keys = " q quit  ± rate  r reset  g graphs  c cores  d disk "
    if len(keys) + 4 > w:
        keys = " q +- r g c d "
    fill = w - 2 - len(keys)
    left = max(0, fill // 2)
    bottom = "└" + "─" * left + keys + "─" * max(0, fill - left) + "┘"
    put(stdscr, h - 1, 0, bottom[:w], theme.frame, w)
    put(stdscr, h - 1, 1 + left, keys, theme.dim, w)
    stdscr.noutrefresh()
    curses.doupdate()


# --- state ------------------------------------------------------------------

def collect(state, sampler, prev_cpu):
    gpus, gpu_err = sampler.snapshot()
    cur_cpu = read_cpu_times()
    chips = read_hwmon()
    cpu_temp, cpu_sensor, cpu_limits = pick_cpu_temp(chips, state["cpu_profile"])

    state["gpus"], state["gpu_err"] = gpus, gpu_err
    state["cpu_pct"] = cpu_percent(prev_cpu, cur_cpu) if prev_cpu else {}
    state["cpu_temp"], state["cpu_sensor"] = cpu_temp, cpu_sensor
    state["cpu_limits"] = cpu_limits
    state["dies"] = cpu_die_temps(chips)
    state["others"] = other_temps(chips)
    state["mem"] = read_meminfo()
    state["disks"] = read_disks()
    state["load"] = read_loadavg()
    state["uptime"] = read_uptime()
    state["phase"] += 1

    hist, peaks = state["hist"], state["peaks"]
    if cpu_temp is not None:
        peaks["cpu"] = max(peaks.get("cpu", 0.0), cpu_temp)
        hist["cpu.temp"].append(cpu_temp)
    if "cpu" in state["cpu_pct"]:
        hist["cpu.util"].append(state["cpu_pct"]["cpu"])
    for g in gpus:
        key = "gpu%d" % g.index
        if g.temp is not None:
            peaks[key] = max(peaks.get(key, 0.0), g.temp)
            hist[key + ".temp"].append(g.temp)
        if g.util is not None:
            hist[key + ".util"].append(g.util)
    return cur_cpu


def new_state(interval):
    model = cpu_model()
    try:
        kernel = os.uname().release
        host = os.uname().nodename
    except OSError:
        kernel = host = "?"
    return {
        "gpus": [], "gpu_err": None, "cpu_pct": {}, "cpu_temp": None,
        "cpu_sensor": None, "cpu_limits": Limits(**_ref(cpu_profile(model))),
        "cpu_profile": cpu_profile(model), "dies": [], "others": [],
        "mem": (None, None, None, None), "disks": [], "load": None, "peaks": {},
        "hist": collections.defaultdict(lambda: collections.deque(maxlen=HISTORY)),
        "interval": interval, "cpu_model": model, "ncpu": os.cpu_count() or 1,
        "host": host, "kernel": kernel, "uptime": read_uptime(),
        "graphs": True, "cores": False, "show_disk": True, "phase": 0,
    }


def run_tui(stdscr, interval):
    theme = Theme()
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
            elif ch in (ord("g"), ord("G")):
                state["graphs"] = not state["graphs"]
                next_draw = 0.0
            elif ch in (ord("c"), ord("C")):
                state["cores"] = not state["cores"]
                next_draw = 0.0
            elif ch in (ord("d"), ord("D")):
                state["show_disk"] = not state["show_disk"]
                next_draw = 0.0
    finally:
        sampler.stop()


# --- non-interactive output -------------------------------------------------

def run_once():
    """Plain-text snapshot: handy for logging or piping."""
    state = new_state(0.0)
    prev = read_cpu_times()
    time.sleep(0.25)
    chips = read_hwmon()
    pct = cpu_percent(prev, read_cpu_times())
    gpus, err, _ = query_gpus()
    gpu_limits = query_gpu_limits()
    cpu_temp, cpu_sensor, cpu_lim = pick_cpu_temp(chips, state["cpu_profile"])
    mem_used, mem_total, _sw_u, _sw_t = read_meminfo()

    print("sysmon %s  %s up %s" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                   state["host"], state["uptime"]))
    if not gpus:
        print("  GPU    %s" % (err or "none detected"))
    for g in gpus or []:
        lim = gpu_limits.get(g.index, g.limits)
        lvl = lim.level(g.temp)
        print("  GPU %d  %s" % (g.index, g.name))
        print("    temp %s  [%s]  %s  util %s  vram %s  power %s" % (
            "%.0fC" % g.temp if g.temp is not None else "n/a",
            LVL_NAME[lvl] if lvl is not None else "n/a",
            headroom_text(lim, g.temp),
            "%.0f%%" % g.util if g.util is not None else "n/a",
            "%.0f/%.0f MiB" % (g.mem_used, g.mem_total) if g.mem_total else "n/a",
            "%.0f W" % g.power if g.power is not None else "n/a",
        ))
    lvl = cpu_lim.level(cpu_temp)
    print("  CPU    %s" % state["cpu_model"])
    print("    temp %s  [%s]  %s  util %s%s" % (
        "%.0fC" % cpu_temp if cpu_temp is not None else "n/a",
        LVL_NAME[lvl] if lvl is not None else "n/a",
        headroom_text(cpu_lim, cpu_temp),
        "%.0f%%" % pct["cpu"] if "cpu" in pct else "n/a",
        "  (%s)" % cpu_sensor if cpu_sensor else "",
    ))
    for name, val, lim in other_temps(chips)[:8]:
        print("    %-24s %.0fC  [%s]" % (name, val, LVL_NAME[lim.level(val)]))
    if mem_total:
        print("  MEM    %.1f/%.1f GiB" % (gib(mem_used), gib(mem_total)))
    disks = read_disks()
    if disks:
        print("  DISK")
        for mount, used, total, pct in disks:
            print("    %-20s %.1f/%.1f GiB  %3.0f%% used"
                  % (mount[:20], gib(used), gib(total), pct))
    return 0


def print_limits():
    """Show the limits actually in force, and where each number came from."""
    state = new_state(0.0)
    chips = read_hwmon()
    gpus, err, _ = query_gpus()
    gpu_limits = query_gpu_limits()
    _t, sensor, cpu_lim = pick_cpu_temp(chips, state["cpu_profile"])

    print("thermal limits in force\n")
    for g in gpus or []:
        lim = gpu_limits.get(g.index, g.limits)
        print("  GPU %d  %s" % (g.index, g.name))
        print("    throttle/target  %.0fC" % lim.limit)
        if lim.shutdown:
            print("    shutdown         %.0fC" % lim.shutdown)
        print("    source           %s" % lim.source)
    if not gpus:
        print("  GPU  %s" % (err or "none detected"))
    print("\n  CPU  %s" % state["cpu_model"])
    print("    throttle/Tjmax   %.0fC" % cpu_lim.limit)
    print("    source           %s%s"
          % (cpu_lim.source, "  (sensor %s)" % sensor if sensor else ""))
    for name, val, lim in other_temps(chips):
        print("\n  %s" % name)
        print("    limit            %.0fC   source %s" % (lim.limit, lim.source))

    print("\nverdict bands, relative to each sensor's own limit L:")
    print("  COOL           below L-%.0fC" % BAND_COOL)
    print("  NOMINAL        L-%.0fC .. L-%.0fC" % (BAND_COOL, BAND_NOMINAL))
    print("  WARM           L-%.0fC .. L-%.0fC" % (BAND_NOMINAL, BAND_WARM))
    print("  HOT            L-%.0fC .. L" % BAND_WARM)
    print("  THERMAL LIMIT  at or above L")

    print("\npublished reference figures (fallback only; see README):")
    print("  %-12s %-7s %-9s %-9s %s"
          % ("profile", "limit", "idle C", "load C", "note"))
    for name, r in REFERENCE.items():
        print("  %-12s %-7s %-9s %-9s %s"
              % (name, "%.0fC" % r["limit"], r["idle"], r["load"], r["note"]))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="sysmon",
        description="Live GPU and system thermal dashboard.")
    parser.add_argument("-n", "--interval", type=float, default=1.0,
                        help="refresh seconds (default 1.0)")
    parser.add_argument("--once", action="store_true",
                        help="print a single plain-text snapshot and exit")
    parser.add_argument("--limits", action="store_true",
                        help="show the thermal limits in force and their source")
    args = parser.parse_args(argv)

    if args.limits:
        return print_limits()
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
