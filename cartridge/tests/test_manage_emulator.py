"""Offline fake-UART tests for V6 state, image and provenance controls."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import manage_emulator as emu
from manage_esp32 import Audit, ManagementError, Session
from test_bench_eeprom import FakeUART, Tick, valid_image

ELF = "c" * 64


def device_state(
    data: bytes, *, active=False, live=7, durable=7, unclean=False, marker=None, session_open=None
):
    opened = active if session_open is None else session_open
    return {
        "firmware": emu.FIRMWARE,
        "capability": emu.CAPABILITY,
        "emulation_active": active,
        "session_open": opened,
        "loaded": True,
        "address": 80,
        "image_sha256": "" if active or opened or live != durable else emu.digest(data),
        "seed_sha256": emu.digest(data),
        "live_generation": live,
        "durable_generation": durable,
        "dirty": live != durable,
        "unclean_boot": unclean,
        "session_active_marker": active if marker is None else marker,
        "pins_input_only": not active,
        "scl_input_only": True,
        "sda_open_drain": active,
        "internal_pulls": False,
        "last_fault": "none",
        "transport_qualified": False,
    }


def probe_frames(current):
    return [
        {
            "type": "status",
            "firmware": emu.FIRMWARE,
            "emulator_capability": emu.CAPABILITY,
            "cpu_mhz": 240,
            "sda_gpio": 21,
            "scl_gpio": 22,
            "armed": False,
            "pins_input_only": current["pins_input_only"],
            "internal_pulls": False,
        },
        {"type": "netinfo", "app_elf_sha256": ELF, "armed": False, "update_busy": False},
        {"type": "emu_status", **current},
    ]


def image_frame(data, current, generation=None):
    inspection = emu.inspect_eeprom(data)
    generation = current["live_generation"] if generation is None else generation
    return {
        "type": "emu_snapshot",
        **current,
        "bytes": 256,
        "hex": data.hex(),
        "sha256": emu.digest(data),
        "image_generation": generation,
        "snapshot_is_current": generation == current["live_generation"],
        **dict(
            zip(
                ("crc_r0", "crc_rw1", "crc_rw2"),
                [x["matches"] for x in inspection["crc_checks"]],
                strict=True,
            )
        ),
    }


class EmulatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.output = self.root / "output"
        self.output.mkdir()
        self.data = valid_image(remaining=6256)
        self.current = device_state(self.data)

    def tearDown(self):
        self.temp.cleanup()

    def control(self, frames, on_command=None):
        uart = FakeUART(frames, on_command)
        control = emu.Control(
            Session(uart, Audit(io.StringIO()), Tick(), expected_firmware=emu.FIRMWARE),
            self.output,
            ELF,
        )
        return control, uart

    def seed(self, data=None):
        data = self.data if data is None else data
        image = self.root / "image.bin"
        image.write_bytes(data)
        value = {
            "schema": "sunburn.brother.assembled-emulator-seed.v1",
            "sha256": emu.digest(data),
            "bytes": 256,
            "unknown_bytes_filled": False,
            "all_three_crcs_match": True,
            "counter_automatically_reset": False,
            "remaining_u16_le": int.from_bytes(data[232:234], "little"),
            "is_direct_bench_dump": False,
        }
        provenance = self.root / "provenance.json"
        provenance.write_text(json.dumps(value))
        return emu.Seed.load(
            image, emu.digest(data), provenance, emu.digest(provenance.read_bytes())
        )

    def test_active_aware_status_does_not_require_passive_output_pin(self):
        current = device_state(self.data, active=True, live=8)
        control, _ = self.control(probe_frames(current))
        result = control.run("status")
        self.assertTrue(result["result"]["dirty"])
        self.assertFalse(result["result"]["pins_input_only"])
        self.assertFalse(result["physical_transport_success_proven"])

    def test_snapshot_accepts_real_intermediate_bad_crc_and_does_not_call_it_durable(self):
        data = bytearray(self.data)
        data[232] ^= 1
        data = bytes(data)
        current = device_state(data, active=True, live=8)
        control, _ = self.control([*probe_frames(current), image_frame(data, current)])
        result = control.run("snapshot")
        self.assertFalse(result["snapshot"]["all_three_crcs_match"])
        self.assertFalse(result["snapshot"]["snapshot_bytes_are_confirmed_durable"])
        self.assertEqual((self.output / "snapshot.bin").read_bytes(), data)

    def test_stale_but_coherent_active_snapshot_is_labeled(self):
        current = device_state(self.data, active=True, live=9)
        control, _ = self.control([*probe_frames(current), image_frame(self.data, current, 8)])
        result = control.run("snapshot")
        self.assertFalse(result["snapshot"]["snapshot_is_current"])
        self.assertEqual(result["snapshot"]["image_generation"], 8)

    def test_snapshot_receipt_uses_latest_reported_state(self):
        before = device_state(self.data, active=True, live=8)
        after = device_state(self.data, active=True, live=9)
        control, _ = self.control([*probe_frames(before), image_frame(self.data, after)])
        result = control.run("snapshot")
        self.assertEqual(result["result"]["live_generation"], 9)

    def test_detached_fault_state_has_unknown_common_hash_and_actual_snapshot(self):
        detached = device_state(self.data, live=8, marker=True, session_open=True)
        detached["last_fault"] = "tx_underflow"
        self.assertEqual(detached["image_sha256"], "")
        emu.state(detached)
        data, _ = emu.snapshot(image_frame(self.data, detached))
        self.assertEqual(data, self.data)
        detached["image_sha256"] = emu.digest(self.data)
        with self.assertRaises(ManagementError):
            emu.state(detached)

    def test_fault_snapshot_with_unknown_hash_and_false_marker_is_preserved(self):
        detached = device_state(self.data, live=8, marker=False, session_open=False)
        detached["last_fault"] = "checkpoint_failed"
        control, _ = self.control([*probe_frames(detached), image_frame(self.data, detached)])
        result = control.run("snapshot")
        self.assertTrue(result["result"]["dirty"])
        self.assertFalse(result["result"]["session_active_marker"])
        self.assertEqual((self.output / "snapshot.bin").read_bytes(), self.data)
        self.assertFalse(result["snapshot"]["snapshot_bytes_are_confirmed_durable"])

    def test_invalid_unknown_hash_state_preserves_frame_and_probe_failure(self):
        invalid = dict(self.current, image_sha256="")
        control, uart = self.control(probe_frames(invalid))
        with self.assertRaises(ManagementError):
            control.run("status")
        failure = json.loads((self.output / "failure.json").read_text())
        self.assertEqual(failure["status"], "unverified_probe")
        self.assertEqual(failure["last_device_frame"]["image_sha256"], "")
        self.assertFalse(failure["command_may_have_reached_device"])
        self.assertFalse(any(value.startswith(b"EMU_START") for value in uart.writes))

    def test_false_crc_and_generation_claims_rejected(self):
        frame = image_frame(self.data, self.current)
        frame["crc_rw2"] = False
        with self.assertRaises(ManagementError):
            emu.snapshot(frame)
        frame = image_frame(self.data, self.current)
        frame["image_generation"] = 8
        with self.assertRaises(ManagementError):
            emu.snapshot(frame)

    def test_bad_active_pin_address_or_durability_state_rejected(self):
        for key, value in (
            ("address", 81),
            ("sda_open_drain", False),
            ("scl_input_only", False),
            ("dirty", True),
            ("unclean_boot", True),
        ):
            current = device_state(self.data, active=True)
            current[key] = value
            with self.subTest(key=key), self.assertRaises(ManagementError):
                emu.state(current)

    def test_seed_keeps_consumed_counter_and_records_assembled_provenance(self):
        selected = self.seed()
        self.assertEqual(int.from_bytes(selected.data[232:234], "little"), 6256)
        self.assertFalse(selected.provenance["is_direct_bench_dump"])

    def test_actual_reviewed_consumed_seed_and_provenance_pins(self):
        from private_replay import private_folder
        directory = private_folder("emulator-seed-after-confirmed-print-01")
        selected = emu.Seed.load(
            directory / "assembled-emulator-seed-256.bin",
            "d7c0db4edb0db564e8980ba26b8ebf6f95082685aa4e0b40c8c8630b4715e091",
            directory / "provenance.json",
            "f50cec4b7074c5b798029ceb3382f9b7e62614db31896eb2369798e75b241850",
        )
        self.assertEqual(int.from_bytes(selected.data[232:234], "little"), 6256)
        self.assertEqual(selected.data[246:248].hex(), "aa25")
        self.assertFalse(selected.provenance["is_fresh_full_postprint_read"])

    def test_stop_keeps_crc_inconsistent_checkpoint_without_repair(self):
        changed = bytearray(self.data)
        changed[232] ^= 1
        changed = bytes(changed)
        active = device_state(changed, active=True, live=8)
        stopped = device_state(changed, live=8, durable=8)
        frames = [
            *probe_frames(active),
            image_frame(changed, active),
            {"type": "emu_result", "op": "stop", "ok": True, **stopped},
            image_frame(changed, stopped),
        ]
        control, _ = self.control(frames)
        result = control.run("stop")
        self.assertEqual(result["status"], "stopped_checkpointed")
        self.assertFalse(result["snapshot"]["all_three_crcs_match"])
        self.assertEqual((self.output / "after-stop-checkpoint.bin").read_bytes(), changed)

    def test_seed_rejects_hash_counter_and_crc_disagreement(self):
        self.seed()
        image = self.root / "image.bin"
        provenance = self.root / "provenance.json"
        value = json.loads(provenance.read_text())
        value["remaining_u16_le"] = 6299
        provenance.write_text(json.dumps(value))
        with self.assertRaises(ManagementError):
            emu.Seed.load(
                image, emu.digest(self.data), provenance, emu.digest(provenance.read_bytes())
            )
        with self.assertRaises(ManagementError):
            emu.Seed.load(image, "b" * 64, provenance, emu.digest(provenance.read_bytes()))

    def test_load_saves_source_before_exact_binary_and_verifies_snapshot(self):
        selected = self.seed()

        def before(command):
            if command.startswith(b"EMU_LOAD"):
                self.assertEqual((self.output / "selected-seed.bin").read_bytes(), self.data)
                self.assertEqual(
                    (self.output / "selected-source-provenance.json").read_bytes(),
                    selected.provenance_bytes,
                )
                self.assertTrue((self.output / "intent-before-command.json").exists())

        frames = [
            *probe_frames(self.current),
            {"type": "emu_load_ready", "bytes": 256, "sha256": selected.sha256, "timeout_ms": 5000},
            {"type": "emu_result", "op": "load", "ok": True, **self.current},
            image_frame(self.data, self.current),
        ]
        control, uart = self.control(frames, before)
        result = control.run("load", selected)
        self.assertEqual(result["status"], "loaded_verified")
        self.assertIn(self.data, uart.writes)
        binary = uart.writes.index(self.data)
        self.assertEqual(uart.writes[binary + 1], b"EMU_SNAPSHOT\n")
        self.assertTrue(result["snapshot"]["snapshot_bytes_are_confirmed_durable"])

    def test_load_ready_mismatch_sends_no_binary_and_keeps_failure(self):
        selected = self.seed()
        frames = [
            *probe_frames(self.current),
            {"type": "emu_load_ready", "bytes": 255, "sha256": selected.sha256, "timeout_ms": 5000},
        ]
        control, uart = self.control(frames)
        with self.assertRaises(ManagementError):
            control.run("load", selected)
        self.assertNotIn(self.data, uart.writes)
        self.assertTrue(
            json.loads((self.output / "failure.json").read_text())[
                "command_may_have_reached_device"
            ]
        )

    def test_load_rejects_dirty_unclean_open_or_marked_state_before_command(self):
        selected = self.seed()
        for name, current in (
            ("dirty", device_state(self.data, live=8)),
            ("unclean", device_state(self.data, unclean=True)),
            ("session", device_state(self.data, marker=True, session_open=True)),
            ("marker", device_state(self.data, marker=True)),
        ):
            with self.subTest(name=name):
                output = self.root / name
                output.mkdir()
                uart = FakeUART(probe_frames(current))
                control = emu.Control(Session(uart, Audit(io.StringIO()), Tick()), output, ELF)
                with self.assertRaises(ManagementError):
                    control.run("load", selected)
                self.assertFalse(any(value.startswith(b"EMU_LOAD") for value in uart.writes))
                self.assertFalse((output / "selected-seed.bin").exists())

    def test_load_does_not_accept_a_valid_crc_counter_reset(self):
        selected = self.seed()
        wrong = valid_image(remaining=6299)
        frames = [
            *probe_frames(self.current),
            {"type": "emu_load_ready", "bytes": 256, "sha256": selected.sha256, "timeout_ms": 5000},
            {"type": "emu_result", "op": "load", "ok": True, **device_state(wrong)},
        ]
        control, _ = self.control(frames)
        with self.assertRaises(ManagementError):
            control.run("load", selected)
        self.assertFalse((self.output / "receipt.json").exists())

    def test_start_saves_current_snapshot_and_sole_assertion_before_command(self):
        active = device_state(self.data, active=True)

        def before(command):
            if command.startswith(b"EMU_START"):
                self.assertEqual((self.output / "before-start.bin").read_bytes(), self.data)
                self.assertTrue((self.output / "sole-address-start-before-command.json").exists())

        frames = [
            *probe_frames(self.current),
            image_frame(self.data, self.current),
            {"type": "emu_result", "op": "start", "ok": True, **active},
        ]
        control, uart = self.control(frames, before)
        result = control.run(
            "start", image_pin=emu.digest(self.data), durable_generation=7, sole_0x50=True
        )
        self.assertEqual(result["status"], "started_experimental")
        self.assertEqual(sum(value.startswith(b"EMU_START") for value in uart.writes), 1)
        self.assertFalse(result["transport_qualified"])

    def test_start_rejects_missing_sole_stale_generation_dirty_or_unclean_state(self):
        for setting in ("sole", "generation", "dirty", "unclean"):
            with self.subTest(setting=setting):
                output = self.root / setting
                output.mkdir()
                current = dict(self.current)
                if setting == "dirty":
                    current.update(live_generation=8, dirty=True)
                if setting == "unclean":
                    current.update(unclean_boot=True, session_active_marker=True)
                uart = FakeUART(probe_frames(current))
                control = emu.Control(Session(uart, Audit(io.StringIO()), Tick()), output, ELF)
                with self.assertRaises(ManagementError):
                    control.run(
                        "start",
                        image_pin=emu.digest(self.data),
                        durable_generation=6 if setting == "generation" else 7,
                        sole_0x50=setting != "sole",
                    )
                self.assertFalse(any(value.startswith(b"EMU_START") for value in uart.writes))

    def test_stop_verifies_checkpoint_and_preserves_full_bytes(self):
        active = device_state(self.data, active=True, live=8)
        stopped = device_state(self.data, live=8, durable=8)
        frames = [
            *probe_frames(active),
            image_frame(self.data, active),
            {"type": "emu_result", "op": "stop", "ok": True, **stopped},
            image_frame(self.data, stopped),
        ]
        control, _ = self.control(frames)
        result = control.run("stop")
        self.assertEqual(result["status"], "stopped_checkpointed")
        self.assertTrue(result["snapshot"]["snapshot_bytes_are_confirmed_durable"])
        self.assertEqual((self.output / "after-stop-checkpoint.bin").read_bytes(), self.data)

    def test_fault_result_retains_uncertainty_and_does_not_retry_stop(self):
        active = device_state(self.data, active=True)
        failed = device_state(self.data)
        failed.update(last_fault="tx_underflow", session_active_marker=True, session_open=True)
        failed["image_sha256"] = ""
        frames = [
            *probe_frames(active),
            image_frame(self.data, active),
            {"type": "emu_result", "op": "stop", "ok": False, **failed},
        ]
        control, uart = self.control(frames)
        with self.assertRaises(ManagementError):
            control.run("stop")
        failure = json.loads((self.output / "failure.json").read_text())
        self.assertEqual(failure["last_device_frame"]["last_fault"], "tx_underflow")
        self.assertEqual(sum(value == b"EMU_STOP\n" for value in uart.writes), 1)

    def test_failed_optional_snapshot_does_not_prevent_stop_once(self):
        active = device_state(self.data, active=True, live=8)
        stopped = device_state(self.data, live=8, durable=8)
        frames = [
            *probe_frames(active),
            {"type": "emu_snapshot", "ok": False, "message": "snapshot_busy", **active},
            {"type": "emu_result", "op": "stop", "ok": True, **stopped},
            image_frame(self.data, stopped),
        ]
        control, uart = self.control(frames)
        result = control.run("stop")
        self.assertEqual(result["status"], "stopped_checkpointed")
        self.assertEqual(sum(value == b"EMU_STOP\n" for value in uart.writes), 1)
        self.assertTrue((self.output / "before-stop-snapshot-error.json").exists())
        self.assertIsNotNone(result["pre_stop_snapshot_error"])
        self.assertEqual((self.output / "after-stop-checkpoint.bin").read_bytes(), self.data)

    def test_detached_dirty_fault_can_send_stop_and_keeps_fault_receipt(self):
        detached = device_state(self.data, live=8, session_open=True, marker=True)
        detached["last_fault"] = "tx_underflow"
        stopped = device_state(self.data, live=8, durable=8)
        stopped["last_fault"] = "tx_underflow"
        frames = [
            *probe_frames(detached),
            image_frame(self.data, detached),
            {"type": "emu_result", "op": "stop", "ok": False, **stopped},
        ]
        control, uart = self.control(frames)
        with self.assertRaises(ManagementError):
            control.run("stop")
        self.assertEqual(sum(value == b"EMU_STOP\n" for value in uart.writes), 1)
        failure = json.loads((self.output / "failure.json").read_text())
        self.assertEqual(failure["last_device_frame"]["last_fault"], "tx_underflow")
        self.assertEqual((self.output / "before-stop-volatile.bin").read_bytes(), self.data)

    def test_optional_snapshot_timeout_still_sends_stop_once(self):
        active = device_state(self.data, active=True, live=8)
        stopped = device_state(self.data, live=8, durable=8)
        uart = FakeUART(probe_frames(active))

        def after_command(command):
            if command == b"EMU_STOP\n":
                uart.data.extend(
                    (
                        json.dumps({"type": "emu_result", "op": "stop", "ok": True, **stopped})
                        + "\n"
                    ).encode()
                )
            elif command == b"EMU_SNAPSHOT\n" and b"EMU_STOP\n" in uart.writes:
                uart.data.extend((json.dumps(image_frame(self.data, stopped)) + "\n").encode())

        uart.on_command = after_command
        control = emu.Control(Session(uart, Audit(io.StringIO()), Tick()), self.output, ELF)
        result = control.run("stop")
        self.assertEqual(result["status"], "stopped_checkpointed")
        self.assertEqual(sum(value == b"EMU_STOP\n" for value in uart.writes), 1)
        self.assertIsNotNone(result["pre_stop_snapshot_error"])

    def test_snapshot_error_export_failure_still_sends_stop_once(self):
        active = device_state(self.data, active=True, live=8)
        stopped = device_state(self.data, live=8, durable=8)
        frames = [
            *probe_frames(active),
            {"type": "emu_snapshot", "ok": False, "message": "snapshot_busy", **active},
            {"type": "emu_result", "op": "stop", "ok": True, **stopped},
            image_frame(self.data, stopped),
        ]
        control, uart = self.control(frames)
        save_json = emu.durable_json

        def export(path, value):
            if path.name == "before-stop-snapshot-error.json":
                raise OSError("synthetic diagnostic export failure")
            return save_json(path, value)

        with patch.object(emu, "durable_json", side_effect=export):
            result = control.run("stop")
        self.assertEqual(result["status"], "stopped_checkpointed")
        self.assertEqual(sum(value == b"EMU_STOP\n" for value in uart.writes), 1)
        self.assertEqual(
            result["pre_stop_snapshot_error"]["diagnostic_write_failed"]["exception_type"],
            "OSError",
        )

    def test_inactive_stop_preserves_unclean_boot_evidence(self):
        unclean = device_state(self.data, unclean=True, marker=True)
        frames = [
            *probe_frames(unclean),
            image_frame(self.data, unclean),
            {"type": "emu_result", "op": "stop", "ok": True, **unclean},
            image_frame(self.data, unclean),
        ]
        control, _ = self.control(frames)
        result = control.run("stop")
        self.assertEqual(result["status"], "stopped_unclean_state_preserved")
        self.assertFalse(result["snapshot"]["snapshot_bytes_are_confirmed_durable"])

    def test_inactive_stop_cannot_silently_clear_prior_unclean_boot(self):
        unclean = device_state(self.data, unclean=True, marker=True)
        frames = [
            *probe_frames(unclean),
            image_frame(self.data, unclean),
            {"type": "emu_result", "op": "stop", "ok": True, **self.current},
        ]
        control, _ = self.control(frames)
        with self.assertRaises(ManagementError):
            control.run("stop")

    def test_wrong_firmware_elf_never_sends_active_command(self):
        frames = probe_frames(self.current)
        frames[1]["app_elf_sha256"] = "b" * 64
        control, uart = self.control(frames)
        with self.assertRaises(ManagementError):
            control.run(
                "start", image_pin=emu.digest(self.data), durable_generation=7, sole_0x50=True
            )
        self.assertFalse(any(value.startswith(b"EMU_START") for value in uart.writes))


if __name__ == "__main__":
    unittest.main()
