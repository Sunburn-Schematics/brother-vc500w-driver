"""Offline detached-diagnostic framing and immutable-state contract tests."""

from __future__ import annotations

import copy
import io
import json
import tempfile
import unittest
from pathlib import Path

import diagnose_emulator_v8 as diag
import test_manage_emulator as fixture
from manage_esp32 import Audit, ManagementError, Session
from test_bench_eeprom import FakeUART, Tick, valid_image

ELF = "d" * 64


def current_state(data: bytes) -> dict:
    result = fixture.device_state(data, live=1, durable=1, unclean=True, marker=True)
    result.update(
        firmware=diag.FIRMWARE,
        last_fault_code=0,
        reset_reason=4,
        attach_diagnostics={
            "stage": "not_attempted",
            "core": 0,
            "levels": 0,
            "output_enable": 0,
            "sda_mux": 0,
            "scl_mux": 0,
            "pins_input_only": True,
            "internal_pulls": False,
            "irq_error": 0,
            "gpio_error": 0,
        },
        boot_breadcrumb={"valid": False, "stage": 0, "stage_inverse": 0},
        isr_diagnostics={
            "isr_calls": 0,
            "last_irq_flags": 0,
            "last_rx_count": 0,
            "last_tx_count": 0,
            "irq_storms": 0,
            "empty_rx_irqs": 0,
        },
    )
    return result


def probe(current: dict) -> list[dict]:
    return [
        {
            "type": "status",
            "firmware": diag.FIRMWARE,
            "emulator_capability": diag.CAPABILITY,
            "cpu_mhz": 240,
            "sda_gpio": 21,
            "scl_gpio": 22,
            "armed": False,
            "pins_input_only": True,
            "internal_pulls": False,
        },
        {
            "type": "netinfo",
            "firmware": diag.FIRMWARE,
            "app_elf_sha256": ELF,
            "armed": False,
            "update_busy": False,
        },
        {"type": "emu_status", **current},
    ]


def registers() -> dict:
    return {
        "raw_interrupts": 1,
        "enabled_interrupts": 0,
        "masked_interrupts": 0,
        "hw_status": 0,
        "ctr": 3,
        "fifo_conf": 512,
        "rx_fifo": 0,
        "tx_fifo": 32,
        "levels": 3,
        "gpio_output_enable": 0,
        "sda_output_select": 1280,
        "scl_output_select": 1280,
    }


def transcript(data: bytes, current: dict) -> list[dict]:
    phases = [
        {"type": "emu_diag_phase", "op": "diag", "phase": name, **registers()}
        for name in diag.PHASES
    ]
    result = {
        "type": "emu_diag",
        "ok": True,
        "message": "detached_diagnostic_complete",
        **registers(),
        "one_shot_irq_count": 1,
        "observed_irq": 1,
        "observed_status": 0,
        "observation_ms": 5,
        "outputs_enabled": False,
        **current,
    }
    return [
        *probe(current),
        fixture.image_frame(data, current),
        *phases,
        result,
        fixture.image_frame(data, current),
        *probe(current),
    ]


class CommandUART(FakeUART):
    """Release replies only after a command; startup bytes are independent."""

    def __init__(self, frames, on_command=None):
        super().__init__([], on_command)
        self.pending_frames = copy.deepcopy(frames)
        self.diagnostic_prefix = b""
        self.diagnostic_suffix = b""

    def write(self, data):
        result = super().write(data)
        if data == b"EMU_DIAG DETACHED\n":
            self.data.extend(self.diagnostic_prefix)
            while self.pending_frames:
                frame = self.pending_frames.pop(0)
                self.data.extend((json.dumps(frame) + "\n").encode())
                if frame.get("type") == "emu_diag":
                    break
            self.data.extend(self.diagnostic_suffix)
        elif self.pending_frames:
            frame = self.pending_frames.pop(0)
            self.data.extend((json.dumps(frame) + "\n").encode())
        return result


class FragmentUART(CommandUART):
    def __init__(self, frames, on_command=None):
        super().__init__(frames, on_command)
        self.reads = 0

    def read(self, size=1):
        self.reads += 1
        if self.reads % 7 == 0:
            return b""
        return super().read(size)


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name)
        self.data = valid_image(remaining=6256)
        self.current = current_state(self.data)

    def control(self, frames, *, uart_class=CommandUART, callback=None):
        uart = uart_class(frames, callback)
        session = Session(
            uart, Audit(io.StringIO()), Tick(), expected_firmware=diag.FIRMWARE
        )
        return diag.DiagnosticControl(
            session, self.output, ELF, diag.digest(self.data)
        ), uart

    def test_fragmented_success_preserves_unclean_marker_and_complete_image(self):
        control, uart = self.control(
            transcript(self.data, self.current), uart_class=FragmentUART
        )
        result = control.run()
        self.assertEqual(result["status"], "detached_diagnostic_verified")
        self.assertEqual((self.output / "before-diag.bin").read_bytes(), self.data)
        self.assertEqual((self.output / "after-diag.bin").read_bytes(), self.data)
        self.assertTrue(result["after"]["unclean_boot"])
        self.assertTrue(result["after"]["session_active_marker"])
        self.assertEqual(result["after"]["durable_generation"], 1)
        self.assertEqual(uart.writes.count(b"EMU_DIAG DETACHED\n"), 1)
        self.assertFalse(
            any(
                word in value
                for value in uart.writes
                for word in (
                    b"START",
                    b"RECOVER",
                    b"ARM",
                    b"BENCH",
                    b"UPDATE",
                    b"WIFI",
                    b"STOP",
                )
            )
        )
        self.assertEqual(len(list(self.output.glob("raw-line-*.bin"))), 12)

    def test_evidence_and_snapshot_exist_before_single_diagnostic_command(self):
        def check(command):
            if command == b"EMU_DIAG DETACHED\n":
                self.assertTrue((self.output / "intent-before-command.json").exists())
                self.assertEqual(
                    (self.output / "before-diag.bin").read_bytes(), self.data
                )

        control, _ = self.control(transcript(self.data, self.current), callback=check)
        control.run()

    def test_buffered_startup_status_and_truncated_json_saved_before_fresh_probe(self):
        control, uart = self.control(transcript(self.data, self.current))
        startup = (json.dumps(probe(self.current)[0]) + "\n").encode()
        startup += b"rst:0xc software reset\n" + b'{"type":"status","unfinished":'
        uart.data.extend(startup)
        result = control.run()
        self.assertEqual(result["status"], "detached_diagnostic_verified")
        self.assertEqual(result["startup_capture"]["raw_lines_saved"], 3)
        self.assertEqual(result["startup_capture"]["json_frames_saved"], 1)
        self.assertTrue(result["startup_capture"]["partial_fragment_retained"])
        saved = b"".join(
            p.read_bytes() for p in sorted(self.output.glob("raw-line-00*.bin"))[:3]
        )
        self.assertEqual(saved, startup)
        self.assertEqual(uart.writes[:3], [b"INFO\n", b"NETINFO\n", b"EMU_STATUS\n"])
        self.assertEqual(uart.writes.count(b"EMU_DIAG DETACHED\n"), 1)
        self.assertEqual(
            json.loads((self.output / "frame-001.json").read_text())["type"], "status"
        )

    def test_damaged_complete_startup_json_retained_without_poisoning_probe(self):
        control, uart = self.control(transcript(self.data, self.current))
        raw = b'{"type": bad}\n["boot-only"]\n'
        uart.data.extend(raw)
        result = control.run()
        self.assertEqual(result["status"], "detached_diagnostic_verified")
        self.assertEqual(result["startup_capture"]["raw_lines_saved"], 2)
        self.assertEqual(result["startup_capture"]["json_frames_saved"], 0)
        self.assertEqual(
            (self.output / "raw-line-000.bin").read_bytes()
            + (self.output / "raw-line-001.bin").read_bytes(),
            raw,
        )
        self.assertEqual(uart.writes.count(b"EMU_DIAG DETACHED\n"), 1)

    def test_continuous_startup_hits_hard_bound_before_any_command(self):
        control, uart = self.control(transcript(self.data, self.current))
        elapsed = 0.0

        def clock():
            nonlocal elapsed
            elapsed += 0.001
            return elapsed

        control.session.clock = clock
        uart.data.extend(b"boot still busy " * 20000)
        with self.assertRaisesRegex(ManagementError, "bounded drain"):
            control.run()
        self.assertEqual(uart.writes, [])
        self.assertTrue(list(self.output.glob("raw-line-*-partial.bin")))
        failure = json.loads((self.output / "failure.json").read_text())
        self.assertFalse(failure["command_may_have_reached_device"])

    def test_chatter_after_quiet_drain_rejects_initial_probe_before_diagnostic(self):
        def inject(command):
            if command == b"INFO\n":
                uart.data.extend(b"rst: unexpected new boot\n")

        control, uart = self.control(
            transcript(self.data, self.current), callback=inject
        )
        with self.assertRaisesRegex(ManagementError, "startup quiet interval"):
            control.run()
        self.assertEqual(uart.writes, [b"INFO\n"])
        self.assertEqual(
            (self.output / "raw-line-000.bin").read_bytes(),
            b"rst: unexpected new boot\n",
        )
        self.assertTrue((self.output / "frame-000.json").exists())

    def test_wrong_elf_fails_before_diagnostic_and_retains_raw_frame(self):
        frames = transcript(self.data, self.current)
        frames[1]["app_elf_sha256"] = "a" * 64
        control, uart = self.control(frames)
        with self.assertRaises(ManagementError):
            control.run()
        self.assertNotIn(b"EMU_DIAG DETACHED\n", uart.writes)
        self.assertTrue((self.output / "frame-001.json").exists())

    def test_active_state_rejected_before_diagnostic(self):
        frames = transcript(self.data, self.current)
        frames[2]["session_open"] = True
        control, uart = self.control(frames)
        with self.assertRaises(ManagementError):
            control.run()
        self.assertNotIn(b"EMU_DIAG DETACHED\n", uart.writes)

    def test_phase_reordering_is_saved_then_rejected_without_retry(self):
        frames = transcript(self.data, self.current)
        frames[4], frames[5] = frames[5], frames[4]
        control, uart = self.control(frames)
        with self.assertRaises(ManagementError):
            control.run()
        self.assertEqual(
            json.loads((self.output / "frame-004.json").read_text())["phase"],
            "fifo_loaded",
        )
        self.assertEqual(uart.writes.count(b"EMU_DIAG DETACHED\n"), 1)

    def test_timeout_keeps_boot_lines_and_partial_panic_bytes(self):
        control, uart = self.control(transcript(self.data, self.current)[:5])
        uart.diagnostic_suffix = b"Guru Meditation Error\nBacktrace: 0x4008"
        with self.assertRaises(ManagementError):
            control.run()
        raw = b"".join(
            p.read_bytes() for p in sorted(self.output.glob("raw-line-*.bin"))
        )
        self.assertIn(b"Guru Meditation Error\n", raw)
        self.assertTrue(raw.endswith(b"Backtrace: 0x4008"))
        failure = json.loads((self.output / "failure.json").read_text())
        self.assertTrue(failure["command_may_have_reached_device"])
        self.assertEqual(uart.writes.count(b"EMU_DIAG DETACHED\n"), 1)

    def test_complete_boot_chatter_during_diagnostic_prevents_success(self):
        control, uart = self.control(transcript(self.data, self.current))
        uart.diagnostic_prefix = b"rst:0x8 watchdog\n"
        with self.assertRaises(ManagementError):
            control.run()
        self.assertEqual(control.reader.chatter, 1)
        self.assertFalse((self.output / "receipt.json").exists())
        self.assertTrue((self.output / "after-diag.bin").exists())

    def test_changed_complete_after_image_is_retained_before_rejection(self):
        changed = valid_image(remaining=6200)
        frames = transcript(self.data, self.current)
        changed_state = current_state(changed)
        frames[8] = fixture.image_frame(changed, changed_state)
        control, _ = self.control(frames)
        with self.assertRaises(ManagementError):
            control.run()
        self.assertEqual((self.output / "after-diag.bin").read_bytes(), changed)

    def test_metadata_clear_is_not_accepted_as_diagnostic_success(self):
        frames = transcript(self.data, self.current)
        frames[7]["session_active_marker"] = False
        control, _ = self.control(frames)
        with self.assertRaises(ManagementError):
            control.run()

    def test_peripheral_output_selection_rejected_even_if_gpio_enable_zero(self):
        frames = transcript(self.data, self.current)
        frames[5]["sda_output_select"] = 30
        control, _ = self.control(frames)
        with self.assertRaises(ManagementError):
            control.run()
        self.assertTrue((self.output / "frame-005.json").exists())

    def test_irq_bound_rejected_and_no_second_diag_command_sent(self):
        frames = transcript(self.data, self.current)
        frames[7]["one_shot_irq_count"] = 2
        control, uart = self.control(frames)
        with self.assertRaises(ManagementError):
            control.run()
        self.assertEqual(uart.writes.count(b"EMU_DIAG DETACHED\n"), 1)

    def test_partial_error_result_still_collects_full_cleanup_snapshot(self):
        frames = transcript(self.data, self.current)
        frames[7].update(ok=False, message="diagnostic_irq_or_pin_setup_failed")
        del frames[4:7]
        control, _ = self.control(frames)
        with self.assertRaises(ManagementError):
            control.run()
        self.assertEqual((self.output / "after-diag.bin").read_bytes(), self.data)
        self.assertEqual(
            json.loads((self.output / "failure.json").read_text())["phases"], []
        )

    def test_reset_metadata_change_rejected(self):
        frames = transcript(self.data, self.current)
        frames[7]["reset_reason"] = 12
        control, _ = self.control(frames)
        with self.assertRaises(ManagementError):
            control.run()

    def test_zero_irq_success_does_not_claim_printer_activity(self):
        frames = transcript(self.data, self.current)
        frames[7].update(one_shot_irq_count=0, observed_irq=0, observed_status=0)
        control, _ = self.control(frames)
        result = control.run()
        self.assertFalse(result["transport_qualified"])
        self.assertFalse(result["printer_activity_proven"])


class FramingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name)

    def reader(self, raw):
        uart = FakeUART([])
        uart.data.extend(raw)
        return diag.EvidenceReader(
            Session(uart, Audit(io.StringIO()), Tick()), self.output
        )

    def test_malformed_json_raw_bytes_saved_before_parsing_failure(self):
        raw = b'{"type": damaged}\n'
        reader = self.reader(raw)
        with self.assertRaises(ManagementError):
            reader.receive(reader.session.clock() + 1)
        self.assertEqual((self.output / "raw-line-000.bin").read_bytes(), raw)

    def test_line_bound_preserves_overlong_partial(self):
        reader = self.reader(b"x" * (diag.MAX_LINE_BYTES + 1))
        with self.assertRaises(ManagementError):
            reader.receive(reader.session.clock() + 1)
        self.assertEqual(
            (self.output / "raw-line-000-partial.bin").stat().st_size,
            diag.MAX_LINE_BYTES + 1,
        )

    def test_json_array_is_not_a_frame(self):
        reader = self.reader(b"[]\n")
        with self.assertRaises(ManagementError):
            reader.receive(reader.session.clock() + 1)

    def test_valid_breadcrumb_inverse_must_agree(self):
        value = current_state(valid_image())
        value["boot_breadcrumb"] = {"valid": True, "stage": 2, "stage_inverse": 2}
        with self.assertRaises(ManagementError):
            diag.state(value)

    def test_state_validation_does_not_mutate_frozen_v7_or_input(self):
        value = current_state(valid_image())
        original = copy.deepcopy(value)
        v7_identity = diag.v7.FIRMWARE
        diag.state(value)
        self.assertEqual(value, original)
        self.assertEqual(diag.v7.FIRMWARE, v7_identity)


if __name__ == "__main__":
    unittest.main()
