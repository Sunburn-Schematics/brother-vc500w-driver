"""V7 fake-UART regression and narrowly scoped V6 metadata recovery tests."""

from __future__ import annotations

import io
import json
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch

import manage_emulator_v7 as emu
import test_manage_emulator as v6_tests
from manage_esp32 import Audit, ManagementError, Session
from test_bench_eeprom import FakeUART, Tick

ORIGINAL_DEVICE_STATE = v6_tests.device_state
ELF = "c" * 64


def device_state(data, **kwargs):
    current = ORIGINAL_DEVICE_STATE(data, **kwargs)
    current.update(
        firmware=emu.FIRMWARE,
        last_fault_code=0,
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
    )
    return current


class V7InheritedTests(v6_tests.EmulatorTests):
    """Run all frozen V6 functional regressions against the distinct V7 helper."""

    def setUp(self):
        for name, replacement in (("emu", emu), ("device_state", device_state)):
            replacement_patch = patch.object(v6_tests, name, replacement)
            replacement_patch.start()
            self.addCleanup(replacement_patch.stop)
        super().setUp()


class V7RecoveryTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.output = self.root / "output"
        self.output.mkdir()
        from private_replay import private_folder
        self.source = private_folder("emulator-v7-recovery-source-01")
        self.proof = emu.RecoveryProof.load(self.source, emu.RECOVERY_MANIFEST_SHA256)
        self.data = self.proof.image
        self.current = device_state(self.data, live=1, durable=1, unclean=True)
        self.current.update(
            firmware=emu.FIRMWARE,
            last_fault="gpio_release_failed",
            last_fault_code=10,
            metrics={"rx_bytes": 123},
        )
        self.cleaned = dict(self.current, unclean_boot=False, last_fault="none", last_fault_code=0)

    def control(self, frames, callback=None, output=None):
        uart = FakeUART(frames, callback)
        result = emu.Control(
            Session(uart, Audit(io.StringIO()), Tick(), expected_firmware=emu.FIRMWARE),
            self.output if output is None else output,
            ELF,
        )
        return result, uart

    def probe(self, current):
        frames = v6_tests.probe_frames(current)
        frames[0]["firmware"] = emu.FIRMWARE
        return frames

    def image(self, current, data=None):
        return v6_tests.image_frame(self.data if data is None else data, current)

    def run_recovery(self, control, proof=None):
        return control.run(
            "recover",
            image_pin=emu.RECOVERY_SEED_SHA256,
            durable_generation=1,
            recovery_proof=self.proof if proof is None else proof,
        )

    def test_exact_observed_v6_bundle_keeps_consumed_seed(self):
        self.assertEqual(emu.digest(self.proof.image), emu.RECOVERY_SEED_SHA256)
        self.assertEqual(int.from_bytes(self.proof.image[0xE8:0xEA], "little"), 6256)
        self.assertEqual(self.proof.generation, 1)
        self.assertEqual(len(self.proof.records), 9)

    def test_manifest_and_each_record_must_match_exact_reviewed_pins(self):
        with self.assertRaises(ManagementError):
            emu.RecoveryProof.load(self.source, "a" * 64)
        copied = self.root / "changed-source"
        shutil.copytree(self.source, copied)
        changed = copied / "stop-result.json"
        changed.write_bytes(changed.read_bytes() + b" ")
        with self.assertRaises(ManagementError):
            emu.RecoveryProof.load(copied, emu.RECOVERY_MANIFEST_SHA256)

    def test_source_qualification_rejects_missing_stop_closure_and_prior_activity(self):
        for field, value in (
            ("session_open", True),
            ("session_active_marker", True),
            ("metrics", {"starts": 1}),
        ):
            records = dict(self.proof.records)
            result = json.loads(records["stop-result.json"])
            result[field] = value
            records["stop-result.json"] = json.dumps(result).encode()
            failure = json.loads(records["stop-failure.json"])
            failure["last_device_frame"] = result
            records["stop-failure.json"] = json.dumps(failure).encode()
            with self.subTest(field=field), self.assertRaises(ManagementError):
                emu.qualify_v6_recovery(records)

    def test_recovery_saves_proof_and_exact_plan_before_one_command(self):
        def before(command):
            if command.startswith(b"EMU_RECOVER"):
                plan = json.loads((self.output / "recovery-plan-before-command.json").read_text())
                self.assertFalse(plan["runtime_zero_metrics_used_as_proof"])
                self.assertFalse(plan["image_or_count_or_generation_change_permitted"])
                self.assertEqual((self.output / "before-recover.bin").read_bytes(), self.data)
                self.assertEqual(
                    (self.output / "reviewed-v6-evidence/manifest.json").read_bytes(),
                    self.proof.manifest_bytes,
                )

        frames = [
            *self.probe(self.current),
            self.image(self.current),
            {
                "type": "emu_result",
                "op": "recover",
                "ok": True,
                "message": "unchanged_seed_fault_metadata_cleared",
                **self.cleaned,
            },
            self.image(self.cleaned),
        ]
        control, uart = self.control(frames, before)
        receipt = self.run_recovery(control)
        self.assertEqual(receipt["status"], "recovered_legacy_attach_metadata")
        self.assertEqual(receipt["result"]["live_generation"], 1)
        self.assertEqual((self.output / "after-recover.bin").read_bytes(), self.data)
        self.assertEqual(sum(value.startswith(b"EMU_RECOVER") for value in uart.writes), 1)
        self.assertFalse(receipt["physical_transport_success_proven"])

    def test_new_v7_faults_or_changed_runtime_state_never_recover(self):
        for name, updates in (
            ("new_attach", {"last_fault_code": 12, "last_fault": "attach_failed"}),
            ("new_cleanup", {"last_fault_code": 13, "last_fault": "gpio_cleanup_failed"}),
            ("generation", {"live_generation": 2, "durable_generation": 2}),
            ("marker", {"session_active_marker": True}),
            ("seed", {"seed_sha256": "b" * 64}),
            ("no_legacy_fault", {"last_fault_code": 0, "last_fault": "none"}),
        ):
            with self.subTest(name=name):
                current = dict(self.current, **updates)
                output = self.root / name
                output.mkdir()
                control, uart = self.control(self.probe(current), output=output)
                with self.assertRaises(ManagementError):
                    self.run_recovery(control)
                self.assertFalse(any(value.startswith(b"EMU_RECOVER") for value in uart.writes))

    def test_fresh_valid_but_changed_image_is_not_recovered(self):
        changed = bytearray(self.data)
        changed[0xF8] ^= 1  # Outside manufacturer CRC regions: hashes still protect it.
        changed = bytes(changed)
        current = dict(self.current, image_sha256=emu.digest(changed))
        frames = [*self.probe(self.current), self.image(current, changed)]
        control, uart = self.control(frames)
        with self.assertRaises(ManagementError):
            self.run_recovery(control)
        self.assertFalse(any(value.startswith(b"EMU_RECOVER") for value in uart.writes))
        self.assertEqual((self.output / "before-recover.bin").read_bytes(), changed)

    def test_recovery_failure_retains_uncertain_frame_and_does_not_retry(self):
        frames = [
            *self.probe(self.current),
            self.image(self.current),
            {
                "type": "emu_result",
                "op": "recover",
                "ok": False,
                "message": "checkpoint_failed_outcome_uncertain",
                **self.current,
            },
        ]
        control, uart = self.control(frames)
        with self.assertRaises(ManagementError):
            self.run_recovery(control)
        failure = json.loads((self.output / "failure.json").read_text())
        self.assertEqual(
            failure["last_device_frame"]["message"], "checkpoint_failed_outcome_uncertain"
        )
        self.assertTrue(failure["command_may_have_reached_device"])
        self.assertEqual(sum(value.startswith(b"EMU_RECOVER") for value in uart.writes), 1)

    def test_recovery_cannot_change_generation_or_counter(self):
        changed = dict(self.cleaned, live_generation=2, durable_generation=2)
        frames = [
            *self.probe(self.current),
            self.image(self.current),
            {
                "type": "emu_result",
                "op": "recover",
                "ok": True,
                "message": "unchanged_seed_fault_metadata_cleared",
                **changed,
            },
        ]
        control, _ = self.control(frames)
        with self.assertRaises(ManagementError):
            self.run_recovery(control)
        self.assertFalse((self.output / "receipt.json").exists())

    def test_preflight_failure_retains_low_sda_and_does_not_retry_start(self):
        current = dict(self.cleaned)
        rejected = dict(current)
        rejected["attach_diagnostics"] = dict(
            current["attach_diagnostics"], stage="bus_not_idle_high", core=1, levels=1
        )
        frames = [
            *self.probe(current),
            self.image(current),
            {
                "type": "emu_result",
                "op": "start",
                "ok": False,
                "message": "attach_preflight_failed",
                **rejected,
            },
        ]
        control, uart = self.control(frames)
        with self.assertRaises(ManagementError):
            control.run(
                "start", image_pin=emu.RECOVERY_SEED_SHA256, durable_generation=1, sole_0x50=True
            )
        failure = json.loads((self.output / "failure.json").read_text())
        self.assertEqual(failure["last_device_frame"]["attach_diagnostics"]["levels"], 1)
        self.assertFalse(failure["last_device_frame"]["session_active_marker"])
        self.assertEqual(sum(value.startswith(b"EMU_START") for value in uart.writes), 1)
        self.assertFalse(any(value.startswith(b"EMU_RECOVER") for value in uart.writes))

    def test_v6_identity_and_malformed_v7_diagnostics_rejected(self):
        old = dict(self.current, firmware="i2c-eeprom-emulator-v6")
        with self.assertRaises(ManagementError):
            emu.state(old)
        malformed = dict(
            self.current, attach_diagnostics=dict(self.current["attach_diagnostics"], levels=4)
        )
        with self.assertRaises(ManagementError):
            emu.state(malformed)


if __name__ == "__main__":
    unittest.main()
