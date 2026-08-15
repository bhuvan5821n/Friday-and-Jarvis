"""Phase 3 tests: read-only status.

The formatting tests inject synthetic readings so they assert on behaviour, not
on whatever this laptop happens to be doing. One live test runs against the
real machine to prove the collector wiring holds.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from remote_control import status
from remote_control.status import (TEMPERATURE_UNAVAILABLE, format_status,
                                   snapshot, temperature, whats_happening)

FULL = {
    "host": "BHUVAN-LAPTOP", "persona": "jarvis", "uptime": "3h 12m",
    "cpu_percent": 14.0, "cpu_name": "Intel64 Family 6",
    "ram_percent": 61.0, "ram_detail": "9.8 / 16 GB",
    "gpu_percent": 7.0, "gpu_name": "NVIDIA RTX 3050",
    "storage_percent": 72.0, "storage_detail": "0.72 / 1.00 TB",
    "battery_percent": 84.0, "battery_plugged": True,
    "net_online": True, "net_down_mbps": 2.4, "net_up_mbps": 0.6,
    "temperature_celsius": None, "temperature_text": TEMPERATURE_UNAVAILABLE,
}

EMPTY = {k: None for k in FULL}
EMPTY.update({"host": "", "persona": "friday", "uptime": "Not available",
              "cpu_name": "", "ram_detail": "", "gpu_name": "",
              "storage_detail": "", "temperature_text": TEMPERATURE_UNAVAILABLE})


class TestHonesty(unittest.TestCase):

    def test_missing_temperature_uses_the_required_wording(self):
        self.assertIn(TEMPERATURE_UNAVAILABLE, format_status(FULL))

    def test_no_sensor_returns_none_not_a_number(self):
        with mock.patch("psutil.sensors_temperatures", create=True,
                        return_value={}):
            celsius, text = temperature()
        self.assertIsNone(celsius)
        self.assertEqual(text, TEMPERATURE_UNAVAILABLE)

    def test_a_real_sensor_is_reported_when_present(self):
        entry = mock.Mock(current=47.5)
        with mock.patch("psutil.sensors_temperatures", create=True,
                        return_value={"coretemp": [entry]}):
            celsius, text = temperature()
        self.assertEqual(celsius, 47.5)
        self.assertIn("48", text)

    def test_unmeasurable_figures_say_so_rather_than_showing_zero(self):
        text = format_status(EMPTY)
        for label in ("CPU", "RAM", "GPU", "Disk", "Battery", "Network"):
            line = next(l for l in text.splitlines() if l.startswith(label))
            self.assertIn("Not available", line, line)
        self.assertNotIn("0%", text)

    def test_zero_percent_is_a_real_reading_not_unavailable(self):
        data = dict(FULL, cpu_percent=0.0, gpu_percent=0.0)
        text = format_status(data)
        self.assertIn("CPU: 0%", text)
        self.assertIn("GPU: 0%", text)


class TestFormatting(unittest.TestCase):

    def test_every_figure_appears(self):
        text = format_status(FULL)
        for expected in ("BHUVAN-LAPTOP", "CPU: 14%", "RAM: 61%", "GPU: 7%",
                         "Disk: 72%", "Battery: 84%", "charging",
                         "Network: online", "3h 12m", "JARVIS"):
            self.assertIn(expected, text)

    def test_battery_state_distinguishes_unplugged_from_unknown(self):
        self.assertIn("on battery",
                      format_status(dict(FULL, battery_plugged=False)))
        self.assertNotIn("on battery",
                         format_status(dict(FULL, battery_plugged=None)))

    def test_offline_network_is_reported_plainly(self):
        self.assertIn("Network: offline",
                      format_status(dict(FULL, net_online=False)))

    def test_output_is_short_enough_for_one_whatsapp_message(self):
        self.assertLess(len(format_status(FULL)), 1200)

    def test_status_carries_no_secrets(self):
        text = format_status(snapshot()).lower()
        for leak in ("token", "password", "session", "credential", "api_key",
                     "whatsapp"):
            self.assertNotIn(leak, text)


class TestWhatsHappening(unittest.TestCase):

    def test_idle_assistant_says_so(self):
        reading = mock.Mock(state=None, model=None, task=None, served=None,
                            latency_ms=None, routing="Automatic")
        with mock.patch("friday.data.ai_reading", return_value=reading):
            self.assertIn("idle", whats_happening().lower())

    def test_active_routing_is_described(self):
        reading = mock.Mock(state="routing", model="gemini-2.0-flash",
                            task="chat", served="google", latency_ms=412,
                            routing="Automatic")
        with mock.patch("friday.data.ai_reading", return_value=reading):
            text = whats_happening()
        self.assertIn("gemini-2.0-flash", text)
        self.assertIn("412 ms", text)

    def test_unreadable_state_is_admitted_not_invented(self):
        with mock.patch("friday.data.ai_reading", side_effect=RuntimeError):
            self.assertIn("not readable", whats_happening())


class TestLiveCollector(unittest.TestCase):
    """Runs against the real machine — proves the wiring, not the numbers."""

    def test_snapshot_reads_the_shared_friday_collector(self):
        data = snapshot()
        self.assertIn("cpu_percent", data)
        self.assertIn("temperature_text", data)
        self.assertIn(data["persona"], ("jarvis", "friday"))

    def test_no_second_poller_is_started(self):
        """We must reuse FRIDAY's one collector, not spawn our own thread."""
        import friday.data
        before = friday.data.system
        snapshot()
        snapshot()
        self.assertIs(friday.data.system, before)

    def test_formats_without_raising_on_real_data(self):
        self.assertTrue(format_status().splitlines())

    def test_an_unpolled_collector_is_primed_rather_than_reported_empty(self):
        """Right after boot the timer has not fired; a running machine must
        not be described as entirely unmeasurable."""
        import friday.data
        friday.data.system._reading = friday.data.SystemReading()
        data = snapshot()
        self.assertIsNotNone(data["cpu_percent"])
        self.assertIsNotNone(data["ram_percent"])


class TestReadOnly(unittest.TestCase):

    def test_module_performs_no_writes_or_process_control(self):
        source = Path(status.__file__).read_text(encoding="utf-8")
        for forbidden in ("subprocess", "os.remove", "shutil.rmtree",
                          "open(", "write_text", "Popen", "os.system"):
            self.assertNotIn(forbidden, source, forbidden)


if __name__ == "__main__":
    unittest.main(verbosity=2)
