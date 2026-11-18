"""Audited P53-local V7 emulator control. Imports never access hardware.

Templates: frozen manage_esp32.py UART/device guards, bench_eeprom.py durable
evidence/framing helpers and inspect_eeprom.py CRC inspection. A load retains
the selected complete image exactly. A start requires the operator's explicit
sole-0x50 assertion. This helper never resets a counter or repairs image CRCs.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import manage_emulator as v6
from bench_eeprom import (
    DurableAudit,
    digest,
    durable_bytes,
    durable_json,
    receive_frame,
    require_hash,
)
from inspect_eeprom import inspect_eeprom
from manage_esp32 import ManagementError, Session, validate_device

FIRMWARE = "i2c-eeprom-emulator-v7"
CAPABILITY = "eeprom_256_experimental_v1"
SCHEMA = "sunburn.brother.emulator-control.v1"
UINT32_MAX = (1 << 32) - 1
RECOVERY_MANIFEST_SHA256 = "b261679e6c6d8bb473544c566c821ffc06cc3b3010cf5e6f574e3ab28b261a66"
RECOVERY_SEED_SHA256 = "d7c0db4edb0db564e8980ba26b8ebf6f95082685aa4e0b40c8c8630b4715e091"
V6_ELF_SHA256 = "3dcca8476b6208286e97e7f3f96916c5cb28590d329afdb12d91373f204f33be"


def integer(value: object, name: str) -> int:
    if type(value) is not int or not 0 <= value <= UINT32_MAX:
        raise ManagementError(f"Invalid unsigned generation/value: {name}")
    return value


def state(frame: dict[str, object]) -> dict[str, object]:
    if (
        frame.get("firmware") != FIRMWARE
        or frame.get("capability") != CAPABILITY
        or type(frame.get("address")) is not int
        or frame.get("address") != 0x50
        or frame.get("transport_qualified") is not False
        or not isinstance(frame.get("last_fault"), str)
    ):
        raise ManagementError("Unexpected experimental emulator identity or address")
    for name in (
        "emulation_active",
        "session_open",
        "loaded",
        "dirty",
        "unclean_boot",
        "session_active_marker",
        "pins_input_only",
        "scl_input_only",
        "sda_open_drain",
        "internal_pulls",
    ):
        if type(frame.get(name)) is not bool:
            raise ManagementError("Emulator state contains an unknown boolean")
    live = integer(frame.get("live_generation"), "live_generation")
    durable = integer(frame.get("durable_generation"), "durable_generation")
    if durable > live or frame["dirty"] != (durable != live):
        raise ManagementError("Emulator durability generations are inconsistent")
    if frame["scl_input_only"] is not True or frame["internal_pulls"] is not False:
        raise ManagementError("Unexpected SCL drive or internal pull configuration")
    if frame["emulation_active"]:
        if (
            frame["loaded"] is not True
            or frame["session_open"] is not True
            or frame["pins_input_only"] is not False
            or frame["sda_open_drain"] is not True
            or frame["session_active_marker"] is not True
            or frame["unclean_boot"] is not False
            or frame.get("image_sha256") != ""
        ):
            raise ManagementError("Active emulator state/pins/session marker are inconsistent")
    elif frame["pins_input_only"] is not True:
        raise ManagementError("Inactive emulator has not released both pins")
    if frame["loaded"]:
        require_hash(frame.get("seed_sha256"))
        hash_available = (
            not frame["emulation_active"] and not frame["session_open"] and not frame["dirty"]
        )
        if hash_available:
            require_hash(frame.get("image_sha256"))
        elif frame.get("image_sha256") != "":
            raise ManagementError("Uncheckpointed emulator claims a cached current image hash")
    elif frame.get("image_sha256") != "" or frame.get("seed_sha256") != "":
        raise ManagementError("Unloaded emulator claims an image")
    integer(frame.get("last_fault_code"), "last_fault_code")
    diagnostics = frame.get("attach_diagnostics")
    if not isinstance(diagnostics, dict) or not isinstance(diagnostics.get("stage"), str):
        raise ManagementError("V7 attach diagnostics are missing")
    for name in ("core", "levels", "output_enable", "sda_mux", "scl_mux"):
        integer(diagnostics.get(name), name)
    if diagnostics["levels"] > 3 or any(
        type(diagnostics.get(name)) is not bool for name in ("pins_input_only", "internal_pulls")
    ):
        raise ManagementError("V7 diagnostic pin samples are invalid")
    for name in ("irq_error", "gpio_error"):
        if type(diagnostics.get(name)) is not int or not -(1 << 31) <= diagnostics[name] < (
            1 << 31
        ):
            raise ManagementError("V7 diagnostic error value is invalid")
    return frame


def snapshot(frame: dict[str, object]) -> tuple[bytes, dict[str, object]]:
    state(frame)
    value = frame.get("hex")
    if (
        frame.get("type") != "emu_snapshot"
        or frame.get("ok", True) is not True
        or frame["loaded"] is not True
        or type(frame.get("bytes")) is not int
        or frame.get("bytes") != 256
        or not isinstance(value, str)
        or len(value) != 512
        or value != value.lower()
        or type(frame.get("snapshot_is_current")) is not bool
    ):
        raise ManagementError("Emulator snapshot is not a coherent complete image")
    try:
        data = bytes.fromhex(value)
    except ValueError as error:
        raise ManagementError("Emulator snapshot hex is invalid") from error
    if len(data) != 256 or require_hash(frame.get("sha256")) != digest(data):
        raise ManagementError("Emulator snapshot byte count/hash mismatch")
    generation = integer(frame.get("image_generation"), "image_generation")
    if generation > frame["live_generation"] or frame["snapshot_is_current"] != (
        generation == frame["live_generation"]
    ):
        raise ManagementError("Snapshot generation/currentness is inconsistent")
    inspection = inspect_eeprom(data)
    for label, check in zip(
        ("crc_r0", "crc_rw1", "crc_rw2"), inspection["crc_checks"], strict=True
    ):
        if type(frame.get(label)) is not bool or frame[label] != check["matches"]:
            raise ManagementError("Snapshot CRC report differs from independent calculation")
    if frame.get("image_sha256") and frame["image_sha256"] != digest(data):
        raise ManagementError("Inactive image state does not match snapshot bytes")
    return data, inspection


@dataclass(frozen=True)
class Seed:
    data: bytes
    sha256: str
    provenance: dict[str, object]
    provenance_bytes: bytes
    provenance_sha256: str

    @classmethod
    def load(cls, image: Path, image_pin: str, provenance: Path, provenance_pin: str) -> Seed:
        require_hash(image_pin)
        require_hash(provenance_pin)
        with image.open("rb") as stream:
            data = stream.read(257)
        with provenance.open("rb") as stream:
            evidence = stream.read(131073)
        if (
            len(data) != 256
            or digest(data) != image_pin
            or len(evidence) > 131072
            or digest(evidence) != provenance_pin
        ):
            raise ManagementError("Selected seed/provenance does not match exact bounded pins")
        if not inspect_eeprom(data)["all_three_crc_regions_match"]:
            raise ManagementError("Selected seed fails independent EEPROM CRC validation")
        try:
            parsed = json.loads(evidence)
        except (ValueError, UnicodeError) as error:
            raise ManagementError("Seed provenance is not valid JSON") from error
        if not isinstance(parsed, dict):
            raise ManagementError("Seed provenance is not a JSON object")
        schema = parsed.get("schema")
        if schema == "sunburn.brother.assembled-emulator-seed.v1":
            valid = (
                parsed.get("sha256") == image_pin
                and parsed.get("bytes") == 256
                and parsed.get("unknown_bytes_filled") is False
                and parsed.get("all_three_crcs_match") is True
                and parsed.get("counter_automatically_reset") is False
                and parsed.get("remaining_u16_le") == int.from_bytes(data[0xE8:0xEA], "little")
            )
        elif schema == "sunburn.brother.isolated-bench.v1":
            valid = (
                parsed.get("status") == "verified_restore"
                and parsed.get("action") == "restore"
                and parsed.get("postimage_sha256") == image_pin
                and parsed.get("reads_identical") is True
                and parsed.get("all_crcs_valid") is True
            )
        elif schema == SCHEMA:
            saved = parsed.get("snapshot")
            valid = (
                parsed.get("status")
                in {"snapshot_saved", "stopped_checkpointed", "loaded_verified"}
                and isinstance(saved, dict)
                and saved.get("sha256") == image_pin
                and saved.get("emulation_active") is False
                and saved.get("dirty") is False
                and saved.get("unclean_boot") is False
                and saved.get("session_active_marker") is False
                and saved.get("session_open") is False
                and saved.get("all_three_crcs_match") is True
                and saved.get("snapshot_bytes_are_confirmed_durable") is True
            )
        else:
            valid = False
        if not valid:
            raise ManagementError("Seed provenance is not a reviewed complete-image source")
        return cls(data, image_pin, parsed, evidence, provenance_pin)


def qualify_v6_recovery(records: dict[str, bytes]) -> tuple[bytes, int]:
    """Qualify the observed failed START/explicit STOP, never reboot-zero metrics."""
    documents = {name: json.loads(data) for name, data in records.items() if name.endswith(".json")}
    start, stop = documents["start-result.json"], documents["stop-result.json"]
    before = records["start-before.bin"]
    if (
        len(before) != 256
        or digest(before) != RECOVERY_SEED_SHA256
        or records["stop-before.bin"] != before
        or not inspect_eeprom(before)["all_three_crc_regions_match"]
    ):
        raise ManagementError("Legacy recovery source image is not the unchanged consumed seed")
    for name, op, result, message in (
        ("start", "start", start, "hardware_attach_failed"),
        ("stop", "stop", stop, "detached_saved_with_fault"),
    ):
        v6.state(result)
        failure = documents[f"{name}-failure.json"]
        image, inspection = v6.snapshot(documents[f"{name}-snapshot.json"])
        if (
            result.get("type") != "emu_result"
            or result.get("op") != op
            or result.get("ok") is not False
            or result.get("message") != message
            or result["emulation_active"]
            or result["dirty"]
            or result["unclean_boot"]
            or result["live_generation"] != 1
            or result["durable_generation"] != 1
            or result["seed_sha256"] != RECOVERY_SEED_SHA256
            or result["last_fault"] != "gpio_release_failed"
            or failure.get("schema") != SCHEMA
            or failure.get("action") != op
            or failure.get("firmware") != v6.FIRMWARE
            or failure.get("app_elf_sha256") != V6_ELF_SHA256
            or failure.get("status") != "unverified"
            or failure.get("command_may_have_reached_device") is not True
            or failure.get("last_device_frame") != result
            or image != before
            or documents[f"{name}-snapshot.json"]["image_generation"] != 1
            or not inspection["all_three_crc_regions_match"]
        ):
            raise ManagementError("Legacy V6 failure/STOP provenance did not qualify")
        metrics = result.get("metrics")
        if (
            not isinstance(metrics, dict)
            or not metrics
            or any(type(value) is not int or value != 0 for value in metrics.values())
        ):
            raise ManagementError("Historical V6 attach evidence has observed bus activity")
    ownership = documents["start-ownership.json"]
    if (
        not start["session_open"]
        or not start["session_active_marker"]
        or stop["session_open"]
        or stop["session_active_marker"]
        or stop["image_sha256"] != RECOVERY_SEED_SHA256
        or ownership.get("operator_asserts_sole_0x50") is not True
        or ownership.get("image_sha256") != RECOVERY_SEED_SHA256
        or ownership.get("durable_generation") != 1
    ):
        raise ManagementError("Legacy START and explicit STOP closure did not qualify")
    return before, 1


@dataclass(frozen=True)
class RecoveryProof:
    records: dict[str, bytes]
    manifest_bytes: bytes
    manifest_sha256: str
    image: bytes
    generation: int

    @classmethod
    def load(cls, directory: Path, manifest_pin: str) -> RecoveryProof:
        if require_hash(manifest_pin) != RECOVERY_MANIFEST_SHA256:
            raise ManagementError("Only the reviewed V6 attach event manifest is recoverable")
        with (directory / "manifest.json").open("rb") as stream:
            evidence = stream.read(65537)
        if len(evidence) > 65536 or digest(evidence) != manifest_pin:
            raise ManagementError("Recovery manifest does not match the reviewed exact pin")
        parsed = json.loads(evidence)
        expected_names = {
            "start-result.json",
            "start-failure.json",
            "start-before.bin",
            "start-snapshot.json",
            "start-ownership.json",
            "stop-result.json",
            "stop-failure.json",
            "stop-before.bin",
            "stop-snapshot.json",
        }
        if (
            parsed.get("schema") != "sunburn.brother.v7-legacy-attach-recovery.v1"
            or parsed.get("event_id")
            != "emulator-v6-start-01_then_emulator-v6-stop-after-attach-failure-01"
            or parsed.get("expected_image_sha256") != RECOVERY_SEED_SHA256
            or parsed.get("durable_generation") != 1
            or parsed.get("v6_elf_sha256") != V6_ELF_SHA256
            or not isinstance(parsed.get("files"), dict)
            or set(parsed["files"]) != expected_names
        ):
            raise ManagementError("Recovery manifest scope is not the reviewed V6 event")
        records = {}
        for name in sorted(expected_names):
            with (directory / name).open("rb") as stream:
                data = stream.read(65537)
            item = parsed["files"][name]
            if len(data) > 65536 or len(data) != item["bytes"] or digest(data) != item["sha256"]:
                raise ManagementError("Recovery event source changed from its exact evidence pin")
            records[name] = data
        image, generation = qualify_v6_recovery(records)
        return cls(records, evidence, manifest_pin, image, generation)


class Control:
    def __init__(self, session: Session, output: Path, elf_pin: str) -> None:
        self.session, self.output, self.elf_pin = session, output, require_hash(elf_pin)
        self.frame_number = 0
        self.command_may_have_reached = False
        self.last_frame: dict[str, object] | None = None

    def receive(self, kind: str, timeout: float = 5) -> dict[str, object]:
        frame = receive_frame(self.session, self.session.clock() + timeout)
        durable_json(self.output / f"frame-{self.frame_number:03d}.json", frame)
        self.frame_number += 1
        self.last_frame = frame
        if frame.get("type") != kind or frame.get("ok", True) is False:
            raise ManagementError("Device rejected control or returned an unexpected response")
        return frame

    def exchange(self, command: str, kind: str, timeout: float = 5) -> dict[str, object]:
        self.session.command(command)
        return self.receive(kind, timeout)

    def probe(self) -> dict[str, object]:
        info = self.exchange("INFO", "status")
        if (
            info.get("firmware") != FIRMWARE
            or info.get("emulator_capability") != CAPABILITY
            or info.get("cpu_mhz") != 240
            or info.get("sda_gpio") != 21
            or info.get("scl_gpio") != 22
            or info.get("armed") is not False
        ):
            raise ManagementError("Unexpected V7 firmware or GPIO/CPU configuration")
        network = self.exchange("NETINFO", "netinfo")
        if (
            network.get("app_elf_sha256") != self.elf_pin
            or network.get("armed") is not False
            or network.get("update_busy") is not False
        ):
            raise ManagementError("Firmware ELF, capture or update state did not match")
        current = state(self.exchange("EMU_STATUS", "emu_status"))
        if (
            info.get("pins_input_only") != current["pins_input_only"]
            or info.get("internal_pulls") != current["internal_pulls"]
        ):
            raise ManagementError("INFO and emulator pin state disagree")
        return current

    def save_snapshot(self, label: str) -> dict[str, object]:
        frame = self.exchange("EMU_SNAPSHOT", "emu_snapshot")
        data, inspection = snapshot(frame)
        durable_bytes(self.output / f"{label}.bin", data)
        durable_json(self.output / f"{label}-inspection.json", inspection)
        return {
            "sha256": digest(data),
            "bytes": 256,
            "image_generation": frame["image_generation"],
            "seed_sha256": frame["seed_sha256"],
            "live_generation": frame["live_generation"],
            "durable_generation": frame["durable_generation"],
            "snapshot_is_current": frame["snapshot_is_current"],
            "emulation_active": frame["emulation_active"],
            "dirty": frame["dirty"],
            "unclean_boot": frame["unclean_boot"],
            "session_active_marker": frame["session_active_marker"],
            "session_open": frame["session_open"],
            "all_three_crcs_match": inspection["all_three_crc_regions_match"],
            "snapshot_bytes_are_confirmed_durable": not frame["emulation_active"]
            and not frame["dirty"]
            and not frame["unclean_boot"]
            and not frame["session_open"]
            and not frame["session_active_marker"]
            and frame["image_generation"] == frame["durable_generation"],
        }

    def result(self, op: str, timeout: float = 10) -> dict[str, object]:
        frame = state(self.receive("emu_result", timeout))
        if frame.get("op") != op or frame.get("ok") is not True:
            raise ManagementError("Emulator control result does not match the operation")
        return frame

    def run(
        self,
        action: str,
        seed: Seed | None = None,
        image_pin: str | None = None,
        durable_generation: int | None = None,
        sole_0x50: bool = False,
        recovery_proof: RecoveryProof | None = None,
    ) -> dict[str, object]:
        if action not in {"status", "snapshot", "load", "start", "stop", "recover"}:
            raise ManagementError("Unknown emulator action")
        try:
            current = self.probe()
        except Exception:
            durable_json(
                self.output / "failure.json",
                {
                    "schema": SCHEMA,
                    "action": action,
                    "status": "unverified_probe",
                    "command_may_have_reached_device": False,
                    "last_device_frame": self.last_frame,
                    "automatic_retry": False,
                },
            )
            raise
        intent = {
            "schema": SCHEMA,
            "action": action,
            "firmware": FIRMWARE,
            "app_elf_sha256": self.elf_pin,
            "automatic_retry": False,
            "transport_qualified": False,
            "before": current,
        }
        durable_json(self.output / "intent-before-command.json", intent)
        saved = None
        pre_stop_snapshot_error = None
        final = current
        try:
            if action == "recover":
                require_hash(image_pin)
                integer(durable_generation, "expected durable_generation")
                if (
                    recovery_proof is None
                    or recovery_proof.manifest_sha256 != RECOVERY_MANIFEST_SHA256
                    or digest(recovery_proof.manifest_bytes) != RECOVERY_MANIFEST_SHA256
                    or qualify_v6_recovery(recovery_proof.records) != (recovery_proof.image, 1)
                    or image_pin != RECOVERY_SEED_SHA256
                    or durable_generation != recovery_proof.generation
                    or current["emulation_active"]
                    or current["session_open"]
                    or not current["loaded"]
                    or current["dirty"]
                    or current["session_active_marker"]
                    or current["last_fault_code"] != 10
                    or current["last_fault"] != "gpio_release_failed"
                    or current["live_generation"] != durable_generation
                    or current["durable_generation"] != durable_generation
                    or current["image_sha256"] != image_pin
                    or current["seed_sha256"] != image_pin
                ):
                    raise ManagementError(
                        "RECOVER requires the exact reviewed legacy V6 fault/event/seed"
                    )
                evidence_directory = self.output / "reviewed-v6-evidence"
                evidence_directory.mkdir()
                durable_bytes(evidence_directory / "manifest.json", recovery_proof.manifest_bytes)
                for name, data in recovery_proof.records.items():
                    durable_bytes(evidence_directory / name, data)
                saved = self.save_snapshot("before-recover")
                if (
                    saved["sha256"] != image_pin
                    or saved["image_generation"] != durable_generation
                    or not saved["snapshot_is_current"]
                    or not saved["all_three_crcs_match"]
                    or saved["emulation_active"]
                    or saved["session_open"]
                    or saved["session_active_marker"]
                    or saved["dirty"]
                    or (self.output / "before-recover.bin").read_bytes() != recovery_proof.image
                ):
                    raise ManagementError(
                        "RECOVER fresh full image differs from the reviewed unchanged seed"
                    )
                durable_json(
                    self.output / "recovery-plan-before-command.json",
                    {
                        "recovery_manifest_sha256": RECOVERY_MANIFEST_SHA256,
                        "scope": "ATTACH_V6_UNCHANGED",
                        "image_sha256_before_and_after": image_pin,
                        "generation_before_and_after": durable_generation,
                        "raw_legacy_fault_code": 10,
                        "clear_only_fault_and_unclean_metadata": True,
                        "image_or_count_or_generation_change_permitted": False,
                        "runtime_zero_metrics_used_as_proof": False,
                        "automatic_retry": False,
                    },
                )
                self.command_may_have_reached = True
                self.session.command(
                    "EMU_RECOVER",
                    f"EMU_RECOVER ATTACH_V6_UNCHANGED {image_pin} {durable_generation}\n".encode(
                        "ascii"
                    ),
                )
                final = self.result("recover")
                if (
                    final.get("message") != "unchanged_seed_fault_metadata_cleared"
                    or final["emulation_active"]
                    or final["session_open"]
                    or final["session_active_marker"]
                    or final["dirty"]
                    or final["unclean_boot"]
                    or final["last_fault"] != "none"
                    or final["last_fault_code"] != 0
                    or final["image_sha256"] != image_pin
                    or final["seed_sha256"] != image_pin
                    or final["live_generation"] != durable_generation
                    or final["durable_generation"] != durable_generation
                ):
                    raise ManagementError(
                        "RECOVER did not preserve image/generation and clear only fault metadata"
                    )
                saved = self.save_snapshot("after-recover")
                if (
                    saved["sha256"] != image_pin
                    or saved["image_generation"] != durable_generation
                    or not saved["snapshot_bytes_are_confirmed_durable"]
                    or not saved["all_three_crcs_match"]
                    or (self.output / "after-recover.bin").read_bytes() != recovery_proof.image
                ):
                    raise ManagementError(
                        "RECOVER final complete durable image did not match reviewed seed"
                    )
            elif action == "load":
                if (
                    seed is None
                    or current["emulation_active"]
                    or current["session_open"]
                    or current["dirty"]
                    or current["unclean_boot"]
                    or current["session_active_marker"]
                ):
                    raise ManagementError(
                        "LOAD requires an exact seed and clean passive state without unsettled evidence"
                    )
                durable_bytes(self.output / "selected-seed.bin", seed.data)
                durable_bytes(
                    self.output / "selected-source-provenance.json", seed.provenance_bytes
                )
                durable_json(
                    self.output / "selected-source-pins.json",
                    {
                        "image_sha256": seed.sha256,
                        "provenance_sha256": seed.provenance_sha256,
                        "source_schema": seed.provenance["schema"],
                        "remaining_u16_le": int.from_bytes(seed.data[0xE8:0xEA], "little"),
                        "counter_reset_or_crc_repair": False,
                    },
                )
                self.command_may_have_reached = True
                self.session.command("EMU_LOAD", f"EMU_LOAD 256 {seed.sha256}\n".encode("ascii"))
                ready = self.receive("emu_load_ready")
                if (
                    type(ready.get("bytes")) is not int
                    or ready.get("bytes") != 256
                    or ready.get("sha256") != seed.sha256
                    or ready.get("timeout_ms") != 5000
                ):
                    raise ManagementError("LOAD readiness framing does not match exact image")
                self.session.write(seed.data, self.session.clock() + 5)
                final = self.result("load")
                if (
                    final["emulation_active"]
                    or not final["loaded"]
                    or final["dirty"]
                    or final["unclean_boot"]
                    or final["session_active_marker"]
                    or final["session_open"]
                    or final["last_fault"] != "none"
                    or final.get("image_sha256") != seed.sha256
                    or final.get("seed_sha256") != seed.sha256
                ):
                    raise ManagementError("LOAD did not confirm the exact clean durable image")
                saved = self.save_snapshot("loaded-snapshot")
                if (
                    saved["sha256"] != seed.sha256
                    or saved["seed_sha256"] != seed.sha256
                    or saved["all_three_crcs_match"] is not True
                    or saved["snapshot_bytes_are_confirmed_durable"] is not True
                ):
                    raise ManagementError(
                        "Loaded full snapshot did not match durable selected seed"
                    )
            elif action == "start":
                require_hash(image_pin)
                integer(durable_generation, "expected durable_generation")
                if (
                    not sole_0x50
                    or current["emulation_active"]
                    or not current["loaded"]
                    or current["dirty"]
                    or current["session_open"]
                    or current["unclean_boot"]
                    or current["session_active_marker"]
                    or current["last_fault"] != "none"
                    or current.get("image_sha256") != image_pin
                    or current["durable_generation"] != durable_generation
                ):
                    raise ManagementError(
                        "START requires sole-address assertion and exact clean durable image/generation"
                    )
                saved = self.save_snapshot("before-start")
                if (
                    saved["sha256"] != image_pin
                    or saved["image_generation"] != durable_generation
                    or not saved["snapshot_is_current"]
                    or not saved["all_three_crcs_match"]
                    or not saved["snapshot_bytes_are_confirmed_durable"]
                ):
                    raise ManagementError(
                        "START preimage is not the exact qualified durable snapshot"
                    )
                durable_json(
                    self.output / "sole-address-start-before-command.json",
                    {
                        "operator_asserts_sole_0x50": True,
                        "image_sha256": image_pin,
                        "durable_generation": durable_generation,
                        "automatic_counter_reset": False,
                    },
                )
                self.command_may_have_reached = True
                self.session.command(
                    "EMU_START",
                    f"EMU_START SOLE_0X50 {image_pin} {durable_generation}\n".encode("ascii"),
                )
                final = self.result("start")
                if (
                    not final["emulation_active"]
                    or final["durable_generation"] != durable_generation
                    or final["last_fault"] != "none"
                ):
                    raise ManagementError("START active state/durable generation not confirmed")
            elif action == "stop":
                if current["loaded"]:
                    try:
                        self.save_snapshot("before-stop-volatile")
                    except Exception as error:
                        pre_stop_snapshot_error = {
                            "exception_type": type(error).__name__,
                            "message": str(error),
                            "last_device_frame": self.last_frame,
                            "stop_will_still_be_sent_once": True,
                        }
                        try:
                            durable_json(
                                self.output / "before-stop-snapshot-error.json",
                                pre_stop_snapshot_error,
                            )
                        except Exception as diagnostic_error:
                            # An optional snapshot/export failure cannot prevent detach.
                            pre_stop_snapshot_error["diagnostic_write_failed"] = {
                                "exception_type": type(diagnostic_error).__name__,
                                "message": str(diagnostic_error),
                            }
                self.command_may_have_reached = True
                self.session.command("EMU_STOP")
                final = self.result("stop")
                preserved_unclean = not current["emulation_active"] and current["unclean_boot"]
                if (
                    final["emulation_active"]
                    or final["session_open"]
                    or final["dirty"]
                    or (final["session_active_marker"] and not preserved_unclean)
                ):
                    raise ManagementError("STOP did not detach and confirm a clean checkpoint")
                if preserved_unclean and (
                    not final["unclean_boot"]
                    or final["session_active_marker"] != current["session_active_marker"]
                ):
                    raise ManagementError("STOP improperly cleared prior unclean-boot evidence")
                if final["last_fault"] != "none":
                    raise ManagementError("STOP retained a transport fault; review saved state")
                if final["loaded"]:
                    saved = self.save_snapshot("after-stop-checkpoint")
                    if (
                        saved["sha256"] != final["image_sha256"]
                        or saved["image_generation"] != final["durable_generation"]
                        or saved["emulation_active"]
                        or saved["dirty"]
                    ):
                        raise ManagementError(
                            "STOP checkpoint snapshot did not match exact durable state"
                        )
            elif action == "snapshot":
                saved = self.save_snapshot("snapshot")
                final = state(dict(self.last_frame))
            status = {
                "status": "status_saved",
                "snapshot": "snapshot_saved",
                "load": "loaded_verified",
                "start": "started_experimental",
                "stop": "stopped_checkpointed",
                "recover": "recovered_legacy_attach_metadata",
            }[action]
            if action == "stop" and final["unclean_boot"]:
                status = "stopped_unclean_state_preserved"
            receipt = {
                **intent,
                "status": status,
                "result": final,
                "snapshot": saved,
                "pre_stop_snapshot_error": pre_stop_snapshot_error,
                "physical_transport_success_proven": False,
                "counter_reset_or_crc_repair": False,
            }
            durable_json(self.output / "receipt.json", receipt)
            return receipt
        except Exception:
            durable_json(
                self.output / "failure.json",
                {
                    **intent,
                    "status": "unverified",
                    "command_may_have_reached_device": self.command_may_have_reached,
                    "last_device_frame": self.last_frame,
                    "pre_stop_snapshot_error": pre_stop_snapshot_error,
                    "automatic_retry": False,
                },
            )
            raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("status", "snapshot", "load", "start", "stop", "recover")
    )
    parser.add_argument("--port", required=True)
    parser.add_argument("--serial-number", required=True)
    parser.add_argument("--expect-elf-sha256", required=True)
    parser.add_argument("--image", type=Path)
    parser.add_argument("--image-sha256")
    parser.add_argument("--source-provenance", type=Path)
    parser.add_argument("--source-provenance-sha256")
    parser.add_argument("--sole-0x50", action="store_true")
    parser.add_argument("--durable-generation", type=int)
    parser.add_argument("--recovery-evidence", type=Path)
    parser.add_argument("--recovery-manifest-sha256")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    require_hash(args.expect_elf_sha256)
    seed = None
    recovery_proof = None
    if args.action == "load":
        if not all(
            (args.image, args.image_sha256, args.source_provenance, args.source_provenance_sha256)
        ):
            parser.error("LOAD requires exact image/provenance files and SHA256 pins")
        seed = Seed.load(
            args.image, args.image_sha256, args.source_provenance, args.source_provenance_sha256
        )
    elif args.action == "start" and (
        not args.sole_0x50 or args.durable_generation is None or args.image_sha256 is None
    ):
        parser.error("START requires --sole-0x50, --image-sha256 and --durable-generation")
    elif args.action == "recover":
        if (
            not all((args.recovery_evidence, args.recovery_manifest_sha256, args.image_sha256))
            or args.durable_generation is None
        ):
            parser.error(
                "RECOVER requires exact reviewed evidence/manifest pin, image pin and generation"
            )
        recovery_proof = RecoveryProof.load(args.recovery_evidence, args.recovery_manifest_sha256)
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
        uart.dtr = False
        uart.rts = False
        uart.port = args.port
        try:
            uart.open()
            time.sleep(1.5)
            uart.reset_input_buffer()
            receipt = Control(
                Session(uart, audit, expected_firmware=FIRMWARE),
                args.output,
                args.expect_elf_sha256,
            ).run(
                args.action,
                seed,
                args.image_sha256,
                args.durable_generation,
                args.sole_0x50,
                recovery_proof,
            )
        finally:
            uart.close()
    print(
        json.dumps(
            {
                "status": receipt["status"],
                "result": receipt["result"],
                "snapshot": receipt["snapshot"],
                "transport_qualified": False,
            }
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print(
            "Emulator control failed; review retained evidence before any retry.", file=sys.stderr
        )
        raise SystemExit(1) from None
