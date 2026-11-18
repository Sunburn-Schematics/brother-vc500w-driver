"""Focused V13 fake-UART policy and exact checkpoint-recovery regressions."""

from __future__ import annotations

import copy
import io
import json
import tempfile
import unittest
from pathlib import Path

import manage_emulator_v13 as v13
import test_manage_emulator_v12 as prior
from inspect_eeprom import REGIONS, crc16_x25, inspect_eeprom
from manage_esp32 import Audit, ManagementError, Session
from test_bench_eeprom import Tick, valid_image

ELF = "f" * 64


def proof_folder() -> Path:
    from private_replay import private_folder

    return private_folder("emulator-v13-stop-recovery-source-01")


def counters(
    data: bytes, view: bytes, *, enabled: bool = False, generation: int = 0
) -> dict:
    return {
        "mode": "initialization_budget_v1",
        "enabled": enabled,
        "capacity_counts": 11232 if enabled else 0,
        "persisted": False,
        "view_generation": generation,
        "refresh_events": 0,
        "backing_initial_raw": int.from_bytes(data[0x60:0x62], "little"),
        "backing_remaining_raw": int.from_bytes(data[0xE8:0xEA], "little"),
        "advertised_initial_raw": int.from_bytes(view[0x60:0x62], "little"),
        "advertised_remaining_raw": int.from_bytes(view[0xE8:0xEA], "little"),
        "write_verification_pending": False,
        "cache_refresh_required": enabled,
        "suspended": False,
    }


def current(data: bytes, *, enabled: bool = False, view: bytes | None = None) -> dict:
    result = prior.current(data)
    result.update(firmware=v13.FIRMWARE, live_generation=43, durable_generation=43)
    result["policy"] = counters(
        data, view or data, enabled=enabled, generation=int(enabled)
    )
    return result


def probe(value: dict) -> list[dict]:
    frames = prior.probe(value)
    frames[0]["firmware"] = frames[1]["firmware"] = v13.FIRMWARE
    frames[1]["app_elf_sha256"] = ELF
    return frames


def image(data: bytes, value: dict) -> dict:
    return prior.fixture.old.image_frame(data, value)


def view_image(data: bytes, value: dict) -> dict:
    return {
        **image(data, value),
        "type": "emu_view_snapshot",
        "image_kind": "advertised",
        "image_generation": value["policy"]["view_generation"],
    }


class Tests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.output = Path(temporary.name)
        self.data = valid_image(remaining=5485)
        self.view = v13.advertised_budget(self.data, 11232)
        self.clean = current(self.data)
        self.enabled = current(self.data, enabled=True, view=self.view)

    def control(self, frames: list[dict], callback=None, *, data: bytes | None = None):
        uart = prior.UART(frames, callback)
        session = Session(
            uart, Audit(io.StringIO()), Tick(), expected_firmware=v13.FIRMWARE
        )
        return v13.Control(
            session, self.output, ELF, v13.digest(data or self.data)
        ), uart

    def transition(
        self, before: dict, after: dict, data: bytes, view: bytes, enabled: bool
    ) -> list[dict]:
        return [
            *probe(before),
            image(data, before),
            {
                "type": "emu_result",
                "op": "policy_enable" if enabled else "policy_disable",
                "ok": True,
                **after,
            },
            image(data, after),
            view_image(view, after),
            *probe(after),
        ]

    def test_budget_changes_only_eight_allowed_bytes_and_both_crc_regions(self) -> None:
        self.assertEqual(int.from_bytes(self.view[0x60:0x62], "little"), 11232)
        self.assertEqual(int.from_bytes(self.view[0xE8:0xEA], "little"), 11232)
        self.assertTrue(inspect_eeprom(self.view)["all_three_crc_regions_match"])
        changed = {
            i
            for i, (a, b) in enumerate(zip(self.data, self.view, strict=True))
            if a != b
        }
        self.assertLessEqual(changed, {0x60, 0x61, 0xAE, 0xAF, 0xE8, 0xE9, 0xF6, 0xF7})
        self.assertEqual(self.view[0xD0:0xE8], self.data[0xD0:0xE8])
        for capacity in (0, 11233, 65535, True, -1):
            with self.subTest(capacity=capacity), self.assertRaises(ManagementError):
                v13.advertised_budget(self.data, capacity)
        with self.assertRaises(ManagementError):
            v13.advertised_budget(valid_image(revision=3), 100)

    def test_enable_saves_preimage_and_plan_before_exact_one_command(self) -> None:
        def callback(command: bytes) -> None:
            if command.startswith(b"EMU_POLICY "):
                self.assertTrue(
                    (self.output / "policy-plan-before-command.json").exists()
                )
                self.assertEqual(
                    (self.output / "before-policy-enable.bin").read_bytes(), self.data
                )

        control, uart = self.control(
            self.transition(self.clean, self.enabled, self.data, self.view, True),
            callback,
        )
        receipt = control.run_action("policy-enable", generation=43)
        self.assertEqual(receipt["status"], "policy_transition_verified")
        self.assertEqual(
            (self.output / "after-policy-enable.bin").read_bytes(), self.data
        )
        self.assertEqual(
            (self.output / "after-policy-enable-advertised-view.bin").read_bytes(),
            self.view,
        )
        self.assertEqual(
            [x for x in uart.writes if x.startswith(b"EMU_POLICY ")],
            [f"EMU_POLICY ENABLE {v13.digest(self.data)} 43 11232\n".encode()],
        )

    def test_invalid_state_or_generation_never_sends_policy_command(self) -> None:
        for changes in (
            {"unclean_boot": True},
            {"dirty": True},
            {"last_fault_code": 9},
            {"session_active_marker": True},
            {"live_generation": 44},
            {"pullup_control": prior.pullup(True)},
        ):
            with self.subTest(changes=changes):
                control, uart = self.control([])
                with self.assertRaises(ManagementError):
                    control.set_policy({**self.clean, **changes}, 43, True, 11232)
                self.assertEqual(uart.writes, [])

    def test_disable_preserves_newest_backing_instead_of_old_seed(self) -> None:
        newest = valid_image(remaining=11189)
        before = current(newest, enabled=True, view=newest)
        before["seed_sha256"] = v13.digest(self.data)
        after = current(newest)
        after["seed_sha256"] = v13.digest(self.data)
        control, uart = self.control(
            self.transition(before, after, newest, newest, False), data=newest
        )
        receipt = control.run_action("policy-disable", generation=43)
        self.assertEqual(receipt["after"]["sha256"], v13.digest(newest))
        self.assertEqual(
            (self.output / "after-policy-disable.bin").read_bytes(), newest
        )
        self.assertEqual(
            sum(x.startswith(b"EMU_POLICY DISABLE ") for x in uart.writes), 1
        )

    def test_contradictory_view_or_policy_is_rejected_after_retaining_raw(self) -> None:
        frames = self.transition(self.clean, self.enabled, self.data, self.view, True)
        frames[6]["policy"] = {**self.enabled["policy"], "advertised_remaining_raw": 1}
        control, uart = self.control(frames)
        with self.assertRaises(ManagementError):
            control.run_action("policy-enable", generation=43)
        self.assertTrue((self.output / "failure.json").exists())
        self.assertTrue(list(self.output.glob("raw-line-*.bin")))
        self.assertEqual(
            sum(x.startswith(b"EMU_POLICY ENABLE ") for x in uart.writes), 1
        )

    def test_policy_is_typed_and_cannot_claim_persistence(self) -> None:
        for changes in (
            {"persisted": True},
            {"enabled": 1},
            {"capacity_counts": True},
            {"capacity_counts": 11233},
            {"view_generation": -1},
        ):
            with self.subTest(changes=changes), self.assertRaises(ManagementError):
                v13.state({**self.clean, "policy": {**self.clean["policy"], **changes}})

    def test_enable_rejects_suspended_pending_or_unreported_cache_refresh(self) -> None:
        for field, invalid in (
            ("suspended", True),
            ("write_verification_pending", True),
            ("cache_refresh_required", False),
        ):
            with self.subTest(field=field):
                result = {
                    "type": "emu_result",
                    "op": "policy_enable",
                    "ok": True,
                    **self.enabled,
                    "policy": {**self.enabled["policy"], field: invalid},
                }
                directory = self.output / field
                directory.mkdir()
                uart = prior.UART([result])
                control = v13.Control(
                    Session(
                        uart,
                        Audit(io.StringIO()),
                        Tick(),
                        expected_firmware=v13.FIRMWARE,
                    ),
                    directory,
                    ELF,
                    v13.digest(self.data),
                )
                with self.assertRaises(ManagementError):
                    control.set_policy(self.clean, 43, True, 11232)
                self.assertEqual(
                    sum(x.startswith(b"EMU_POLICY ENABLE ") for x in uart.writes), 1
                )

    def test_passive_final_policy_drift_is_rejected_even_with_identical_image(
        self,
    ) -> None:
        frames = self.transition(self.clean, self.enabled, self.data, self.view, True)
        frames[-1]["policy"] = {**self.enabled["policy"], "refresh_events": 5}
        control, uart = self.control(frames)
        with self.assertRaises(ManagementError):
            control.run_action("policy-enable", generation=43)
        self.assertEqual(
            sum(x.startswith(b"EMU_POLICY ENABLE ") for x in uart.writes), 1
        )
        self.assertTrue((self.output / "failure.json").exists())

    def test_wrong_elf_or_image_pin_rejects_enable_before_mutation(self) -> None:
        root = self.output
        for bad_elf in (True, False):
            with self.subTest(bad_elf=bad_elf):
                self.output = root / str(bad_elf)
                self.output.mkdir()
                frames = self.transition(
                    self.clean, self.enabled, self.data, self.view, True
                )
                if bad_elf:
                    frames[1]["app_elf_sha256"] = "0" * 64
                control, uart = self.control(frames)
                if not bad_elf:
                    control.image_pin = "0" * 64
                with self.assertRaises(ManagementError):
                    control.run_action("policy-enable", generation=43)
                self.assertFalse(any(x.startswith(b"EMU_POLICY ") for x in uart.writes))

    def test_panic_after_policy_command_is_retained_and_never_retried(self) -> None:
        def callback(command: bytes) -> None:
            if command.startswith(b"EMU_POLICY "):
                uart.data.extend(b"Guru Meditation Error\n")

        control, uart = self.control(
            self.transition(self.clean, self.enabled, self.data, self.view, True),
            callback,
        )
        with self.assertRaises(ManagementError):
            control.run_action("policy-enable", generation=43)
        self.assertEqual(sum(x.startswith(b"EMU_POLICY ") for x in uart.writes), 1)
        self.assertTrue(
            any(
                b"Guru Meditation Error" in p.read_bytes()
                for p in self.output.glob("raw-line-*.bin")
            )
        )
        self.assertFalse(
            json.loads((self.output / "failure.json").read_text())["automatic_retry"]
        )

    def test_two_label_low_budget_requires_initialization_to_refresh_cached_ram(
        self,
    ) -> None:
        budget = v13.advertised_budget(self.data, 64)
        cached_remaining = int.from_bytes(budget[0xE8:0xEA], "little")
        cached_remaining -= 43
        first_write = bytearray(budget)
        first_write[0xE8:0xEA] = cached_remaining.to_bytes(2, "little")
        for _, start, length, target in REGIONS:
            first_write[target : target + 2] = crc16_x25(
                first_write[start : start + length]
            ).to_bytes(2, "little")
        refreshed_view = v13.advertised_budget(bytes(first_write), 64)
        self.assertEqual(cached_remaining, 21)
        self.assertEqual(int.from_bytes(refreshed_view[0xE8:0xEA], "little"), 64)
        self.assertLess(cached_remaining, 43)
        cached_remaining = int.from_bytes(refreshed_view[0xE8:0xEA], "little")
        self.assertGreater(cached_remaining - 43, 0)

    def test_actual_stop_proof_preserves_failed_stop_and_latest_checkpoint(
        self,
    ) -> None:
        proof = v13.StopProof.load(proof_folder(), v13.STOP_PROOF_MANIFEST)
        manifest = json.loads(proof.manifest)
        self.assertFalse(manifest["historical_stop_result_ok"])
        self.assertTrue(manifest["closed_checkpoint_verified"])
        self.assertTrue(manifest["uncommitted_bus_activity_not_excluded"])
        self.assertEqual(proof.files["before.bin"], proof.files["after.bin"])
        damaged = copy.deepcopy(proof.files)
        damaged["after.bin"] = damaged["after.bin"][:-1]
        with self.assertRaises(ManagementError):
            v13.StopProof.qualify(damaged)

    def test_exact_stop_recovery_changes_only_fault_metadata(self) -> None:
        proof = v13.StopProof.load(proof_folder(), v13.STOP_PROOF_MANIFEST)
        data = proof.files["after.bin"]
        before = current(data)
        before.update(unclean_boot=True, last_fault_code=9, last_fault="stop_timeout")
        after = current(data)
        frames = [
            *probe(before),
            image(data, before),
            {
                "type": "emu_result",
                "op": "recover_stop_checkpoint",
                "ok": True,
                **after,
            },
            image(data, after),
            view_image(data, after),
            *probe(after),
        ]
        control, uart = self.control(frames, data=data)
        receipt = control.run_action("recover-stop", generation=43, proof=proof)
        self.assertEqual(receipt["after"]["sha256"], v13.STOP_PROOF_IMAGE)
        self.assertEqual(receipt["after"]["durable_generation"], 43)
        self.assertEqual(
            sum(x.startswith(b"EMU_RECOVER_STOP_CHECKPOINT ") for x in uart.writes), 1
        )

    def test_status_does_not_enable_policy_and_saves_both_full_images(self) -> None:
        control, uart = self.control(
            [
                *probe(self.clean),
                image(self.data, self.clean),
                view_image(self.data, self.clean),
            ]
        )
        receipt = control.run_action("status")
        self.assertEqual(receipt["status"], "status_and_snapshot_saved")
        self.assertEqual(
            (self.output / "status-advertised-view.bin").read_bytes(), self.data
        )
        self.assertFalse(any(x.startswith(b"EMU_POLICY ") for x in uart.writes))


if __name__ == "__main__":
    unittest.main()
