# sysmon

One-pane GPU + system thermal dashboard. No dependencies beyond `python3` and
`nvidia-smi`.

Gradient heat bars, live history sparklines, a per-thread CPU strip, and a
verdict per sensor (`COOL` / `NOMINAL` / `WARM` / `HOT` / `THERMAL LIMIT`)
measured against limits read off the hardware itself.

## Install

```bash
chmod +x sysmon.py
ln -sf "$PWD/sysmon.py" ~/.local/bin/sysmon
```

## Run

```bash
sysmon                 # dashboard, 1s refresh
sysmon -n 0.5          # faster refresh (min 0.25)
sysmon -n 3            # slower refresh (max 5)
sysmon --once          # single plain-text snapshot
sysmon --limits        # show the thermal limits in force, and their source
sysmon --help          # usage
```

Needs a UTF-8, 256-colour terminal for the full look; it degrades to 8 colours
and stays readable down to 46x12.

## Keys

```
q      quit
+      refresh faster (-0.25s)
-      refresh slower (+0.25s)
r      reset peak temperatures
g      toggle history sparklines
c      toggle the per-core grid
d      toggle the disk panel
b      toggle the Bitcoin Core panel
```

## Bitcoin Core watch

If `bitcoin-cli` is on `PATH`, sysmon adds a **BTC** panel that shows at a glance
whether a local Bitcoin Core node is up:

```
BTC   Bitcoin Core                                              [ UP ]
  SYNC  ████████████████████████████████████████████  100.00%
  block 975450  ·  10 peers  ·  main  ·  891G on disk
```

* The chip reads `UP`, `SYNCING` (node answering but still in initial block
  download), `DOWN` (`bitcoind` not running), or `N/A` (binary not installed).
* Status is polled with `bitcoin-cli -getinfo`-style RPC calls
  (`getblockchaininfo` + `getconnectioncount`), throttled to once every ~5s and
  cached between refreshes so it never slows the dashboard.
* It reads **your own** `bitcoin.conf` for RPC credentials. Nothing leaves the
  machine; sysmon still makes no network calls.
* Press **`b`** to hide or show the panel. It only appears when `bitcoin-cli`
  exists, so boxes without Bitcoin Core are unaffected.

`sysmon --once` also prints a `BTC` line, so node up/down is greppable in logs.

To run the node itself as a boot service, see
[docs/bitcoin-service.md](docs/bitcoin-service.md).

## Where the limits come from

sysmon does not guess your thresholds. It asks the hardware, every run:

```bash
sysmon --limits                             # what it found, and from where
nvidia-smi -q -d TEMPERATURE                # the GPU numbers it parses

# the CPU/board numbers it parses
for f in /sys/class/hwmon/hwmon*/temp*_{label,crit,max}; do
  [ -e "$f" ] && echo "$f = $(cat "$f")"
done
```

* **GPU** — the NVIDIA driver reports each card's own target, slowdown and
  shutdown points. On recent drivers these come back as `T.Limit` *margins*
  rather than absolute temperatures: `GPU T.Limit Temp` is how many degrees are
  left before the target, so the absolute limit is `current + tlimit`, and the
  other margins are subtracted from that reference. sysmon handles both the
  margin and the older absolute form.
* **CPU / drives / board** — the kernel exports `tempN_crit`, `tempN_max` and
  `tempN_emergency` for chips whose driver knows them. `k10temp` (AMD) does
  not, so Ryzen falls back to the spec table below.
* **Fallback** — the published figures in `REFERENCE` in `sysmon.py`, used only
  when neither of the above answers.

Everything is read locally at runtime. sysmon makes no network calls.

## What temperature should you actually be at

Verdicts are relative to each sensor's own limit `L`, so the same band means the
same thing on a card that throttles at 84C and a CPU that throttles at 95C:

```
COOL            below L-35C
NOMINAL         L-35C .. L-15C          <- where you want to live under load
WARM            L-15C .. L-6C
HOT             L-6C .. L               <- boost is already backing off
THERMAL LIMIT   at or above L
```

Published reference figures, for when the hardware stays quiet:

| part | idle | under load | limit | notes |
| --- | --- | --- | --- | --- |
| NVIDIA (Ada/Ampere) | 30-50C | 60-75C | ~83-84C | boost eases off near the target temp; Tjmax 88-93C |
| AMD Ryzen (Zen 2/3/4) | 35-50C | 60-85C | 95C | Tctl Tjmax is 95C and Zen boosts *into* its thermal budget by design, so 80-90C under load is the CPU working as intended, not a fault |
| Intel Core | 30-50C | 60-90C | 100C | throttles rather than degrades |
| NVMe SSD | 30-45C | 45-65C | ~70C | most consumer drives are rated to 70C and throttle by 75-85C; check your model |
| chipset / VRM / board | 25-40C | 40-60C | ~75C | sustained above 75C is worth investigating |

Rules of thumb behind those numbers:

* Brief touches of the limit are fine and are what the boost algorithms are for.
  *Sustained* operation at the limit is the thing to fix, because you are paying
  for it in clocks.
* On a GPU, the number that matters is the headroom line sysmon prints
  (`Nc to limit`), not the raw temperature. A card sitting at 1-2C of headroom
  is throttling whatever the absolute reading says.
* A GPU core-to-hotspot delta above ~15-20C points at pump/paste/mounting rather
  than airflow.

Sources: [wepc RTX 4070 temperature guide](https://www.wepc.com/gpu/faq/what-temperature-should-the-rtx-4070-run-at/),
[Tom's Hardware — RTX 4070 Ti temps](https://forums.tomshardware.com/threads/rtx-4070ti-best-temps.3801000/),
[Guru3D RTX 4070 Ti review](https://www.guru3d.com/articles-pages/geforce-rtx-4070-ti-review,7.html),
[NVIDIA devtalk — what is GPU T.Limit Temp](https://forums.developer.nvidia.com/t/what-is-gpu-t-limit-temp/261642),
[nvidia-smi manual](https://docs.nvidia.com/deploy/nvidia-smi/index.html),
[Tom's Hardware — Ryzen 5 3600 temperatures](https://forums.tomshardware.com/threads/ryzen-5-3600-gaming-temps-at-95-degrees-c.3604295/),
[SilverPC — safe Ryzen 5 3600 operating temperatures](https://blog.silverpc.hu/2025/10/17/is-your-ryzen-5-3600-running-hot-a-guide-to-safe-operating-temperatures/),
[Evetech — safe NVMe temperatures](https://evezone.evetech.co.za/quick-bytes/safe-nvme-temperatures-hot-ssd-problem),
[Tom's Hardware — NVMe acceptable temps](https://forums.tomshardware.com/threads/nvme-ssd-acceptable-temps-for-controller-and-nand.3888563/).

## Logging

```bash
sysmon --once >> ~/sysmon.log                          # append one snapshot
watch -n 60 'sysmon --once >> ~/sysmon.log'            # snapshot every minute
while :; do sysmon --once; sleep 60; done > ~/sysmon.log
```

`--once` prints the verdict and headroom per sensor, so a log is greppable:

```bash
grep 'THERMAL LIMIT' ~/sysmon.log                      # when did anything peg its limit
```

## More sensors

Beyond the CPU package and the GPU, sysmon lists whatever else `hwmon` exposes
under `sensors`. If the drives and motherboard are missing there, the chips are
probably just not bound yet:

```bash
sudo apt install lm-sensors nvme-cli                   # sensor tooling
sudo sensors-detect                                    # bind board/VRM chips
sudo modprobe drivetemp                                # SATA drive temperatures
```

## Raw sources

```bash
nvidia-smi                                             # full GPU detail
nvidia-smi -l 2                                        # GPU, refreshed every 2s
nvidia-smi --query-gpu=timestamp,temperature.gpu,temperature.gpu.tlimit,power.draw --format=csv -l 5
cat /sys/class/hwmon/hwmon*/name                       # available sensor chips
cat /proc/loadavg                                      # load average
```

## Tuning

```bash
sysmon --limits                                        # check what is in force first
grep -n 'BAND_COOL\|BAND_NOMINAL\|BAND_WARM' sysmon.py # verdict band widths
grep -n 'REFERENCE = ' -A 32 sysmon.py                 # fallback spec table
```

## Uninstall

```bash
rm ~/.local/bin/sysmon
```
