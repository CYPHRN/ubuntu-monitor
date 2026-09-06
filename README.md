# sysmon

One-pane GPU + system temperature dashboard. No dependencies beyond `python3` and `nvidia-smi`.

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
sysmon --help          # usage
```

## Keys

```
q      quit
+      refresh faster (-0.25s)
-      refresh slower (+0.25s)
r      reset peak temperatures
```

## Logging

```bash
sysmon --once >> ~/sysmon.log                          # append one snapshot
watch -n 60 'sysmon --once >> ~/sysmon.log'            # snapshot every minute
while :; do sysmon --once; sleep 60; done > ~/sysmon.log
```

## Raw sources

```bash
nvidia-smi                                             # full GPU detail
nvidia-smi -l 2                                        # GPU, refreshed every 2s
nvidia-smi --query-gpu=timestamp,temperature.gpu,utilization.gpu,power.draw --format=csv -l 5
cat /sys/class/hwmon/hwmon*/name                       # available sensor chips
cat /proc/loadavg                                      # load average
```

## Tuning

```bash
grep -n '_WARN\|_CRIT' sysmon.py                       # show colour thresholds
sed -i 's/^CPU_TEMP_WARN, CPU_TEMP_CRIT = .*/CPU_TEMP_WARN, CPU_TEMP_CRIT = 75.0, 88.0/' sysmon.py
```

## Uninstall

```bash
rm ~/.local/bin/sysmon
```
