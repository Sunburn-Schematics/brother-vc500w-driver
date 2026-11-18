"""Offline protocol/provenance tests; fake UART only, no serial dependency."""

from __future__ import annotations

import io
import json
import struct
import tempfile
import unittest
from pathlib import Path

import bench_eeprom as bench
from inspect_eeprom import REGIONS, crc16_x25
from manage_esp32 import Audit, ManagementError, Session

ELF = "a" * 64


def valid_image(*, initial: int = 6299, remaining: int = 4, revision: int = 2) -> bytes:
    data = bytearray((index * 37) & 255 for index in range(256))
    data[1] = revision
    struct.pack_into("<H", data, 0x60, initial)
    struct.pack_into("<H", data, 0xE8, remaining)
    for _, start, length, target in REGIONS:
        struct.pack_into("<H", data, target, crc16_x25(data[start : start + length]))
    return bytes(data)


def image_frame(data: bytes, phase: str) -> dict[str, object]:
    return {
        "type": "bench_image",
        "phase": phase,
        "bytes": len(data),
        "hex": data.hex(),
        "sha256": bench.digest(data),
        "crc_r0": True,
        "crc_rw1": True,
        "crc_rw2": True,
    }


def info() -> dict[str, object]:
    return {
        "type": "status",
        "firmware": bench.FIRMWARE,
        "pins_input_only": True,
        "internal_pulls": False,
        "cpu_mhz": 240,
        "sda_gpio": 21,
        "scl_gpio": 22,
        "armed": False,
        "bench_capability": bench.CAPABILITY,
    }


def netinfo() -> dict[str, object]:
    return {"type": "netinfo", "armed": False, "update_busy": False, "app_elf_sha256": ELF}


def transcript(action: str, data: bytes, *, pullups: str = "NONE") -> list[dict[str, object]]:
    plan, expected = bench.restoration_plan(data)
    frames = [info(), netinfo(), info(), {"type": "bench_start", "op": action, "pullups": pullups}]
    for phase in ("pre_read_1", "pre_read_2"):
        frames += [{"type": "bench_phase", "phase": phase}, image_frame(data, phase)]
    if action == "restore":
        frames += [{"type": "bench_phase", "phase": "precheck"}, {"type": "bench_plan", **plan}]
        if expected != data:
            frames += [
                {"type": "bench_phase", "phase": phase}
                for phase in (
                    "counter_write",
                    "counter_ack_poll",
                    "counter_verify",
                    "crc_write",
                    "crc_ack_poll",
                )
            ]
        for phase in ("post_read_1", "post_read_2"):
            frames += [{"type": "bench_phase", "phase": phase}, image_frame(expected, phase)]
    frames += [
        {"type": "bench_phase", "phase": "complete"},
        {
            "type": "bench_result",
            "op": action,
            "ok": True,
            "phase": "complete",
            "mutation_state": "none"
            if action == "read"
            else "unchanged"
            if expected == data
            else "verified",
            "preimage_sha256": bench.digest(data),
            "expected_sha256": bench.digest(expected) if action == "restore" else "",
            "postimage_sha256": bench.digest(expected) if action == "restore" else "",
            "reads_identical": True,
            "crcs_valid": True,
            "pins_input_only": True,
            "internal_pulls": False,
        },
        info(),
        netinfo(),
        info(),
    ]
    return frames


class FakeUART:
    def __init__(self, frames: list[dict[str, object]], on_command=None) -> None:
        self.data = bytearray(b"".join((json.dumps(frame) + "\n").encode() for frame in frames))
        self.writes: list[bytes] = []
        self.on_command = on_command

    def read(self, size=1) -> bytes:
        if not self.data:
            return b""
        result = bytes(self.data[:size])
        del self.data[:size]
        return result

    def write(self, data: bytes) -> int:
        self.writes.append(data)
        if self.on_command:
            self.on_command(data)
        return len(data)


class Tick:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        self.value += 0.00001
        return self.value


class BenchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.output = Path(self.temp.name)
        self.data = valid_image()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_frames(self, action="read", frames=None, on_command=None):
        uart = FakeUART(frames if frames is not None else transcript(action, self.data), on_command)
        session = Session(uart, Audit(io.StringIO()), Tick(), expected_firmware=bench.FIRMWARE)
        result = bench.run_bench(
            session,
            self.output,
            action,
            "NONE",
            bench.digest(self.data),
            ELF,
            self.data if action == "restore" else None,
        )
        return result, uart

    def test_read_saves_two_exact_images_and_qualified_receipt(self):
        result, uart = self.run_frames()
        self.assertEqual(result["status"], "verified_read")
        self.assertEqual((self.output / "pre_read_1.bin").read_bytes(), self.data)
        self.assertEqual((self.output / "pre_read_2.bin").read_bytes(), self.data)
        self.assertEqual(
            bench.load_qualified_preimage(self.output, bench.digest(self.data)), self.data
        )
        self.assertIn(b"BENCH_READ ISOLATED NONE\n", uart.writes)
        self.assertFalse(any(b"RESTORE" in value for value in uart.writes))

    def test_restore_durable_plan_and_images_exist_before_command(self):
        observed = []

        def check(command):
            if command.startswith(b"BENCH_RESTORE"):
                self.assertEqual((self.output / "qualified-preimage.bin").read_bytes(), self.data)
                plan, expected = bench.restoration_plan(self.data)
                self.assertEqual(
                    json.loads((self.output / "restore-plan-before-command.json").read_text()), plan
                )
                self.assertEqual((self.output / "expected-postimage.bin").read_bytes(), expected)
                observed.append(True)

        result, _ = self.run_frames("restore", on_command=check)
        self.assertEqual(result["status"], "verified_restore")
        self.assertTrue(observed)
        self.assertEqual(len((self.output / "post_read_2.bin").read_bytes()), 256)

    def test_plan_changes_only_counter_and_crc(self):
        plan, expected = bench.restoration_plan(self.data)
        changed = {index for index in range(256) if expected[index] != self.data[index]}
        self.assertTrue(changed <= {232, 233, 246, 247})
        self.assertEqual(plan["changed_offsets"], sorted(changed))
        self.assertEqual(struct.unpack_from("<H", expected, 232)[0], 6299)
        self.assertTrue(bench.inspect_eeprom(expected)["all_three_crc_regions_match"])

    def test_already_at_initial_remains_unchanged_and_still_has_two_postreads(self):
        self.data = valid_image(remaining=6299)
        result, _ = self.run_frames("restore")
        self.assertEqual(result["device_result"]["mutation_state"], "unchanged")
        self.assertEqual((self.output / "post_read_2.bin").read_bytes(), self.data)

    def test_bad_restore_scope_rejected(self):
        for data in (
            valid_image(initial=0),
            valid_image(initial=65535),
            valid_image(revision=3),
            valid_image(remaining=6300),
        ):
            with self.subTest(data=data[:3]), self.assertRaises(ManagementError):
                bench.restoration_plan(data)

    def test_observed_cartridge_matches_independently_reviewed_restore_hash(self):
        from private_replay import private_folder
        observed = (private_folder("capture-v4-old-ic-full-read-01") /
                    "root-candidate-observed-256.bin").read_bytes()
        self.assertEqual(
            bench.digest(observed),
            "0d35ecd79b8c4165a032bf4632343553d690c882cff8e46dec3eb36aeed5e3e5",
        )
        plan, expected = bench.restoration_plan(observed)
        self.assertEqual(
            bench.digest(expected),
            "523998f111128374fc433f30adbc08585850eb4bec1a24c24524adddd0b7204f",
        )
        self.assertEqual(plan["changed_offsets"], [232, 233, 246, 247])

    def test_qualifier_rejects_short_hash_and_crc_mismatches(self):
        for field, value in (
            ("bytes", 255),
            ("hex", self.data.hex()[:-2]),
            ("sha256", "b" * 64),
            ("crc_r0", False),
        ):
            frame = image_frame(self.data, "pre_read_1")
            frame[field] = value
            with self.subTest(field=field), self.assertRaises(ManagementError):
                bench.qualify_image(frame)
        corrupt = bytearray(self.data)
        corrupt[7] ^= 1
        with self.assertRaises(ManagementError):
            bench.qualify_image(image_frame(bytes(corrupt), "pre_read_1"))

    def test_fresh_second_image_differs_but_valid_crcs(self):
        frames = transcript("read", self.data)
        for index, frame in enumerate(frames):
            if frame.get("type") == "bench_image" and frame.get("phase") == "pre_read_2":
                frames[index] = image_frame(valid_image(remaining=3), "pre_read_2")
        with self.assertRaises(ManagementError):
            self.run_frames(frames=frames)
        self.assertFalse((self.output / "receipt.json").exists())

    def test_wrong_device_plan_rejected_no_retry(self):
        frames = transcript("restore", self.data)
        next(frame for frame in frames if frame.get("type") == "bench_plan")["changed_offsets"] = [
            0
        ]
        with self.assertRaises(ManagementError):
            self.run_frames("restore", frames)
        failure = json.loads((self.output / "failure.json").read_text())
        self.assertTrue(failure["eeprom_mutation_possible"])
        self.assertFalse(failure["automatic_retry"])

    def test_unrelated_postimage_change_rejected_even_with_valid_crcs(self):
        frames = transcript("restore", self.data)
        _, expected = bench.restoration_plan(self.data)
        changed = bytearray(expected)
        changed[0x20] ^= 1
        struct.pack_into("<H", changed, 0xAE, crc16_x25(changed[:0xAE]))
        for index, frame in enumerate(frames):
            if frame.get("type") == "bench_image" and frame.get("phase") == "post_read_1":
                frames[index] = image_frame(bytes(changed), "post_read_1")
        with self.assertRaises(ManagementError):
            self.run_frames("restore", frames)

    def test_partial_write_failure_retains_uncertainty(self):
        frames = transcript("restore", self.data)
        result = next(frame for frame in frames if frame.get("type") == "bench_result")
        result.update(ok=False, mutation_state="possible_partial", phase="crc_ack_poll")
        with self.assertRaises(ManagementError):
            self.run_frames("restore", frames)
        failure = json.loads((self.output / "failure.json").read_text())
        self.assertEqual(failure["device_result"]["mutation_state"], "possible_partial")
        self.assertTrue(failure["eeprom_mutation_possible"])

    def test_reordered_images_and_write_before_plan_rejected(self):
        frames = transcript("restore", self.data)
        frames.insert(4, {"type": "bench_phase", "phase": "counter_write"})
        with self.assertRaises(ManagementError):
            self.run_frames("restore", frames)

    def test_post_operation_identity_change_rejected(self):
        frames = transcript("read", self.data)
        frames[-2]["app_elf_sha256"] = "b" * 64
        with self.assertRaises(ManagementError):
            self.run_frames(frames=frames)

    def test_wrong_running_elf_never_sends_bench_command(self):
        uart = FakeUART([info(), {**netinfo(), "app_elf_sha256": "b" * 64}])
        session = Session(uart, Audit(io.StringIO()), Tick(), expected_firmware=bench.FIRMWARE)
        with self.assertRaises(ManagementError):
            bench.run_bench(session, self.output, "read", "NONE", bench.digest(self.data), ELF)
        self.assertFalse(any(value.startswith(b"BENCH_") for value in uart.writes))

    def test_prior_file_changed_after_read_rejected(self):
        self.run_frames()
        (self.output / "pre_read_2.bin").write_bytes(valid_image(remaining=3))
        with self.assertRaises(ManagementError):
            bench.load_qualified_preimage(self.output, bench.digest(self.data))

    def test_passive_candidate_without_isolated_receipt_rejected(self):
        (self.output / "pre_read_1.bin").write_bytes(self.data)
        with self.assertRaises(FileNotFoundError):
            bench.load_qualified_preimage(self.output, bench.digest(self.data))

    def test_framing_bound_malformed_and_deadline(self):
        for raw in (b"x" * 4097, b"{broken}\n"):
            uart = FakeUART([])
            uart.data = bytearray(raw)
            session = Session(uart, Audit(io.StringIO()), Tick(), expected_firmware=bench.FIRMWARE)
            with self.assertRaises(ManagementError):
                bench.receive_frame(session, 10)
        uart = FakeUART([])
        session = Session(uart, Audit(io.StringIO()), Tick(), expected_firmware=bench.FIRMWARE)
        with self.assertRaises(ManagementError):
            bench.receive_frame(session, 0.01)


if __name__ == "__main__":
    unittest.main()
