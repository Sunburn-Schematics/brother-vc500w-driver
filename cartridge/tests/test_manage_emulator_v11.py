"""Focused read recovery and D19 release/start regressions; hardware never opened."""

import io
import json
import tempfile
import unittest
from pathlib import Path

import manage_emulator_v11 as v11
import test_manage_emulator_v9 as fixture
from manage_esp32 import Audit, ManagementError, Session
from test_bench_eeprom import Tick


def proof_folder() -> Path:
    from private_replay import private_folder
    return private_folder("emulator-v11-read-recovery-source-01")


def pullup(on=False):
    return {
        "gpio": 19,
        "configured_on": on,
        "output_enabled": on,
        "output_high": True,
        "input_high": on,
        "output_select": 1280,
        "internal_pulls": False,
        "off_verified": not on,
    }


def current(data, *, fault=False, on=False):
    state = fixture.current(data, unclean=fault)
    state.update(
        firmware=v11.FIRMWARE,
        session_active_marker=False,
        last_fault_code=2 if fault else 0,
        last_fault="tx_byte_count_unqualified" if fault else "none",
        pullup_control=pullup(on),
        read_accounting={
            "method": "fifo_dequeued_at_terminal_nack",
            "loaded": 0,
            "fifo_remaining": 0,
            "consumed": 0,
            "terminal_nack": False,
            "rx_irq_diagnostic": 0,
            "tx_irq_diagnostic": 0,
        },
    )
    return state


def probe(state):
    frames = fixture.probe(state)
    frames[0]["firmware"] = frames[1]["firmware"] = v11.FIRMWARE
    return frames


class UART(fixture.UART):
    def write(self, data):
        if not data.startswith(b"EMU_START_PULLUP "):
            return super().write(data)
        self.writes.append(data)
        if self.on_command:
            self.on_command(data)
        self.data.extend(self.diagnostic_prefix)
        while self.pending_frames:
            frame = self.pending_frames.pop(0)
            self.data.extend((json.dumps(frame) + "\n").encode())
            if frame.get("type") == "emu_result":
                break
        self.data.extend(self.diagnostic_suffix)
        return len(data)


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)
        self.proof = v11.ReadProof.load(proof_folder(), v11.RECOVERY_MANIFEST)
        self.data = self.proof.files["stop-after.bin"]
        self.clean = current(self.data)
        self.fault = current(self.data, fault=True)

    def control(self, frames, callback=None):
        uart = UART(frames, callback)
        session = Session(uart, Audit(io.StringIO()), Tick(), expected_firmware=v11.FIRMWARE)
        return v11.Control(session, self.output, fixture.ELF, v11.RECOVERY_SEED), uart

    def recovery_frames(self):
        result = {
            "type": "emu_result",
            "op": "recover_read",
            "ok": True,
            "message": "unchanged_read_fault_metadata_cleared",
            "prior_fault_code": 2,
            "recovery_policy": "V10_READ_UNCHANGED",
            **self.clean,
        }
        return [
            *probe(self.fault),
            fixture.old.image_frame(self.data, self.fault),
            result,
            fixture.old.image_frame(self.data, self.clean),
            *probe(self.clean),
        ]

    def start_frames(self):
        pending = {
            **self.clean,
            "session_open": True,
            "session_active_marker": True,
            "image_sha256": "",
        }
        enabled = {**pending, "pullup_control": pullup(True)}
        active = {
            **enabled,
            "emulation_active": True,
            "pins_input_only": False,
            "sda_open_drain": True,
        }
        return [
            *probe(self.clean),
            fixture.old.image_frame(self.data, self.clean),
            {
                "type": "emu_start_phase",
                "op": "start",
                "phase": "marker_saved",
                **pending,
            },
            {
                "type": "emu_start_phase",
                "op": "start",
                "phase": "fifo_loaded",
                **fixture.regs(),
                **enabled,
            },
            {
                "type": "emu_result",
                "op": "start",
                "ok": True,
                "message": "experimental_responder_enabled",
                **active,
            },
        ]

    def test_actual18_file_read_proof_has_no_inferred_byte_value(self):
        self.assertEqual(len(self.proof.files), 24)
        manifest = json.loads(self.proof.manifest)
        self.assertFalse(manifest["pointer_byte_value_known"])
        self.assertEqual(v11.digest(self.data), v11.RECOVERY_SEED)
        self.assertEqual(int.from_bytes(self.data[232:234], "little"), 6256)

    def test_recovery_exports_exact_plan_before_one_command_and_preserves_count(self):
        def check(command):
            if command.startswith(b"EMU_RECOVER_READ "):
                self.assertTrue((self.output / "recovery-plan-before-command.json").exists())
                self.assertEqual((self.output / "before-recover.bin").read_bytes(), self.data)
                self.assertEqual(
                    len(list((self.output / "reviewed-read-fault-evidence").iterdir())),
                    25,
                )

        control, uart = self.control(self.recovery_frames(), check)
        result = control.run_action("recover", proof=self.proof)
        self.assertEqual(result["status"], "read_fault_metadata_recovered")
        self.assertEqual((self.output / "after-recover.bin").read_bytes(), self.data)
        self.assertEqual(result["after"]["durable_generation"], 1)
        self.assertEqual(sum(x.startswith(b"EMU_RECOVER_READ ") for x in uart.writes), 1)

    def test_other_fault_is_not_recovered(self):
        frames = self.recovery_frames()
        frames[2]["last_fault_code"] = 14
        frames[3]["last_fault_code"] = 14
        control, uart = self.control(frames)
        with self.assertRaises(ManagementError):
            control.run_action("recover", proof=self.proof)
        self.assertFalse(any(x.startswith(b"EMU_RECOVER_READ ") for x in uart.writes))

    def test_read_accounting_is_typed_but_irq_count_is_not_a_byte_count(self):
        frame = {
            **self.clean,
            "read_accounting": {
                "method": "fifo_dequeued_at_terminal_nack",
                "loaded": 288,
                "fifo_remaining": 32,
                "consumed": 256,
                "terminal_nack": True,
                "rx_irq_diagnostic": 1,
                "tx_irq_diagnostic": 3,
            },
        }
        self.assertIs(v11.state(frame), frame)
        self.assertNotEqual(
            frame["read_accounting"]["consumed"], frame["read_accounting"]["tx_irq_diagnostic"]
        )
        for name, bad in (
            ("loaded", True),
            ("consumed", -1),
            ("terminal_nack", 1),
            ("method", "irq_equality"),
        ):
            with self.subTest(name=name), self.assertRaises(ManagementError):
                v11.state({**frame, "read_accounting": {**frame["read_accounting"], name: bad}})

    def test_changed_proof_full_image_or_terminal_nack_is_rejected(self):
        for name in ("stop-after.bin", "fault-raw-002.bin"):
            changed = self.proof.files[name][:-1]
            if name.endswith("002.bin"):
                frame = json.loads(self.proof.files[name])
                frame["metrics"]["nacks"] = 0
                changed = json.dumps(frame).encode()
            damaged = {**self.proof.files, name: changed}
            with self.subTest(name=name), self.assertRaises((ManagementError, ValueError)):
                v11.ReadProof.qualify(damaged)

    def test_pullup_on_requires_explicit_power_assertion(self):
        control, uart = self.control(
            [*probe(self.clean), fixture.old.image_frame(self.data, self.clean)]
        )
        with self.assertRaises(ManagementError):
            control.run_action("pullup-on")
        self.assertNotIn(b"EMU_PULLUP ON PRINTER_POWERED\n", uart.writes)

    def test_pullup_off_works_with_fault_unclean_and_optional_snapshot_busy(self):
        result = {
            "type": "emu_result",
            "op": "pullup_off",
            "ok": True,
            "message": "pullup_off",
            **self.fault,
        }
        frames = [
            *probe(self.fault),
            {"type": "emu_snapshot", "ok": False},
            result,
            fixture.old.image_frame(self.data, self.fault),
        ]
        control, uart = self.control(frames)
        receipt = control.run_action("pullup-off")
        self.assertEqual(receipt["status"], "pullup_off_verified")
        self.assertTrue(receipt["result"]["unclean_boot"])
        self.assertEqual(uart.writes.count(b"EMU_PULLUP OFF\n"), 1)

    def test_combined_start_is_one_command_with_off_then_on_phases(self):
        control, uart = self.control(self.start_frames())
        result = control.run_action("start-pullup", sole=True, powered=True)
        self.assertEqual(result["status"], "started_experimental")
        self.assertTrue(result["result"]["pullup_control"]["configured_on"])
        self.assertEqual(sum(x.startswith(b"EMU_START_PULLUP ") for x in uart.writes), 1)
        self.assertNotIn(b"EMU_PULLUP ON PRINTER_POWERED\n", uart.writes)

    def test_combined_start_rejects_off_fifo_phase_without_retry(self):
        frames = self.start_frames()
        frames[5]["pullup_control"] = pullup(False)
        control, uart = self.control(frames)
        with self.assertRaises(ManagementError):
            control.run_action("start-pullup", sole=True, powered=True)
        self.assertEqual(sum(x.startswith(b"EMU_START_PULLUP ") for x in uart.writes), 1)
        self.assertTrue((self.output / "frame-005.json").exists())

    def test_stop_rejects_a_result_that_left_d19_enabled(self):
        enabled = current(self.data, on=True)
        result = {
            "type": "emu_result",
            "op": "stop",
            "ok": True,
            "message": "already_passive_marker_preserved",
            **enabled,
        }
        frames = [*probe(enabled), fixture.old.image_frame(self.data, enabled), result]
        control, uart = self.control(frames)
        with self.assertRaises(ManagementError):
            control.run_action("stop")
        self.assertEqual(uart.writes.count(b"EMU_STOP\n"), 1)
        self.assertTrue((self.output / "failure.json").exists())


if __name__ == "__main__":
    unittest.main()
