"""Focused V9 action/provenance regressions; frozen V8 framing is reused."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

import manage_emulator_v9 as v9
import test_diagnose_emulator_v8 as fixtures
import test_manage_emulator as old
from manage_esp32 import Audit, ManagementError, Session
from test_bench_eeprom import Tick

ELF = "e" * 64


def bundle() -> Path:
    from private_replay import private_folder
    return private_folder("emulator-v9-adoption-source-01")


def current(data: bytes, *, unclean: bool = True) -> dict:
    result = fixtures.current_state(data)
    result.update(
        firmware=v9.FIRMWARE, unclean_boot=unclean, session_active_marker=unclean
    )
    return result


def probe(state: dict) -> list[dict]:
    frames = fixtures.probe(state)
    frames[0].update(firmware=v9.FIRMWARE, pins_input_only=state["pins_input_only"])
    frames[1].update(firmware=v9.FIRMWARE, app_elf_sha256=ELF)
    frames[2]["type"] = "emu_status"
    return frames


def regs() -> dict:
    return {**fixtures.registers(), "fifo_conf": 513, "raw_interrupts": 0}


def diag_receipt(data: bytes, state: dict) -> dict:
    snapshot = old.image_frame(data, state)
    return {
        "action": "diag",
        "status": "detached_diagnostic_verified",
        "firmware": v9.FIRMWARE,
        "app_elf_sha256": ELF,
        "automatic_retry": False,
        "before": snapshot,
        "after": snapshot,
        "phases": [
            {"type": "emu_diag_phase", "op": "diag", "phase": name, **regs()}
            for name in v9.evidence.PHASES
        ],
        "result": {
            "type": "emu_diag",
            "ok": True,
            "message": "detached_diagnostic_complete",
            "outputs_enabled": False,
            "observation_ms": 5,
            "one_shot_irq_count": 0,
            "observed_irq": 0,
            "observed_status": 0,
            **regs(),
            **state,
        },
    }


class UART(fixtures.CommandUART):
    def write(self, data):
        if not data.startswith(b"EMU_START "):
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


class V9Tests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name)
        self.proof = v9.AdoptionProof.load(bundle(), v9.ADOPTION_MANIFEST)
        self.data = self.proof.files["v8-after.bin"]
        self.current = current(self.data)
        self.diagnostic = v9.DiagnosticProof(
            json.dumps(diag_receipt(self.data, self.current)).encode()
        )

    def control(self, frames, callback=None):
        uart = UART(frames, callback)
        session = Session(
            uart, Audit(io.StringIO()), Tick(), expected_firmware=v9.FIRMWARE
        )
        return v9.Control(session, self.output, ELF, v9.ADOPTION_SEED), uart

    def adoption_frames(self):
        after = current(self.data, unclean=False)
        result = {
            "type": "emu_result",
            "op": "adopt_durable",
            "ok": True,
            "message": "durable_checkpoint_adopted_volatile_history_unknown",
            "adoption_policy": "V7_START_RESET",
            "adopted_durable": True,
            "pre_reset_volatile_history_unknown": True,
            "prior_marker": True,
            "prior_unclean": True,
            "prior_reset_reason": self.current["reset_reason"],
            "prior_boot_breadcrumb": self.current["boot_breadcrumb"],
            **after,
        }
        return [
            *probe(self.current),
            old.image_frame(self.data, self.current),
            result,
            old.image_frame(self.data, after),
            *probe(after),
        ]

    def start_frames(self):
        clean = current(self.data, unclean=False)
        pending = {
            **clean,
            "session_active_marker": True,
            "session_open": True,
            "image_sha256": "",
        }
        active = {
            **pending,
            "emulation_active": True,
            "pins_input_only": False,
            "sda_open_drain": True,
        }
        return [
            *probe(clean),
            old.image_frame(self.data, clean),
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
                **regs(),
                **pending,
            },
            {
                "type": "emu_result",
                "op": "start",
                "ok": True,
                "message": "experimental_responder_enabled",
                **active,
            },
        ]

    def test_actual_provenance_has_all12_raw_frames_and_no_v8_success_claim(self):
        self.assertEqual(len(self.proof.files), 19)
        self.assertEqual(
            json.loads(self.proof.files["v8-diagnostic-failure.json"])["status"],
            "unverified",
        )
        self.assertEqual(v9.digest(self.data), v9.ADOPTION_SEED)
        self.assertEqual(int.from_bytes(self.data[232:234], "little"), 6256)

    def test_diagnostic_threshold1_and_postcleanup_registers_verify(self):
        receipt = diag_receipt(self.data, self.current)
        frames = [
            *probe(self.current),
            receipt["before"],
            *receipt["phases"],
            receipt["result"],
            receipt["after"],
            *probe(self.current),
        ]
        control, uart = self.control(frames)
        result = control.run_action("diag")
        self.assertEqual(result["status"], "detached_diagnostic_verified")
        self.assertEqual(uart.writes.count(b"EMU_DIAG DETACHED\n"), 1)

    def test_threshold0_is_retained_but_rejected_without_retry(self):
        receipt = diag_receipt(self.data, self.current)
        receipt["phases"][0]["fifo_conf"] = 512
        control, uart = self.control(
            [
                *probe(self.current),
                receipt["before"],
                *receipt["phases"],
                receipt["result"],
            ]
        )
        with self.assertRaises(ManagementError):
            control.run_action("diag")
        self.assertEqual(uart.writes.count(b"EMU_DIAG DETACHED\n"), 1)
        self.assertEqual(
            json.loads((self.output / "frame-004.json").read_text())["fifo_conf"], 512
        )

    def test_adoption_exports_plan_and_exact_evidence_before_one_command(self):
        def check(command):
            if command.startswith(b"EMU_ADOPT_DURABLE "):
                self.assertTrue(
                    (self.output / "adoption-plan-before-command.json").exists()
                )
                self.assertEqual(
                    (self.output / "before-adopt.bin").read_bytes(), self.data
                )
                self.assertTrue(
                    (self.output / "reviewed-v9-diagnostic-receipt.json").exists()
                )
                self.assertEqual(
                    len(list((self.output / "reviewed-adoption-evidence").iterdir())),
                    20,
                )

        control, uart = self.control(self.adoption_frames(), check)
        result = control.run_action(
            "adopt", proof=self.proof, diagnostic=self.diagnostic, ack_unknown=True
        )
        self.assertEqual(
            result["status"], "last_durable_adopted_volatile_history_unknown"
        )
        self.assertTrue(result["pre_reset_volatile_history_unknown"])
        self.assertEqual((self.output / "after-adopt.bin").read_bytes(), self.data)
        self.assertEqual(result["after"]["durable_generation"], 1)
        self.assertEqual(
            sum(x.startswith(b"EMU_ADOPT_DURABLE ") for x in uart.writes), 1
        )

    def test_adoption_without_explicit_unknown_ram_ack_sends_no_adopt(self):
        control, uart = self.control(self.adoption_frames())
        with self.assertRaises(ManagementError):
            control.run_action("adopt", proof=self.proof, diagnostic=self.diagnostic)
        self.assertFalse(any(x.startswith(b"EMU_ADOPT_DURABLE ") for x in uart.writes))

    def test_adoption_requires_successful_pinned_v9_diagnostic(self):
        wrong = diag_receipt(self.data, self.current)
        wrong["result"]["outputs_enabled"] = True
        control, uart = self.control(self.adoption_frames())
        with self.assertRaises(ManagementError):
            control.run_action(
                "adopt",
                proof=self.proof,
                diagnostic=v9.DiagnosticProof(json.dumps(wrong).encode()),
                ack_unknown=True,
            )
        self.assertFalse(any(x.startswith(b"EMU_ADOPT_DURABLE ") for x in uart.writes))

    def test_adoption_uncertain_checkpoint_preserves_failure_not_rollback(self):
        frames = self.adoption_frames()
        frames[4].update(
            ok=False,
            message="checkpoint_failed_outcome_uncertain",
            adopted_durable=False,
            **self.current,
        )
        frames[5] = old.image_frame(self.data, self.current)
        frames[6:] = probe(self.current)
        control, uart = self.control(frames)
        with self.assertRaises(ManagementError):
            control.run_action(
                "adopt", proof=self.proof, diagnostic=self.diagnostic, ack_unknown=True
            )
        failure = json.loads((self.output / "failure.json").read_text())
        self.assertEqual(
            failure["result"]["message"], "checkpoint_failed_outcome_uncertain"
        )
        self.assertTrue(failure["command_may_have_reached_device"])
        self.assertEqual(
            sum(x.startswith(b"EMU_ADOPT_DURABLE ") for x in uart.writes), 1
        )

    def test_start_saves_two_preoutput_phases_and_leaves_active_on_success(self):
        control, uart = self.control(self.start_frames())
        result = control.run_action("start", sole=True)
        self.assertEqual(result["status"], "started_experimental")
        self.assertEqual(
            [x["phase"] for x in result["phases"]], ["marker_saved", "fifo_loaded"]
        )
        self.assertTrue(result["result"]["emulation_active"])
        self.assertFalse(any(x == b"EMU_STOP\n" for x in uart.writes))

    def test_start_timeout_retains_partial_panic_and_never_retries_or_auto_stops(self):
        control, uart = self.control(self.start_frames()[:-1])
        uart.diagnostic_suffix = b"Guru Meditation Error\nBacktrace: 0x4008"
        with self.assertRaises(ManagementError):
            control.run_action("start", sole=True)
        raw = b"".join(
            p.read_bytes() for p in sorted(self.output.glob("raw-line-*.bin"))
        )
        self.assertIn(b"Guru Meditation Error\n", raw)
        self.assertIn(b"Backtrace: 0x4008", raw)
        self.assertEqual(sum(x.startswith(b"EMU_START ") for x in uart.writes), 1)
        self.assertNotIn(b"EMU_STOP\n", uart.writes)
        failure = json.loads((self.output / "failure.json").read_text())
        self.assertEqual(len(failure["phases"]), 2)
        self.assertTrue(failure["unknown_pre_reset_volatile_writes_not_excluded"])

    def test_stop_sends_once_even_when_optional_snapshot_is_busy(self):
        clean = current(self.data, unclean=False)
        active = self.start_frames()[-1]
        stopped = {
            "type": "emu_result",
            "op": "stop",
            "ok": True,
            "message": "detached_and_durable",
            **clean,
        }
        frames = [
            *probe(active),
            {
                "type": "emu_snapshot",
                "ok": False,
                "message": "snapshot_busy_or_unloaded",
            },
            stopped,
            old.image_frame(self.data, clean),
        ]
        control, uart = self.control(frames)
        result = control.run_action("stop")
        self.assertEqual(result["status"], "stopped_checkpointed")
        self.assertIsNotNone(result["pre_stop_snapshot_error"])
        self.assertEqual(uart.writes.count(b"EMU_STOP\n"), 1)

    def test_status_after_fault_retains_open_session_and_irq_history(self):
        fault = {
            **self.current,
            "unclean_boot": False,
            "session_open": True,
            "image_sha256": "",
            "last_fault_code": 15,
            "last_fault": "empty_rx_irq",
            "irq_history_available": True,
            "irq_history": [{"flags": 1, "hw_status": 0, "rx_fifo": 0, "tx_fifo": 32}],
        }
        control, _ = self.control([*probe(fault), old.image_frame(self.data, fault)])
        result = control.run_action("status")
        self.assertTrue(result["after"]["session_open"])
        self.assertEqual(result["result"]["irq_history"][0]["flags"], 1)

    def test_wrong_elf_fails_before_adopt_or_diag_or_start(self):
        frames = probe(self.current)
        frames[1]["app_elf_sha256"] = "0" * 64
        control, uart = self.control(frames)
        with self.assertRaises(ManagementError):
            control.run_action("diag")
        self.assertEqual(uart.writes, [b"INFO\n", b"NETINFO\n"])


if __name__ == "__main__":
    unittest.main()
