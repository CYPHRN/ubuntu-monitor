# Bitcoin Core as a system service

This runs `bitcoind` headless (daemon mode) at boot as a **system** service, so
it starts with the machine, is independent of any desktop login, and can be
moved to another device by editing a few paths.

It is a system service (`/etc/systemd/system/`), **not** a user service, on
purpose: the OOM-protection and IO/CPU throttling in the unit only take effect
under `system.slice`. The older GUI unit
(`~/.config/systemd/user/bitcoin-qt.service`) was a user service and its
`OOMScoreAdjust` / `IOWeight` settings were silently ignored; promoting to a
system service is what makes them real.

## Files

| File | Purpose |
| --- | --- |
| `systemd/bitcoind.service` | the unit (version-controlled; copied to `/etc/systemd/system/`) |
| `systemd/install-bitcoind.sh` | installs the unit, disables the conflicting GUI autostart, enables + starts the node |

## Install

Run as your normal user (it uses `sudo` only for the privileged steps):

```bash
./systemd/install-bitcoind.sh              # install, enable at boot, start now
                                           # (cleanly takes over a running bitcoind)
./systemd/install-bitcoind.sh --no-start   # install + enable, leave the running
                                           # node alone; it takes over next reboot
./systemd/install-bitcoind.sh --disabled   # install but leave OFF until you
                                           # turn it on (see "Turning it off")
```

## Turning it off

The service is just a normal systemd unit, so turning the autostart off is one
command:

```bash
sudo systemctl disable --now bitcoind      # stop now AND don't start at boot
sudo systemctl stop bitcoind               # stop now, leave enabled for boot
sudo systemctl start bitcoind              # start it again
sudo systemctl enable --now bitcoind       # turn boot autostart back on
```

To remove it entirely:

```bash
sudo systemctl disable --now bitcoind
sudo rm /etc/systemd/system/bitcoind.service
sudo systemctl daemon-reload
```

## Status

```bash
systemctl status bitcoind                  # service state
journalctl -u bitcoind -f                  # live log
bitcoin-cli -getinfo                       # node's own view (height, peers, sync)
```

The sysmon **BTC** panel reflects all of this live — see the README.

## The GUI autostart conflict

`bitcoind` and the Bitcoin Core GUI both lock the same datadir, so only one can
run. The installer therefore disables the user-level GUI autostart
(`bitcoin-qt.service`). If the daemon held the lock, the GUI unit would just
retry-fail every 30s.

To go back to the GUI instead, reverse it:

```bash
sudo systemctl disable --now bitcoind
systemctl --user enable --now bitcoin-qt.service
```

You can still launch `bitcoin-qt` by hand at any time *while the daemon is
stopped*.

## Moving to another device

The unit is written for this box. To port it, change these in
`systemd/bitcoind.service` (or override the variables at the top of
`install-bitcoind.sh`):

| Setting | This box | Change to |
| --- | --- | --- |
| `User=` / `Group=` | `cypher` | the account that owns the datadir |
| `-conf=` path | `/home/cypher/.bitcoin/bitcoin.conf` | that account's conf |
| `RequiresMountsFor=` | `/mnt/storage/bitcoin` | the datadir's mount (drop it if the datadir is on the root filesystem) |
| `ExecStart` / `ExecStop` binary | `/usr/local/bin/bitcoind` · `bitcoin-cli` | `/usr/bin/...` if installed from a distro package |

The resource limits (`MemoryHigh=4G`, `MemoryMax=6G`, `IOWeight`, `CPUWeight`,
`Nice`) are tuned for a 15 GB desktop box whose datadir is on a shared
rotational LUKS volume. On a dedicated node with more RAM or an SSD datadir,
raise or drop them.

## Why these settings

- **`Type=simple`, no `-daemon`** — this `bitcoind` is not linked against
  `libsystemd`, so `Type=notify` would hang waiting for a readiness signal that
  never comes. systemd tracks the foreground process directly.
- **`RequiresMountsFor=`** — stops bitcoind starting before the datadir volume
  is mounted, which would otherwise create a fresh datadir on the root disk.
- **oomd omit + `OOMScoreAdjust=-500`** — Ubuntu's `systemd-oomd` kills the
  heaviest-reclaim cgroup at 50% session pressure; it killed the old GUI unit
  twice during initial sync. These exempt the node.
- **`MemoryHigh=4G`** — a soft reclaim brake (never a kill) that also bounds the
  block-file page-cache churn that caused desktop stalls. `MemoryMax=6G` is a
  backstop.
- **`TimeoutStopSec=600`** — the UTXO cache must flush on shutdown; killing it
  mid-flush discards sync progress. On an HDD with `dbcache=1000` that flush can
  take minutes.
