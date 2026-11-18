"""Exact V9 diagnostics and explicit last-durable adoption with raw UART evidence.

Template: frozen V8 EvidenceReader and V7 common state/snapshot validators.
Each invocation performs one explicit action. No retries, implicit recovery,
counter reset, image loading or capture command is provided. Current reset
reason describes V9 only; retained valid breadcrumbs describe the previous V8+
stage, not the earlier V7 reset cause. Adoption acknowledges unknown lost RAM.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import diagnose_emulator_v8 as evidence
import manage_emulator_v7 as v7
from bench_eeprom import DurableAudit, digest, durable_bytes, durable_json, require_hash
from manage_esp32 import ManagementError, Session, validate_device

FIRMWARE = "i2c-eeprom-emulator-v9"
CAPABILITY = evidence.CAPABILITY
SCHEMA = "sunburn.brother.emulator-v9.v1"
ADOPTION_MANIFEST = "078eb153c5bd4c23ec0a24ee2235cc70c80b8e05fbc74e9be61abb03968f0360"
ADOPTION_SEED = "d7c0db4edb0db564e8980ba26b8ebf6f95082685aa4e0b40c8c8630b4715e091"
ADOPTION_FILES = {
    "v7-start-failure.json",
    "v7-start-before.bin",
    "v7-stop-receipt.json",
    "v7-stop-after.bin",
    "v8-diagnostic-failure.json",
    "v8-before.bin",
    "v8-after.bin",
    *(f"v8-raw-{index:03d}.bin" for index in range(12)),
}


def state(frame: dict[str, object]) -> dict[str, object]:
    if frame.get("firmware") != FIRMWARE:
        raise ManagementError("Unexpected V9 firmware identity")
    v7.state({**frame, "firmware": v7.FIRMWARE})
    v7.integer(frame.get("reset_reason"), "reset_reason")
    crumb = frame.get("boot_breadcrumb")
    if not isinstance(crumb, dict) or type(crumb.get("valid")) is not bool:
        raise ManagementError("Missing typed V9 reset breadcrumb")
    stage = v7.integer(crumb.get("stage"), "breadcrumb.stage")
    inverse = v7.integer(crumb.get("stage_inverse"), "breadcrumb.stage_inverse")
    if crumb["valid"] and stage ^ inverse != 0xFFFFFFFF:
        raise ManagementError("Invalid retained breadcrumb inverse")
    diagnostics = frame.get("isr_diagnostics")
    if not isinstance(diagnostics, dict):
        raise ManagementError("Missing V9 ISR diagnostics")
    for name in (
        "isr_calls",
        "last_irq_flags",
        "last_rx_count",
        "last_tx_count",
        "irq_storms",
        "empty_rx_irqs",
    ):
        v7.integer(diagnostics.get(name), name)
    return frame


def passive(frame: dict[str, object]) -> None:
    state(frame)
    if (
        frame["emulation_active"]
        or frame["session_open"]
        or not frame["pins_input_only"]
    ):
        raise ManagementError("Operation requires a closed passive emulator")


def registers(frame: dict[str, object]) -> None:
    evidence.registers(frame)
    if frame["fifo_conf"] & 31 != 1:
        raise ManagementError("V9 RX FIFO threshold is not the exact reviewed value1")


@dataclass(frozen=True)
class AdoptionProof:
    manifest: bytes
    files: dict[str, bytes]

    @classmethod
    def load(cls, directory: Path, pin: str) -> AdoptionProof:
        if require_hash(pin) != ADOPTION_MANIFEST:
            raise ManagementError("Adoption requires the exact reviewed event manifest")
        with (directory / "manifest.json").open("rb") as stream:
            manifest = stream.read(131073)
        if len(manifest) > 131072 or digest(manifest) != ADOPTION_MANIFEST:
            raise ManagementError("Adoption manifest differs from the exact event pin")
        description = json.loads(manifest)
        entries = description.get("files")
        if (
            not isinstance(entries, dict)
            or set(entries) != ADOPTION_FILES
            or description.get("event")
            != "V7_START_RESET_LAST_DURABLE_ACK_VOLATILE_UNKNOWN"
            or description.get("image_sha256") != ADOPTION_SEED
            or type(description.get("generation")) is not int
            or description["generation"] != 1
        ):
            raise ManagementError(
                "Adoption event inventory or checkpoint is unexpected"
            )
        files = {}
        for name, expected in entries.items():
            with (directory / name).open("rb") as stream:
                data = stream.read(131073)
            if (
                len(data) > 131072
                or len(data) != expected["bytes"]
                or digest(data) != require_hash(expected["sha256"])
            ):
                raise ManagementError(
                    "Adoption evidence file differs from its exact pin"
                )
            files[name] = data
        cls.qualify(files)
        return cls(manifest, files)

    @staticmethod
    def qualify(files: dict[str, bytes]) -> None:
        if set(files) != ADOPTION_FILES:
            raise ManagementError("Adoption evidence inventory is incomplete")
        for name in (
            "v7-start-before.bin",
            "v7-stop-after.bin",
            "v8-before.bin",
            "v8-after.bin",
        ):
            if len(files[name]) != 256 or digest(files[name]) != ADOPTION_SEED:
                raise ManagementError("Adoption event changed its stored checkpoint")
        start = json.loads(files["v7-start-failure.json"])
        stop = json.loads(files["v7-stop-receipt.json"])
        diag = json.loads(files["v8-diagnostic-failure.json"])
        if (
            start.get("command_may_have_reached_device") is not True
            or stop.get("status") != "stopped_unclean_state_preserved"
            or stop["result"].get("message") != "already_passive_marker_preserved"
            or diag.get("status") != "unverified"
            or diag.get("raw_lines_saved") != 12
        ):
            raise ManagementError(
                "Adoption provenance does not describe the reviewed reset event"
            )
        raw = [json.loads(files[f"v8-raw-{i:03d}.bin"]) for i in range(12)]
        if (
            raw[3] != diag["before"]
            or raw[7] != diag["result"]
            or raw[8] != diag["after"]
            or raw[11] != diag["last_device_frame"]
        ):
            raise ManagementError(
                "V8 raw frames do not match their saved diagnostic context"
            )
        for current in (stop["result"], diag["before"], diag["after"]):
            if (
                current.get("emulation_active") is not False
                or current.get("session_open") is not False
                or current.get("dirty") is not False
                or current.get("unclean_boot") is not True
                or current.get("session_active_marker") is not True
                or current.get("last_fault_code") != 0
                or current.get("image_sha256") != ADOPTION_SEED
                or current.get("seed_sha256") != ADOPTION_SEED
                or current.get("live_generation") != 1
                or current.get("durable_generation") != 1
            ):
                raise ManagementError(
                    "Stored reset event is not the exact marked durable checkpoint"
                )

    def save(self, output: Path) -> None:
        target = output / "reviewed-adoption-evidence"
        target.mkdir()
        durable_bytes(target / "manifest.json", self.manifest)
        for name, data in self.files.items():
            durable_bytes(target / name, data)


@dataclass(frozen=True)
class DiagnosticProof:
    data: bytes

    @classmethod
    def load(cls, path: Path, pin: str) -> DiagnosticProof:
        with path.open("rb") as stream:
            data = stream.read(131073)
        if len(data) > 131072 or digest(data) != require_hash(pin):
            raise ManagementError(
                "V9 diagnostic receipt differs from its exact supplied pin"
            )
        return cls(data)

    def qualify(self, current: dict, elf: str) -> None:
        receipt = json.loads(self.data)
        if (
            receipt.get("action") != "diag"
            or receipt.get("status") != "detached_diagnostic_verified"
            or receipt.get("firmware") != FIRMWARE
            or receipt.get("app_elf_sha256") != elf
            or receipt.get("automatic_retry") is not False
        ):
            raise ManagementError(
                "ADOPT requires the successful current V9 detached diagnostic"
            )
        phases = receipt.get("phases")
        if not isinstance(phases, list) or [
            frame.get("phase") for frame in phases
        ] != list(evidence.PHASES):
            raise ManagementError("Pinned V9 diagnostic phases are incomplete")
        result = receipt["result"]
        for frame in [*phases, result]:
            registers(frame)
            if frame["enabled_interrupts"] != 0:
                raise ManagementError("Pinned V9 diagnostic did not disable IRQs")
        if (
            result.get("ok") is not True
            or result.get("outputs_enabled") is not False
            or result.get("message") != "detached_diagnostic_complete"
        ):
            raise ManagementError("Pinned V9 diagnostic failed its final release check")
        for frame in (receipt["before"], receipt["after"]):
            passive(frame)
            image, inspection = v7.snapshot({**frame, "firmware": v7.FIRMWARE})
            if (
                digest(image) != ADOPTION_SEED
                or not inspection["all_three_crc_regions_match"]
            ):
                raise ManagementError(
                    "Pinned V9 diagnostic checkpoint differs from adoption seed"
                )
            evidence.DiagnosticControl.unchanged(frame, current)

    def save(self, output: Path) -> None:
        durable_bytes(output / "reviewed-v9-diagnostic-receipt.json", self.data)


class Control(evidence.DiagnosticControl):
    """Reuse exact raw reader/exchange framing; only V9 action rules differ."""

    def probe(self) -> dict[str, object]:
        info = self.exchange("INFO", "status")
        for name, value in (
            ("firmware", FIRMWARE),
            ("emulator_capability", CAPABILITY),
            ("cpu_mhz", 240),
            ("sda_gpio", 21),
            ("scl_gpio", 22),
            ("armed", False),
        ):
            if type(info.get(name)) is not type(value) or info[name] != value:
                raise ManagementError(
                    "Unexpected V9 identity, CPU or GPIO pin configuration"
                )
        net = self.exchange("NETINFO", "netinfo")
        if (
            net.get("firmware") != FIRMWARE
            or net.get("app_elf_sha256") != self.elf_pin
            or net.get("armed") is not False
            or net.get("update_busy") is not False
        ):
            raise ManagementError(
                "V9 firmware ELF or idle control state differs from pin"
            )
        current = state(self.exchange("EMU_STATUS", "emu_status"))
        if (
            info.get("pins_input_only") != current["pins_input_only"]
            or info.get("internal_pulls") != current["internal_pulls"]
        ):
            raise ManagementError("V9 INFO and emulator pin state disagree")
        return current

    def save_snapshot(self, label: str, *, pinned: bool = False) -> tuple[dict, bytes]:
        frame = state(self.exchange("EMU_SNAPSHOT", "emu_snapshot"))
        data, inspection = v7.snapshot({**frame, "firmware": v7.FIRMWARE})
        durable_bytes(self.output / f"{label}.bin", data)
        durable_json(self.output / f"{label}-inspection.json", inspection)
        if pinned and (
            digest(data) != self.image_pin
            or frame["dirty"]
            or not frame["snapshot_is_current"]
            or frame["image_generation"] != frame["durable_generation"]
            or not inspection["all_three_crc_regions_match"]
        ):
            raise ManagementError(
                "V9 full preimage differs from the exact valid durable pin"
            )
        return frame, data

    def send_once(self, command: str, payload: bytes | None = None) -> None:
        self.command_may_have_reached = True
        self.session.command(command, payload)

    def result(self, op: str) -> dict:
        frame = state(self.reader.receive(self.session.clock() + 10))
        if frame.get("type") != "emu_result" or frame.get("op") != op:
            raise ManagementError("Unexpected V9 operation result")
        return frame

    def diag(self, before: dict) -> tuple[dict, list[dict]]:
        passive(before)
        self.send_once("EMU_DIAG", b"EMU_DIAG DETACHED\n")
        phases = []
        self.phases = phases
        deadline = self.session.clock() + 10
        while True:
            frame = self.reader.receive(deadline)
            if frame.get("type") == "emu_diag":
                break
            if (
                frame.get("type") != "emu_diag_phase"
                or frame.get("op") != "diag"
                or len(phases) >= 3
                or frame.get("phase") != evidence.PHASES[len(phases)]
            ):
                raise ManagementError("Unexpected V9 detached diagnostic phase")
            registers(frame)
            if frame["enabled_interrupts"] != 0:
                raise ManagementError("Detached phase has enabled IRQs")
            phases.append(frame)
        state(frame)
        self.result_frame = frame
        self.unchanged(before, frame)
        if frame.get("ok") is True:
            registers(frame)
            if (
                len(phases) != 3
                or frame.get("message") != "detached_diagnostic_complete"
                or frame.get("outputs_enabled") is not False
                or type(frame.get("observation_ms")) is not int
                or frame["observation_ms"] != 5
                or type(frame.get("one_shot_irq_count")) is not int
                or frame["one_shot_irq_count"] not in (0, 1)
                or frame["enabled_interrupts"] != 0
            ):
                raise ManagementError("V9 diagnostic result exceeds reviewed bounds")
            v7.integer(frame.get("observed_irq"), "observed_irq")
            v7.integer(frame.get("observed_status"), "observed_status")
        return frame, phases

    def adopt(
        self,
        before: dict,
        proof: AdoptionProof | None,
        generation: int,
        ack_unknown: bool,
        diagnostic: DiagnosticProof | None,
    ) -> dict:
        passive(before)
        if (
            proof is None
            or diagnostic is None
            or ack_unknown is not True
            or self.image_pin != ADOPTION_SEED
            or type(generation) is not int
            or generation != 1
            or digest(proof.manifest) != ADOPTION_MANIFEST
        ):
            raise ManagementError(
                "ADOPT requires the exact event and explicit unknown-RAM acknowledgment"
            )
        AdoptionProof.qualify(proof.files)
        diagnostic.qualify(before, self.elf_pin)
        if (
            not before["loaded"]
            or before["dirty"]
            or not before["unclean_boot"]
            or not before["session_active_marker"]
            or before["last_fault_code"] != 0
            or before["last_fault"] != "none"
            or before["image_sha256"] != ADOPTION_SEED
            or before["seed_sha256"] != ADOPTION_SEED
            or before["live_generation"] != generation
            or before["durable_generation"] != generation
        ):
            raise ManagementError(
                "ADOPT current state is not the reviewed marked checkpoint"
            )
        proof.save(self.output)
        diagnostic.save(self.output)
        durable_json(
            self.output / "adoption-plan-before-command.json",
            {
                "policy": "V7_START_RESET",
                "source_manifest_sha256": ADOPTION_MANIFEST,
                "image_sha256_before_and_after": ADOPTION_SEED,
                "generation_before_and_after": generation,
                "successful_v9_diagnostic_receipt_sha256": digest(diagnostic.data),
                "acknowledge_pre_reset_volatile_history_unknown": True,
                "no_claim_that_pre_reset_ram_was_unchanged": True,
                "counter_or_image_reset": False,
                "before": before,
            },
        )
        self.send_once(
            "EMU_ADOPT_DURABLE",
            f"EMU_ADOPT_DURABLE V7_START_RESET {self.image_pin} {generation} ACK_VOLATILE_UNKNOWN\n".encode(
                "ascii"
            ),
        )
        result = self.result("adopt_durable")
        self.result_frame = result
        if result.get("ok") is True:
            passive(result)
            if (
                result.get("message")
                != "durable_checkpoint_adopted_volatile_history_unknown"
                or result.get("adoption_policy") != "V7_START_RESET"
                or result.get("adopted_durable") is not True
                or result.get("pre_reset_volatile_history_unknown") is not True
                or result.get("prior_marker") is not True
                or result.get("prior_unclean") is not True
                or result.get("prior_reset_reason") != before["reset_reason"]
                or result.get("prior_boot_breadcrumb") != before["boot_breadcrumb"]
                or result["unclean_boot"]
                or result["session_active_marker"]
                or result["last_fault_code"] != 0
                or result["last_fault"] != "none"
            ):
                raise ManagementError(
                    "ADOPT did not confirm the exact metadata-only policy"
                )
            for name in evidence.STABLE_FIELDS:
                if (
                    name not in {"unclean_boot", "session_active_marker"}
                    and result[name] != before[name]
                ):
                    raise ManagementError(
                        "ADOPT unexpectedly changed checkpoint metadata"
                    )
        return result

    def start(self, before: dict, generation: int, sole: bool) -> dict:
        passive(before)
        if (
            sole is not True
            or type(generation) is not int
            or not 0 <= generation < 0xFFFFFFFF
            or not before["loaded"]
            or before["dirty"]
            or before["unclean_boot"]
            or before["session_active_marker"]
            or before["last_fault_code"] != 0
            or before["last_fault"] != "none"
            or before["image_sha256"] != self.image_pin
            or before["live_generation"] != generation
            or before["durable_generation"] != generation
        ):
            raise ManagementError(
                "START requires exact clean checkpoint and explicit sole-address ownership"
            )
        durable_json(
            self.output / "sole-address-before-start.json",
            {
                "sole_0x50": True,
                "physical_eeprom_signals_disconnected_asserted": True,
                "image_sha256": self.image_pin,
                "durable_generation": generation,
                "no_hardware_ownership_proof_in_software": True,
            },
        )
        self.send_once(
            "EMU_START",
            f"EMU_START SOLE_0X50 {self.image_pin} {generation}\n".encode("ascii"),
        )
        phases = []
        self.phases = phases
        deadline = self.session.clock() + 10
        while True:
            frame = self.reader.receive(deadline)
            if frame.get("type") == "emu_result":
                break
            if (
                frame.get("type") != "emu_start_phase"
                or frame.get("op") != "start"
                or len(phases) >= 2
                or frame.get("phase") != ("marker_saved", "fifo_loaded")[len(phases)]
            ):
                raise ManagementError("Unexpected bounded START diagnostic phase")
            state(frame)
            if (
                frame["emulation_active"]
                or not frame["pins_input_only"]
                or not frame["session_active_marker"]
                or frame["unclean_boot"]
                or frame["dirty"]
                or frame["durable_generation"] != generation
                or frame["live_generation"] != generation
                or frame["seed_sha256"] != before["seed_sha256"]
            ):
                raise ManagementError(
                    "START pre-output phase has an unexpected session state"
                )
            if frame["phase"] == "fifo_loaded":
                registers(frame)
                if frame["enabled_interrupts"] != 0:
                    raise ManagementError("START FIFO phase already enables IRQs")
            phases.append(frame)
        state(frame)
        self.result_frame = frame
        if frame.get("op") != "start":
            raise ManagementError("Unexpected START operation result")
        if frame.get("ok") is True and (
            len(phases) != 2
            or not frame["emulation_active"]
            or not frame["session_open"]
            or frame["unclean_boot"]
            or not frame["session_active_marker"]
            or frame["durable_generation"] != generation
            or frame["last_fault_code"] != 0
            or frame["last_fault"] != "none"
            or frame["seed_sha256"] != before["seed_sha256"]
        ):
            raise ManagementError("START active state was not confirmed")
        return frame

    def run_action(
        self,
        action: str,
        *,
        generation: int = 1,
        sole: bool = False,
        proof: AdoptionProof | None = None,
        ack_unknown: bool = False,
        diagnostic: DiagnosticProof | None = None,
    ) -> dict:
        self.phases = []
        self.result_frame = None
        before = after = None
        optional_snapshot_error = None
        try:
            startup = self.reader.drain_startup()
            current = self.probe()
            durable_json(
                self.output / "intent-before-command.json",
                {
                    "schema": SCHEMA,
                    "action": action,
                    "firmware": FIRMWARE,
                    "app_elf_sha256": self.elf_pin,
                    "image_sha256": self.image_pin,
                    "before": current,
                    "automatic_retry": False,
                },
            )
            chatter = self.reader.chatter
            if action == "stop":
                if current["loaded"]:
                    try:
                        before, _ = self.save_snapshot("before-stop-volatile")
                    except Exception as error:  # noqa: BLE001 - Optional export must not block detach.
                        optional_snapshot_error = {
                            "type": type(error).__name__,
                            "message": str(error),
                        }
                self.send_once("EMU_STOP")
                result = self.result("stop")
                self.result_frame = result
                passive(result)
                if result["dirty"]:
                    raise ManagementError("STOP did not reach a checkpoint")
                if current["unclean_boot"] and (
                    not result["unclean_boot"]
                    or result["session_active_marker"]
                    != current["session_active_marker"]
                ):
                    raise ManagementError(
                        "STOP improperly cleared prior unclean evidence"
                    )
                after, _ = self.save_snapshot("after-stop-checkpoint")
                if (
                    after["image_sha256"] != result["image_sha256"]
                    or after["image_generation"] != result["durable_generation"]
                    or after["dirty"]
                ):
                    raise ManagementError("STOP snapshot did not match the checkpoint")
                status = (
                    "stopped_unclean_preserved"
                    if result["unclean_boot"]
                    else "stopped_checkpointed"
                )
            elif action == "status":
                result = current
                if current["loaded"]:
                    after, _ = self.save_snapshot("status-snapshot")
                status = "status_and_snapshot_saved"
            elif action in {"diag", "adopt", "start"}:
                before, preimage = self.save_snapshot(f"before-{action}", pinned=True)
                passive(before)
                self.unchanged(current, before)
                if action == "diag":
                    result, _ = self.diag(before)
                    after, postimage = self.save_snapshot("after-diag", pinned=True)
                    self.unchanged(before, after)
                    if preimage != postimage:
                        raise ManagementError("Detached diagnostic changed image bytes")
                    self.unchanged(after, self.probe())
                    status = "detached_diagnostic_verified"
                elif action == "adopt":
                    result = self.adopt(
                        before, proof, generation, ack_unknown, diagnostic
                    )
                    after, postimage = self.save_snapshot("after-adopt", pinned=True)
                    if preimage != postimage:
                        raise ManagementError("ADOPT changed a complete image byte")
                    self.unchanged(result, after)
                    self.unchanged(after, self.probe())
                    status = "last_durable_adopted_volatile_history_unknown"
                else:
                    result = self.start(before, generation, sole)
                    status = "started_experimental"
            else:
                raise ManagementError("Unknown explicit V9 operation")
            if result.get("ok", True) is not True or self.reader.chatter != chatter:
                raise ManagementError(
                    "V9 operation failed or new raw boot/chatter was retained"
                )
            receipt = {
                "schema": SCHEMA,
                "action": action,
                "status": status,
                "firmware": FIRMWARE,
                "app_elf_sha256": self.elf_pin,
                "before": before,
                "result": result,
                "after": after,
                "phases": self.phases,
                "startup_capture": startup,
                "pre_stop_snapshot_error": optional_snapshot_error,
                "command_may_have_reached_device": self.command_may_have_reached,
                "raw_lines_saved": self.reader.lines,
                "raw_chatter_lines_saved": self.reader.chatter,
                "automatic_retry": False,
                "transport_qualified": False,
                "counter_reset_or_crc_repair": False,
                "pre_reset_volatile_history_unknown": True,
                "volatile_history_scope": "The earlier V7 reset window remains unknown after adoption; current clean metadata does not reconstruct that RAM.",
                "reset_reason_scope": "current V9 boot only; not the prior V7 reset cause",
            }
            durable_json(self.output / "receipt.json", receipt)
            return receipt
        except Exception as error:
            # Do not issue another command, reset the UART or retry. Retain any
            # queued panic/reboot tail, including fragments, within a short bound.
            tail_error = None
            if self.command_may_have_reached:
                deadline = self.session.clock() + 1.5
                try:
                    while self.session.clock() < deadline:
                        self.reader.receive(deadline)
                except Exception as tail:  # noqa: BLE001 - Retain the original operation failure.
                    tail_error = {"type": type(tail).__name__, "message": str(tail)}
            durable_json(
                self.output / "failure.json",
                {
                    "schema": SCHEMA,
                    "action": action,
                    "status": "unverified",
                    "firmware": FIRMWARE,
                    "app_elf_sha256": self.elf_pin,
                    "before": before,
                    "after": after,
                    "result": self.result_frame,
                    "phases": self.phases,
                    "last_device_frame": self.reader.last_frame,
                    "command_may_have_reached_device": self.command_may_have_reached,
                    "raw_lines_saved": self.reader.lines,
                    "raw_chatter_lines_saved": self.reader.chatter,
                    "error_type": type(error).__name__,
                    "error": str(error),
                    "failure_tail_observation": tail_error,
                    "automatic_retry": False,
                    "pre_stop_snapshot_error": optional_snapshot_error,
                    "unknown_pre_reset_volatile_writes_not_excluded": True,
                },
            )
            raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "diag", "adopt", "start", "stop"))
    parser.add_argument("--port", required=True)
    parser.add_argument("--serial-number", required=True)
    parser.add_argument("--expect-elf-sha256", required=True)
    parser.add_argument("--image-sha256", required=True)
    parser.add_argument("--durable-generation", type=int, default=1)
    parser.add_argument("--sole-0x50", action="store_true")
    parser.add_argument("--ack-volatile-history-unknown", action="store_true")
    parser.add_argument("--adoption-evidence", type=Path)
    parser.add_argument("--adoption-manifest-sha256")
    parser.add_argument("--v9-diagnostic-receipt", type=Path)
    parser.add_argument("--v9-diagnostic-receipt-sha256")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    require_hash(args.expect_elf_sha256)
    require_hash(args.image_sha256)
    proof = None
    diagnostic = None
    if args.action == "adopt":
        if (
            not args.ack_volatile_history_unknown
            or not args.adoption_evidence
            or not args.adoption_manifest_sha256
            or not args.v9_diagnostic_receipt
            or not args.v9_diagnostic_receipt_sha256
        ):
            parser.error(
                "ADOPT requires exact event/V9 diagnostic evidence and --ack-volatile-history-unknown"
            )
        proof = AdoptionProof.load(
            args.adoption_evidence, args.adoption_manifest_sha256
        )
        diagnostic = DiagnosticProof.load(
            args.v9_diagnostic_receipt, args.v9_diagnostic_receipt_sha256
        )
    if args.action == "start" and not args.sole_0x50:
        parser.error("START requires explicit --sole-0x50")
    args.output.mkdir(parents=True, exist_ok=False)
    import serial
    from serial.tools import list_ports

    device = validate_device(args.port, list(list_ports.comports()), args.serial_number)
    with (args.output / "audit.jsonl").open("x", encoding="utf-8") as stream:
        audit = DurableAudit(stream)
        audit.record(
            "device_verified",
            port=device.device,
            vid=device.vid,
            pid=device.pid,
            serial_number=args.serial_number,
        )
        uart = serial.Serial(port=None, baudrate=921600, timeout=0.2, write_timeout=5)
        uart.dtr = uart.rts = False
        uart.port = args.port
        try:
            uart.open()
            time.sleep(1.5)
            receipt = Control(
                Session(uart, audit, expected_firmware=FIRMWARE),
                args.output,
                args.expect_elf_sha256,
                args.image_sha256,
            ).run_action(
                args.action,
                generation=args.durable_generation,
                sole=args.sole_0x50,
                proof=proof,
                ack_unknown=args.ack_volatile_history_unknown,
                diagnostic=diagnostic,
            )
        finally:
            uart.close()
    print(json.dumps({"status": receipt["status"], "result": receipt["result"]}))


if __name__ == "__main__":
    try:
        main()
    except (ManagementError, OSError, ValueError, ImportError):
        print(
            "V9 operation unverified; inspect raw evidence before another command.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
