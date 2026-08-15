"""Silent background system monitor.

Watches battery, disk, RAM, CPU and temperature via psutil and raises an
alert exactly once per incident (rising-edge + re-arm on recovery), so
Jarvis warns you when it matters and never nags.

main.py passes `alert(msg)` which logs it and lets Jarvis phrase it aloud.
"""
import os
import threading
import time

import psutil

CHECK_INTERVAL = 30      # seconds between checks — knob
BATTERY_LOW    = 20      # % unplugged
BATTERY_CRIT   = 10
DISK_FULL      = 90      # % system drive
RAM_HIGH       = 92      # %
CPU_HIGH       = 90      # % sustained over CPU_SUSTAIN checks
CPU_SUSTAIN    = 3
TEMP_HIGH      = 85      # °C, when sensors exist (rare on Windows)


class SystemMonitor(threading.Thread):
    def __init__(self, alert, interval: float = CHECK_INTERVAL):
        super().__init__(daemon=True, name="SystemMonitor")
        self.alert     = alert
        self.interval  = interval
        self._fired: dict[str, bool] = {}
        self._cpu_runs = 0

    # fire on rising edge only; re-arm once the condition clears
    def _edge(self, key: str, active: bool, msg: str):
        if active and not self._fired.get(key):
            self._fired[key] = True
            self.alert(msg)
        elif not active:
            self._fired[key] = False

    def check(self):
        bat = psutil.sensors_battery()
        if bat is not None:
            unplugged = not bat.power_plugged
            self._edge("bat_crit", unplugged and bat.percent <= BATTERY_CRIT,
                       f"Battery critical at {bat.percent:.0f} percent. Plug in now.")
            self._edge("bat_low", unplugged and bat.percent <= BATTERY_LOW,
                       f"Battery at {bat.percent:.0f} percent and unplugged.")

        disk = psutil.disk_usage(os.path.abspath(os.sep))
        self._edge("disk", disk.percent >= DISK_FULL,
                   f"System drive is {disk.percent:.0f} percent full "
                   f"({disk.free / 1e9:.1f} GB free).")

        ram = psutil.virtual_memory().percent
        self._edge("ram", ram >= RAM_HIGH,
                   f"Memory usage is at {ram:.0f} percent. Things may get sluggish.")

        cpu = psutil.cpu_percent(interval=None)
        self._cpu_runs = self._cpu_runs + 1 if cpu >= CPU_HIGH else 0
        self._edge("cpu", self._cpu_runs >= CPU_SUSTAIN,
                   f"CPU has been pinned above {CPU_HIGH} percent for a while.")

        try:
            temps = psutil.sensors_temperatures()
            hottest = max((t.current for ts in temps.values() for t in ts), default=0)
            self._edge("temp", hottest >= TEMP_HIGH,
                       f"Hardware temperature reached {hottest:.0f} degrees.")
        except Exception:
            pass  # no sensors on this platform

    def run(self):
        psutil.cpu_percent(interval=None)  # prime the counter
        while True:
            time.sleep(self.interval)
            try:
                self.check()
            except Exception as e:  # noqa: BLE001 — monitor must never die
                print(f"[Monitor] check failed: {e}")


def demo():
    """Self-check: edge-trigger fires once, re-arms after recovery."""
    hits = []
    m = SystemMonitor(alert=hits.append)
    m._edge("k", True,  "first")
    m._edge("k", True,  "suppressed")
    m._edge("k", False, "recovered")
    m._edge("k", True,  "second")
    assert hits == ["first", "second"], hits
    m.check()  # real psutil pass must not raise
    print("monitor demo OK")


if __name__ == "__main__":
    demo()
