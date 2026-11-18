"""Explicit V14 initialization-budget policy and guarded emulator controls.

Template: frozen V12 guards and V8 raw UART reader. Policy changes only the
separate advertised view; the latest backing image is never rolled back.
No automatic printer rescan, print submission, retry or power detection.
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import diagnose_emulator_v8 as evidence
import manage_emulator_v7 as v7
from bench_eeprom import DurableAudit, digest, durable_bytes, durable_json, require_hash
from inspect_eeprom import crc16_x25, inspect_eeprom
from manage_esp32 import ManagementError, Session, validate_device

FIRMWARE = "i2c-eeprom-emulator-v14"
CAPABILITY = evidence.CAPABILITY
SCHEMA = "sunburn.brother.emulator-v14.v1"
MAX_CAPACITY = 11232
STOP_PROOF_MANIFEST = "adf1047183705a56307cc920c4adb5ee4374e469c0a732b2dff7f7178cabc57c"
STOP_PROOF_IMAGE = "50ce837a5bd0d10e347cc665af5c6df5340fbafbe7579e70a0dff437bcde48ed"
STOP_PROOF_GENERATION = 63


def policy(frame: dict[str, object]) -> dict:
    value = frame.get("policy")
    if not isinstance(value, dict) or value.get("mode") != "initialization_budget_v1":
        raise ManagementError("Missing exact initialization-budget policy")
    for name in (
        "enabled",
        "persisted",
        "write_verification_pending",
        "cache_refresh_required",
        "suspended",
    ):
        if type(value.get(name)) is not bool:
            raise ManagementError("Policy contains an unknown boolean")
    if value["persisted"]:
        raise ManagementError("V14 unexpectedly claims persisted policy")
    for name in ("capacity_counts", "view_generation", "refresh_events"):
        v7.integer(value.get(name), "policy." + name)
    capacity = value["capacity_counts"]
    if capacity > MAX_CAPACITY or (value["enabled"] and capacity == 0):
        raise ManagementError("Policy capacity exceeds its exact valid bounds")
    for name in (
        "backing_initial_raw",
        "backing_remaining_raw",
        "advertised_initial_raw",
        "advertised_remaining_raw",
    ):
        if v7.integer(value.get(name), "policy." + name) > 65535:
            raise ManagementError("Policy counter exceeds its uint16 range")
    return value


def advertised_budget(backing: bytes, capacity: int) -> bytes:
    if type(capacity) is not int or not 1 <= capacity <= MAX_CAPACITY:
        raise ManagementError("Capacity must be an integer in1..11232")
    inspection = inspect_eeprom(backing)
    if not inspection["all_three_crc_regions_match"] or backing[1] == 3:
        raise ManagementError("Policy requires a complete valid non-wristband image")
    view = bytearray(backing)
    struct.pack_into("<H", view, 0x60, capacity)
    struct.pack_into("<H", view, 0xE8, capacity)
    struct.pack_into("<H", view, 0xAE, crc16_x25(view[:0xAE]))
    struct.pack_into("<H", view, 0xF6, crc16_x25(view[0xE8:0xF6]))
    return bytes(view)


@dataclass(frozen=True)
class StopProof:
    manifest: bytes
    files: dict[str, bytes]

    @classmethod
    def load(cls, directory: Path, pin: str) -> StopProof:
        manifest = (directory / "manifest.json").read_bytes()
        if require_hash(pin) != STOP_PROOF_MANIFEST or digest(manifest) != pin:
            raise ManagementError(
                "Stop recovery requires the exact actual-event manifest"
            )
        mapping = json.loads(manifest).get("files")
        expected = {
            "stop-failure.json",
            "before.bin",
            "after.bin",
            "intent.json",
            "audit.jsonl",
        }
        # Counts come from the reviewed V13 STOP and detached-DIAG captures.
        expected.update(f"raw-{i:03}.bin" for i in range(6))
        expected.update(
            {
                "diag-receipt.json",
                "diag-before.bin",
                "diag-after.bin",
                "diag-intent.json",
                "diag-audit.jsonl",
            }
        )
        expected.update(f"diag-raw-{i:03}.bin" for i in range(12))
        if not isinstance(mapping, dict) or set(mapping) != expected:
            raise ManagementError("Stop proof has an unexpected file set")
        files = {name: (directory / name).read_bytes() for name in expected}
        if any(digest(data) != mapping[name] for name, data in files.items()):
            raise ManagementError("Stop proof differs from exact saved source bytes")
        cls.qualify(files)
        return cls(manifest, files)

    @staticmethod
    def qualify(files: dict[str, bytes]) -> None:
        import manage_emulator_v13 as old

        failure = json.loads(files["stop-failure.json"])
        frames = [json.loads(files[f"raw-{i:03}.bin"]) for i in range(6)]
        if (
            failure.get("action") != "stop"
            or failure.get("status") != "unverified"
            or failure.get("raw_lines_saved") != 6
            or failure.get("raw_chatter_lines_saved") != 0
            or frames[3] != failure.get("before")
            or frames[4] != failure.get("result")
            or frames[5] != failure.get("after")
            or frames[5] != failure.get("last_device_frame")
            or frames[4].get("ok") is not False
            or frames[4].get("message") != "detached_saved_with_fault"
            or frames[4].get("op") != "stop"
        ):
            raise ManagementError("Actual failed-STOP provenance is inconsistent")
        for frame in frames[2:]:
            old.state(frame)
        before, _ = v7.snapshot({**frames[3], "firmware": v7.FIRMWARE})
        after, inspection = v7.snapshot({**frames[5], "firmware": v7.FIRMWARE})
        if (
            before != files["before.bin"]
            or after != files["after.bin"]
            or before != after
            or digest(after) != STOP_PROOF_IMAGE
            or not inspection["all_three_crc_regions_match"]
            or frames[3]["live_generation"] != STOP_PROOF_GENERATION
        ):
            raise ManagementError("Stop proof full images or current generation differ")
        for frame in frames[4:]:
            old.passive(frame)
            old.require_off(frame)
            if (
                frame["last_fault_code"] != 9
                or frame["last_fault"] != "stop_timeout"
                or frame["dirty"]
                or frame["session_active_marker"]
                or frame["live_generation"] != STOP_PROOF_GENERATION
                or frame["durable_generation"] != STOP_PROOF_GENERATION
                or frame["image_sha256"] != STOP_PROOF_IMAGE
            ):
                raise ManagementError(
                    "Stop proof is not the exact closed fault9 checkpoint"
                )
        commands = [
            json.loads(line).get("command")
            for line in files["audit.jsonl"].splitlines()
        ]
        if commands.count("EMU_STOP") != 1:
            raise ManagementError("Stop proof does not record exactly one STOP")

        diagnostic = json.loads(files["diag-receipt.json"])
        diag_frames = [json.loads(files[f"diag-raw-{i:03}.bin"]) for i in range(12)]
        if (
            diagnostic.get("action") != "diag"
            or diagnostic.get("status") != "detached_diagnostic_verified"
            or diagnostic.get("raw_lines_saved") != 12
            or diagnostic.get("raw_chatter_lines_saved") != 0
            or diagnostic.get("before") != diag_frames[3]
            or diagnostic.get("phases") != diag_frames[4:7]
            or diagnostic.get("result") != diag_frames[7]
            or diagnostic.get("after") != diag_frames[8]
        ):
            raise ManagementError("Detached-DIAG provenance is inconsistent")
        for index in (2, 3, 7, 8, 11):
            frame = diag_frames[index]
            old.state(frame)
            old.passive(frame)
            old.require_off(frame)
            if any(frame[name] != frames[5][name] for name in evidence.STABLE_FIELDS):
                raise ManagementError("Detached-DIAG changed the saved checkpoint")
            if old.policy(frame) != old.policy(frames[5]):
                raise ManagementError("Detached-DIAG changed historical policy")
        for index, phase in enumerate(diag_frames[4:7]):
            old.registers(phase)
            if (
                phase.get("type") != "emu_diag_phase"
                or phase.get("op") != "diag"
                or phase.get("phase") != evidence.PHASES[index]
                or phase["enabled_interrupts"] != 0
            ):
                raise ManagementError("Detached-DIAG phases are not safely ordered")
        result = diag_frames[7]
        old.registers(result)
        if (
            result.get("type") != "emu_diag"
            or result.get("ok") is not True
            or result.get("message") != "detached_diagnostic_complete"
            or result.get("outputs_enabled") is not False
            or result.get("observation_ms") != 5
            or result.get("one_shot_irq_count") != 0
            or result.get("observed_irq") != 0
            or result["enabled_interrupts"] != 0
            or result["hw_status"] & (1 << 4)
        ):
            raise ManagementError(
                "Detached-DIAG did not retain the idle/released result"
            )
        for index, name in ((3, "diag-before.bin"), (8, "diag-after.bin")):
            data, inspected = v7.snapshot(
                {**diag_frames[index], "firmware": v7.FIRMWARE}
            )
            if (
                data != files[name]
                or data != after
                or not inspected["all_three_crc_regions_match"]
            ):
                raise ManagementError("Detached-DIAG complete image differs")
        diag_commands = [
            json.loads(line).get("command")
            for line in files["diag-audit.jsonl"].splitlines()
        ]
        if diag_commands.count("EMU_DIAG") != 1:
            raise ManagementError("Detached-DIAG proof does not record one diagnostic")

    def save(self, output: Path) -> None:
        directory = output / "reviewed-stop-checkpoint-evidence"
        directory.mkdir()
        durable_bytes(directory / "manifest.json", self.manifest)
        for name, data in self.files.items():
            durable_bytes(directory / name, data)


def state(frame: dict[str, object]) -> dict[str, object]:
    if frame.get("firmware") != FIRMWARE:
        raise ManagementError("Unexpected V14 firmware identity")
    v7.state({**frame, "firmware": v7.FIRMWARE})
    v7.integer(frame.get("reset_reason"), "reset_reason")
    crumb = frame.get("boot_breadcrumb")
    if not isinstance(crumb, dict) or type(crumb.get("valid")) is not bool:
        raise ManagementError("Missing typed V14 reset breadcrumb")
    stage = v7.integer(crumb.get("stage"), "breadcrumb.stage")
    inverse = v7.integer(crumb.get("stage_inverse"), "breadcrumb.stage_inverse")
    if crumb["valid"] and stage ^ inverse != 0xFFFFFFFF:
        raise ManagementError("Invalid retained breadcrumb inverse")
    diagnostics = frame.get("isr_diagnostics")
    if not isinstance(diagnostics, dict):
        raise ManagementError("Missing V14 ISR diagnostics")
    for name in (
        "isr_calls",
        "last_irq_flags",
        "last_rx_count",
        "last_tx_count",
        "irq_storms",
        "empty_rx_irqs",
    ):
        v7.integer(diagnostics.get(name), name)
    pullup(frame)
    accounting = frame.get("read_accounting")
    if (
        not isinstance(accounting, dict)
        or accounting.get("method") != "fifo_dequeued_at_terminal_nack"
    ):
        raise ManagementError("Missing exact V14 read-accounting diagnostic method")
    for name in (
        "loaded",
        "fifo_remaining",
        "consumed",
        "rx_irq_diagnostic",
        "tx_irq_diagnostic",
    ):
        v7.integer(accounting.get(name), "read_accounting." + name)
    if type(accounting.get("terminal_nack")) is not bool:
        raise ManagementError("Unknown V14 terminal-NACK diagnostic")
    write_evidence(frame)
    policy(frame)
    return frame


def write_evidence(frame: dict) -> dict:
    value = frame.get("write_evidence")
    if not isinstance(value, dict) or type(value.get("available")) is not bool:
        raise ManagementError("Missing typed V14 write evidence")
    pending, committed = value.get("pending_rejected"), value.get("committed")
    if not isinstance(committed, list) or len(committed) > 4:
        raise ManagementError("Write evidence exceeds the four-record bound")
    if value["available"] != (not frame["emulation_active"]):
        raise ManagementError("Write evidence availability contradicts active state")
    if not value["available"] and (pending is not None or committed):
        raise ManagementError("Active response exported unstable write records")
    for record, is_commit in [(item, True) for item in committed] + (
        [(pending, False)] if pending is not None else []
    ):
        if (
            not isinstance(record, dict)
            or record.get("valid") is not True
            or type(record.get("committed")) is not bool
            or record["committed"] != is_commit
            or type(record.get("payload_truncated")) is not bool
        ):
            raise ManagementError("Malformed committed/rejected write record")
        for name in (
            "fault_code",
            "generation_before",
            "generation_after",
            "pointer",
            "page_base",
            "page_mask",
            "data_bytes",
            "rx_bytes",
            "rx_ack_bytes",
            "rx_bad_ack_samples",
            "irq_flags",
            "hw_status",
            "last_rx_hw_status",
        ):
            v7.integer(record.get(name), "write_evidence." + name)
        if (
            record["pointer"] > 255
            or record["page_base"] > 240
            or record["page_base"] & 15
            or record["page_mask"] > 65535
        ):
            raise ManagementError("Write evidence exceeds 256-byte/16-byte-page bounds")
        for name, maximum in (("page_hex", 32), ("payload_hex", 32)):
            text = record.get(name)
            if (
                not isinstance(text, str)
                or len(text) > maximum
                or len(text) % 2
                or any(c not in "0123456789abcdef" for c in text)
            ):
                raise ManagementError("Write evidence contains malformed lowercase hex")
        if len(record["page_hex"]) != 32:
            raise ManagementError("Write page evidence is not exactly sixteen bytes")
        page, payload = (
            bytes.fromhex(record["page_hex"]),
            bytes.fromhex(record["payload_hex"]),
        )
        if any(
            byte and not (record["page_mask"] & (1 << index))
            for index, byte in enumerate(page)
        ):
            raise ManagementError("Unobserved page slots are not zeroed")
        if record["payload_truncated"]:
            if payload or record["data_bytes"] <= 16:
                raise ManagementError("Truncated payload must be explicitly unknown")
        elif len(payload) != record["data_bytes"]:
            raise ManagementError(
                "Arrival-order payload length differs from observed data"
            )
        else:
            for index, byte in enumerate(payload):
                slot = (record["pointer"] + index) & 15
                if not record["page_mask"] & (1 << slot) or page[slot] != byte:
                    raise ManagementError(
                        "Arrival-order payload contradicts observed page slots"
                    )
    return value


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
        raise ManagementError("V14 RX FIFO threshold is not the exact reviewed value1")


def pullup(frame: dict) -> dict:
    value = frame.get("pullup_control")
    if (
        not isinstance(value, dict)
        or type(value.get("gpio")) is not int
        or value["gpio"] != 19
    ):
        raise ManagementError("Missing exact D19 pullup state")
    for name in (
        "configured_on",
        "output_enabled",
        "output_high",
        "input_high",
        "internal_pulls",
        "off_verified",
    ):
        if type(value.get(name)) is not bool:
            raise ManagementError("Pullup control contains an unknown boolean")
    v7.integer(value.get("output_select"), "D19 output selector")
    off = (
        not value["configured_on"]
        and not value["output_enabled"]
        and not value["internal_pulls"]
        and value["output_select"] == 1280
    )
    if value["off_verified"] != off:
        raise ManagementError("D19 OFF verification contradicts register state")
    return value


def require_off(frame: dict) -> None:
    if not pullup(frame)["off_verified"]:
        raise ManagementError("D19 is not verified input/highZ OFF")


def require_on(frame: dict) -> None:
    value = pullup(frame)
    if (
        not value["configured_on"]
        or not value["output_enabled"]
        or not value["output_high"]
        or not value["input_high"]
        or value["internal_pulls"]
        or value["output_select"] != 1280
        or value["off_verified"]
    ):
        raise ManagementError("D19 powered HIGH state is unverified")


class V14Reader(evidence.EvidenceReader):
    """Retain the frozen framing with a larger bounded diagnostic record line."""

    def receive(self, deadline: float) -> dict[str, object]:
        line = bytearray()
        while self.session.clock() < deadline:
            value = self.session.uart.read(1)
            if not value:
                continue
            line.extend(value)
            self.total_bytes += len(value)
            if len(line) > 16384 or self.total_bytes > evidence.MAX_TOTAL_BYTES:
                self.retain(bytes(line), partial=True)
                raise ManagementError("V14 UART exceeded its finite evidence bound")
            if value != b"\n":
                continue
            raw = bytes(line)
            line.clear()
            frame = self.parse_line(raw)
            if frame is not None:
                return frame
        if line:
            self.retain(bytes(line), partial=True)
        raise ManagementError("V14 UART timed out; no automatic retry")


class Control(evidence.DiagnosticControl):
    def __init__(
        self, session: Session, output: Path, elf_pin: str, image_pin: str
    ) -> None:
        super().__init__(session, output, elf_pin, image_pin)
        self.reader = V14Reader(session, output)

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
                    "Unexpected V14 identity, CPU or GPIO pin configuration"
                )
        net = self.exchange("NETINFO", "netinfo")
        if (
            net.get("firmware") != FIRMWARE
            or net.get("app_elf_sha256") != self.elf_pin
            or net.get("armed") is not False
            or net.get("update_busy") is not False
        ):
            raise ManagementError(
                "V14 firmware ELF or idle control state differs from pin"
            )
        current = state(self.exchange("EMU_STATUS", "emu_status"))
        if (
            info.get("pins_input_only") != current["pins_input_only"]
            or info.get("internal_pulls") != current["internal_pulls"]
        ):
            raise ManagementError("V14 INFO and emulator pin state disagree")
        return current

    def save_snapshot(self, label: str, *, pinned: bool = False) -> tuple[dict, bytes]:
        frame = state(self.exchange("EMU_SNAPSHOT", "emu_snapshot"))
        data, inspection = v7.snapshot({**frame, "firmware": v7.FIRMWARE})
        durable_bytes(self.output / f"{label}.bin", data)
        durable_json(self.output / f"{label}-inspection.json", inspection)
        durable_json(
            self.output / f"{label}-write-evidence.json", frame["write_evidence"]
        )
        counters = policy(frame)
        if counters["backing_initial_raw"] != int.from_bytes(
            data[0x60:0x62], "little"
        ) or counters["backing_remaining_raw"] != int.from_bytes(
            data[0xE8:0xEA], "little"
        ):
            raise ManagementError(
                "Backing snapshot differs from its policy counter diagnostics"
            )
        if pinned and (
            digest(data) != self.image_pin
            or frame["dirty"]
            or not frame["snapshot_is_current"]
            or frame["image_generation"] != frame["durable_generation"]
            or not inspection["all_three_crc_regions_match"]
        ):
            raise ManagementError(
                "V14 full preimage differs from the exact valid durable pin"
            )
        return frame, data

    def save_view(self, label: str) -> tuple[dict, bytes]:
        frame = state(self.exchange("EMU_VIEW_SNAPSHOT", "emu_view_snapshot"))
        value = policy(frame)
        text = frame.get("hex")
        if (
            frame.get("image_kind") != "advertised"
            or frame.get("bytes") != 256
            or not isinstance(text, str)
            or len(text) != 512
            or any(c not in "0123456789abcdef" for c in text)
            or type(frame.get("snapshot_is_current")) is not bool
            or v7.integer(frame.get("image_generation"), "view.image_generation")
            != value["view_generation"]
        ):
            raise ManagementError(
                "Advertised snapshot framing or separate generation is invalid"
            )
        data = bytes.fromhex(text)
        inspection = inspect_eeprom(data)
        durable_bytes(self.output / f"{label}.bin", data)
        durable_json(self.output / f"{label}-inspection.json", inspection)
        if digest(data) != frame.get("sha256"):
            raise ManagementError("Advertised full snapshot hash mismatch")
        checks = [item["matches"] for item in inspection["crc_checks"]]
        for name, actual in zip(("crc_r0", "crc_rw1", "crc_rw2"), checks, strict=True):
            if type(frame.get(name)) is not bool or frame[name] != actual:
                raise ManagementError(
                    "Advertised snapshot CRC diagnostic contradicts bytes"
                )
        if value["advertised_initial_raw"] != int.from_bytes(
            data[0x60:0x62], "little"
        ) or value["advertised_remaining_raw"] != int.from_bytes(
            data[0xE8:0xEA], "little"
        ):
            raise ManagementError(
                "Advertised snapshot differs from its reported counters"
            )
        return frame, data

    def send_once(self, command: str, payload: bytes | None = None) -> None:
        self.command_may_have_reached = True
        self.session.command(command, payload)

    def result(self, op: str) -> dict:
        frame = state(self.reader.receive(self.session.clock() + 10))
        if frame.get("type") != "emu_result" or frame.get("op") != op:
            raise ManagementError("Unexpected V14 operation result")
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
                raise ManagementError("Unexpected V14 detached diagnostic phase")
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
                raise ManagementError("V14 diagnostic result exceeds reviewed bounds")
            v7.integer(frame.get("observed_irq"), "observed_irq")
            v7.integer(frame.get("observed_status"), "observed_status")
        return frame, phases

    def start(
        self,
        before: dict,
        generation: int,
        sole: bool,
        software: bool = False,
        powered: bool = False,
    ) -> dict:
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
        if software and (
            powered is not True or not before["pullup_control"]["off_verified"]
        ):
            raise ManagementError(
                "Combined D19 START requires explicit printer power and verifiedOFF"
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
            "EMU_START_PULLUP" if software else "EMU_START",
            (
                f"EMU_START_PULLUP SOLE_0X50 {self.image_pin} {generation} PRINTER_POWERED\n"
                if software
                else f"EMU_START SOLE_0X50 {self.image_pin} {generation}\n"
            ).encode("ascii"),
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
            if software:
                (require_off if frame["phase"] == "marker_saved" else require_on)(frame)
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
        if frame.get("ok") is True and software:
            require_on(frame)
        return frame

    def set_pullup(self, before: dict, on: bool, powered: bool) -> dict:
        passive(before)
        if on and (
            powered is not True
            or not before["loaded"]
            or before["dirty"]
            or before["unclean_boot"]
            or before["session_active_marker"]
            or before["last_fault_code"] != 0
        ):
            raise ManagementError(
                "D19 ON requires clean passive state and explicit printer-power assertion"
            )
        durable_json(
            self.output / "pullup-plan-before-command.json",
            {
                "gpio": 19,
                "action": "HIGH_through_4k7" if on else "INPUT_highZ",
                "printer_power_asserted": powered if on else None,
                "power_not_measured_by_helper": True,
                "disable_before_printer_power_off": True,
                "physical_wiring_not_verified_by_helper": True,
            },
        )
        self.send_once(
            "EMU_PULLUP",
            b"EMU_PULLUP ON PRINTER_POWERED\n" if on else b"EMU_PULLUP OFF\n",
        )
        result = self.result("pullup_on" if on else "pullup_off")
        self.result_frame = result
        if result.get("ok") is True:
            passive(result)
            (require_on if on else require_off)(result)
            self.unchanged(before, result)
        return result

    def set_policy(
        self, before: dict, generation: int, enabled: bool, capacity: int
    ) -> dict:
        passive(before)
        require_off(before)
        if (
            type(generation) is not int
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
            or policy(before)["enabled"] == enabled
        ):
            raise ManagementError(
                "Policy change requires an exact clean passive checkpoint and state transition"
            )
        if enabled and (type(capacity) is not int or not 1 <= capacity <= MAX_CAPACITY):
            raise ManagementError("Policy capacity must be an integer in1..11232")
        op = "policy_enable" if enabled else "policy_disable"
        durable_json(
            self.output / "policy-plan-before-command.json",
            {
                "operation": op,
                "image_sha256_before_and_after": self.image_pin,
                "generation_before_and_after": generation,
                "capacity_counts": capacity
                if enabled
                else policy(before)["capacity_counts"],
                "capacity_is_virtual_not_measured_paper": True,
                "backing_image_mutation": False,
                "stale_seed_rollback": False,
                "refresh_trigger": "full_initialization_pointer_zero_read_only",
                "automatic_D19_rescan": False,
                "automatic_post_verify_reset": False,
                "policy_persisted": False,
            },
        )
        command = (
            f"EMU_POLICY ENABLE {self.image_pin} {generation} {capacity}\n"
            if enabled
            else f"EMU_POLICY DISABLE {self.image_pin} {generation}\n"
        )
        self.send_once("EMU_POLICY", command.encode("ascii"))
        result = self.result(op)
        self.result_frame = result
        if result.get("ok") is True:
            passive(result)
            require_off(result)
            self.unchanged(before, result)
            value = policy(result)
            if value["enabled"] != enabled or (
                enabled and value["capacity_counts"] != capacity
            ):
                raise ManagementError("Policy state transition was not verified")
            if enabled and (
                value["suspended"]
                or value["write_verification_pending"]
                or not value["cache_refresh_required"]
            ):
                raise ManagementError(
                    "Fresh policy enable has pending/suspended or unreported cache state"
                )
        return result

    def recover_stop(
        self, before: dict, generation: int, proof: StopProof | None
    ) -> dict:
        passive(before)
        require_off(before)
        if (
            proof is None
            or digest(proof.manifest) != STOP_PROOF_MANIFEST
            or self.image_pin != STOP_PROOF_IMAGE
            or generation != STOP_PROOF_GENERATION
            or not before["loaded"]
            or before["dirty"]
            or before["session_active_marker"]
            or before["last_fault_code"] != 9
            or before["last_fault"] != "stop_timeout"
            or before["image_sha256"] != STOP_PROOF_IMAGE
            or before["live_generation"] != generation
            or before["durable_generation"] != generation
            or policy(before)["enabled"]
        ):
            raise ManagementError(
                "Recovery requires exact current closed fault9 checkpoint provenance"
            )
        StopProof.qualify(proof.files)
        proof.save(self.output)
        durable_json(
            self.output / "recovery-plan-before-command.json",
            {
                "operation": "recover_stop_checkpoint",
                "historical_stop_ok": False,
                "historical_closed_checkpoint_verified": True,
                "historical_fault_outstanding": 9,
                "manifest_sha256": STOP_PROOF_MANIFEST,
                "image_sha256_before_and_after": self.image_pin,
                "generation_before_and_after": generation,
                "uncommitted_bus_activity_not_excluded": True,
                "backing_image_mutation": False,
                "stale_seed_rollback": False,
            },
        )
        self.send_once(
            "EMU_RECOVER_STOP_CHECKPOINT",
            f"EMU_RECOVER_STOP_CHECKPOINT {self.image_pin} {generation}\n".encode(
                "ascii"
            ),
        )
        result = self.result("recover_stop_checkpoint")
        self.result_frame = result
        if result.get("ok") is True:
            passive(result)
            require_off(result)
            if (
                result["last_fault_code"] != 0
                or result["last_fault"] != "none"
                or result["unclean_boot"]
                or result["session_active_marker"]
            ):
                raise ManagementError("Fault9 metadata clear was not verified")
            for name in evidence.STABLE_FIELDS:
                if (
                    name not in {"last_fault", "last_fault_code", "unclean_boot"}
                    and before[name] != result[name]
                ):
                    raise ManagementError(
                        "Stop recovery changed unrelated checkpoint metadata"
                    )
            if policy(before) != policy(result):
                raise ManagementError("Stop recovery changed initialization policy")
        return result

    def run_action(
        self,
        action: str,
        *,
        generation: int = 1,
        sole: bool = False,
        proof: StopProof | None = None,
        capacity: int = MAX_CAPACITY,
        powered: bool = False,
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
                    "printer_power_asserted": powered,
                    "physical_D19_wiring_not_verified": True,
                },
            )
            chatter = self.reader.chatter
            if action in {"stop", "pullup-off"}:
                if action == "pullup-off":
                    passive(current)
                if current["loaded"]:
                    try:
                        before, _ = self.save_snapshot(
                            "before-stop-volatile"
                            if action == "stop"
                            else "before-pullup-off"
                        )
                    except Exception as error:  # noqa: BLE001 - optional export must not block release
                        optional_snapshot_error = {
                            "type": type(error).__name__,
                            "message": str(error),
                        }
                if action == "stop":
                    self.send_once("EMU_STOP")
                    result = self.result("stop")
                    self.result_frame = result
                else:
                    result = self.set_pullup(current, False, False)
                passive(result)
                require_off(result)
                if action == "stop" and result["dirty"]:
                    raise ManagementError("STOP checkpoint is unconfirmed")
                if current["unclean_boot"] and (
                    not result["unclean_boot"]
                    or result["session_active_marker"]
                    != current["session_active_marker"]
                ):
                    raise ManagementError(
                        "Release improperly cleared prior unclean evidence"
                    )
                if result["loaded"]:
                    after, postimage = self.save_snapshot(
                        "after-stop-checkpoint"
                        if action == "stop"
                        else "after-pullup-off"
                    )
                    if (
                        after["image_sha256"] != result["image_sha256"]
                        or after["image_generation"] != result["durable_generation"]
                    ):
                        raise ManagementError(
                            "Release snapshot differs from checkpoint"
                        )
                    if (
                        action == "pullup-off"
                        and before is not None
                        and bytes.fromhex(before["hex"]) != postimage
                    ):
                        raise ManagementError("D19 OFF changed image bytes")
                status = (
                    "stopped_checkpointed"
                    if action == "stop"
                    else "pullup_off_verified"
                )
            elif action == "status":
                result = current
                if current["loaded"]:
                    after, _ = self.save_snapshot("status-snapshot")
                    self.save_view("status-advertised-view")
                status = "status_and_snapshot_saved"
            elif action in {
                "diag",
                "start",
                "start-pullup",
                "pullup-on",
                "policy-enable",
                "policy-disable",
                "recover-stop",
            }:
                before, preimage = self.save_snapshot(f"before-{action}", pinned=True)
                passive(before)
                self.unchanged(current, before)
                if action in {"policy-enable", "policy-disable", "recover-stop"}:
                    if action == "recover-stop":
                        result = self.recover_stop(before, generation, proof)
                    else:
                        result = self.set_policy(
                            before, generation, action == "policy-enable", capacity
                        )
                    after, postimage = self.save_snapshot(
                        f"after-{action}", pinned=True
                    )
                    self.unchanged(result, after)
                    require_off(after)
                    view_frame, view = self.save_view(f"after-{action}-advertised-view")
                    expected = (
                        advertised_budget(preimage, capacity)
                        if action == "policy-enable"
                        else postimage
                    )
                    if view != expected or not view_frame["snapshot_is_current"]:
                        raise ManagementError(
                            "Policy advertised full view differs from exact planned bytes"
                        )
                    final = self.probe()
                    self.unchanged(after, final)
                    if any(
                        policy(frame) != policy(result)
                        for frame in (after, view_frame, final)
                    ):
                        raise ManagementError(
                            "Passive policy metadata changed after operation result"
                        )
                    status = (
                        "policy_transition_verified"
                        if action != "recover-stop"
                        else "closed_checkpoint_fault9_metadata_recovered"
                    )
                elif action == "diag":
                    require_off(before)
                    result, _ = self.diag(before)
                    after, postimage = self.save_snapshot("after-diag", pinned=True)
                    require_off(after)
                    self.unchanged(before, after)
                    self.unchanged(after, self.probe())
                    status = "detached_diagnostic_verified"
                elif action == "pullup-on":
                    result = self.set_pullup(before, True, powered)
                    after, postimage = self.save_snapshot(
                        "after-pullup-on", pinned=True
                    )
                    self.unchanged(before, after)
                    require_on(after)
                    status = "pullup_on_printer_power_asserted"
                else:
                    result = self.start(
                        before,
                        generation,
                        sole,
                        software=action == "start-pullup",
                        powered=powered,
                    )
                    postimage = preimage
                    status = "started_experimental"
                if postimage != preimage:
                    raise ManagementError("Operation changed a complete image byte")
            else:
                raise ManagementError("Unknown explicit V14 action")
            if result.get("ok", True) is not True or self.reader.chatter != chatter:
                raise ManagementError(
                    "V14 operation failed or raw boot/panic chatter appeared"
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
                "backing_counter_reset_or_crc_repair": False,
                "policy": policy(result),
                "virtual_capacity_is_not_physical_tape": True,
                "automatic_rescan_or_post_verify_refill": False,
                "transport_qualified": False,
            }
            durable_json(self.output / "receipt.json", receipt)
            return receipt
        except Exception as error:
            tail_error = None
            if self.command_may_have_reached:
                deadline = self.session.clock() + 1.5
                try:
                    while self.session.clock() < deadline:
                        self.reader.receive(deadline)
                except Exception as tail:  # noqa: BLE001 - retain the original failure
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
                },
            )
            raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=(
            "status",
            "diag",
            "pullup-on",
            "pullup-off",
            "start",
            "start-pullup",
            "stop",
            "policy-enable",
            "policy-disable",
            "recover-stop",
        ),
    )
    parser.add_argument("--port", required=True)
    parser.add_argument("--serial-number", required=True)
    parser.add_argument("--expect-elf-sha256", required=True)
    parser.add_argument("--image-sha256", required=True)
    parser.add_argument("--durable-generation", type=int)
    parser.add_argument("--capacity-counts", type=int, default=MAX_CAPACITY)
    parser.add_argument("--sole-0x50", action="store_true")
    parser.add_argument("--printer-powered", action="store_true")
    parser.add_argument("--recovery-evidence", type=Path)
    parser.add_argument("--recovery-manifest-sha256")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    require_hash(args.expect_elf_sha256)
    require_hash(args.image_sha256)
    if not 1 <= args.capacity_counts <= MAX_CAPACITY:
        parser.error("Capacity must be1..11232 counts")
    if args.action in {
        "start",
        "start-pullup",
        "policy-enable",
        "policy-disable",
        "recover-stop",
    } and (
        args.durable_generation is None or not 0 <= args.durable_generation < 0xFFFFFFFF
    ):
        parser.error("This mutation requires an exact --durable-generation")
    if args.action in {"start", "start-pullup"} and not args.sole_0x50:
        parser.error("START requires --sole-0x50")
    if args.action in {"start-pullup", "pullup-on"} and not args.printer_powered:
        parser.error("D19 HIGH requires --printer-powered")
    proof = None
    if args.action == "recover-stop":
        if not args.recovery_evidence or not args.recovery_manifest_sha256:
            parser.error("Recovery requires the exact failed-STOP checkpoint proof")
        proof = StopProof.load(args.recovery_evidence, args.recovery_manifest_sha256)
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
                generation=args.durable_generation or 0,
                sole=args.sole_0x50,
                proof=proof,
                capacity=args.capacity_counts,
                powered=args.printer_powered,
            )
        finally:
            uart.close()
    sys.stdout.write(
        json.dumps({"status": receipt["status"], "result": receipt["result"]}) + "\n"
    )


if __name__ == "__main__":
    try:
        main()
    except (ManagementError, OSError, ValueError, ImportError):
        sys.stderr.write(
            "V14 action unverified; inspect raw evidence before another command.\n"
        )
        raise SystemExit(1) from None
