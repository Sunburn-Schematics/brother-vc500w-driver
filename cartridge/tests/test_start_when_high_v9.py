"""Focused wait transition, timing and panic regressions; no hardware access."""

import io
import json
import unittest

import start_when_high_v9 as wait
import test_manage_emulator_v9 as fixture
from manage_esp32 import Audit, ManagementError, Session
from test_bench_eeprom import Tick


class WaitTests(unittest.TestCase):
    setUp = fixture.V9Tests.setUp
    start_frames = fixture.V9Tests.start_frames

    def control_wait(self, levels, *, panic=False):
        ready = []
        clock = Tick()
        frames = self.start_frames()
        polls = []
        for value in levels:
            frame = fixture.probe(fixture.current(self.data, unclean=False))[0]
            frame.update(levels=value, update_busy=False, recovery_required=False)
            polls.append(frame)
        frames[4:4] = polls
        info_calls = 0

        def callback(command):
            nonlocal info_calls
            if command == b"INFO\n":
                info_calls += 1
                if panic and info_calls == 2:
                    uart.data.extend(b"Guru Meditation Error: reset\n")

        uart = fixture.UART(frames, callback)

        def on_ready(value):
            self.assertEqual((self.output / "before-start.bin").read_bytes(), self.data)
            self.assertTrue((self.output / "sole-address-before-start.json").exists())
            self.assertFalse(any(x.startswith(b"EMU_START ") for x in uart.writes))
            ready.append(value)

        def sleep(seconds):
            clock.value += seconds

        control = wait.Control(
            Session(uart, Audit(io.StringIO()), clock),
            self.output,
            fixture.ELF,
            wait.v9.ADOPTION_SEED,
            wait_ms=1000,
            sleep=sleep,
            ready=on_ready,
        )
        return control, uart, ready

    def test_ready_prevalidated_and_high_sends_once_without_reprobe(self):
        control, uart, ready = self.control_wait([1, 3])
        result = control.run_start(1, True)
        self.assertEqual(result["status"], "started_experimental")
        self.assertEqual(len(ready), 1)
        self.assertEqual(control.wait_result["polls"], 2)
        self.assertLess(control.wait_result["high_to_start_send_begin_ms"], 1)
        self.assertEqual(uart.writes[-3:-1], [b"INFO\n", b"INFO\n"])
        self.assertTrue(uart.writes[-1].startswith(b"EMU_START "))
        self.assertEqual(sum(x.startswith(b"EMU_START ") for x in uart.writes), 1)

    def test_never_high_times_out_and_sends_no_start(self):
        control, uart, _ = self.control_wait([1] * 8)
        with self.assertRaises(ManagementError):
            control.run_start(1, True)
        self.assertFalse(any(x.startswith(b"EMU_START ") for x in uart.writes))
        summary = json.loads((self.output / "wait-summary.json").read_text())
        self.assertIsNone(summary["high_observed_monotonic"])
        self.assertLessEqual(summary["polls"], 4)

    def test_initial_high_is_not_a_low_to_high_transition(self):
        control, uart, _ = self.control_wait([3, 1, 3])
        control.run_start(1, True)
        self.assertEqual(control.wait_result["polls"], 3)
        self.assertEqual(sum(x.startswith(b"EMU_START ") for x in uart.writes), 1)

    def test_panic_while_waiting_is_saved_and_prevents_start(self):
        control, uart, _ = self.control_wait([1, 3], panic=True)
        with self.assertRaises(ManagementError):
            control.run_start(1, True)
        raw = b"".join(p.read_bytes() for p in self.output.glob("raw-line-*.bin"))
        self.assertIn(b"Guru Meditation Error: reset\n", raw)
        self.assertFalse(any(x.startswith(b"EMU_START ") for x in uart.writes))


if __name__ == "__main__":
    unittest.main()
