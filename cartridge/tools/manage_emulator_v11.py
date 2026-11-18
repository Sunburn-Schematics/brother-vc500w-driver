"""V11 read-fault recovery and guarded D19 pullup control with raw evidence.

Template: frozen V9 common guards and V8 raw UART reader. No counter/image reset,
CRC repair, hidden retry or automatic printer power detection is implemented.
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

FIRMWARE = "i2c-eeprom-emulator-v11"
CAPABILITY = evidence.CAPABILITY
SCHEMA = "sunburn.brother.emulator-v11.v1"
RECOVERY_MANIFEST = "951959713c713a50d43297fc4af5eff8cded9cb940d10bc51e94c9a82ba351a5"
RECOVERY_SEED = "d7c0db4edb0db564e8980ba26b8ebf6f95082685aa4e0b40c8c8630b4715e091"
RECOVERY_FILES = {
    "start-receipt.json",
    "start-before.bin",
    "fault-status-receipt.json",
    "fault-status-image.bin",
    "stop-failure.json",
    "stop-before.bin",
    "stop-after.bin",
    *(f"start-raw-{i:03d}.bin" for i in range(7)),
    *(f"fault-raw-{i:03d}.bin" for i in range(4)),
    *(f"stop-raw-{i:03d}.bin" for i in range(6)),
}


def state(frame: dict[str, object]) -> dict[str, object]:
    if frame.get("firmware") != FIRMWARE:
        raise ManagementError("Unexpected V11 firmware identity")
    v7.state({**frame, "firmware": v7.FIRMWARE})
    v7.integer(frame.get("reset_reason"), "reset_reason")
    crumb = frame.get("boot_breadcrumb")
    if not isinstance(crumb, dict) or type(crumb.get("valid")) is not bool:
        raise ManagementError("Missing typed V11 reset breadcrumb")
    stage = v7.integer(crumb.get("stage"), "breadcrumb.stage")
    inverse = v7.integer(crumb.get("stage_inverse"), "breadcrumb.stage_inverse")
    if crumb["valid"] and stage ^ inverse != 0xFFFFFFFF:
        raise ManagementError("Invalid retained breadcrumb inverse")
    diagnostics = frame.get("isr_diagnostics")
    if not isinstance(diagnostics, dict):
        raise ManagementError("Missing V11 ISR diagnostics")
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
        raise ManagementError("Missing exact V11 read-accounting diagnostic method")
    for name in ("loaded", "fifo_remaining", "consumed", "rx_irq_diagnostic", "tx_irq_diagnostic"):
        v7.integer(accounting.get(name), "read_accounting." + name)
    if type(accounting.get("terminal_nack")) is not bool:
        raise ManagementError("Unknown V11 terminal-NACK diagnostic")
    return frame


def passive(frame: dict[str, object]) -> None:
    state(frame)
    if frame["emulation_active"] or frame["session_open"] or not frame["pins_input_only"]:
        raise ManagementError("Operation requires a closed passive emulator")


def registers(frame: dict[str, object]) -> None:
    evidence.registers(frame)
    if frame["fifo_conf"] & 31 != 1:
        raise ManagementError("V11 RX FIFO threshold is not the exact reviewed value1")


def pullup(frame: dict) -> dict:
    value = frame.get("pullup_control")
    if not isinstance(value, dict) or type(value.get("gpio")) is not int or value["gpio"] != 19:
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


@dataclass(frozen=True)
class ReadProof:
    manifest: bytes
    files: dict[str, bytes]

    @classmethod
    def load(cls, directory: Path, pin: str) -> ReadProof:
        if require_hash(pin) != RECOVERY_MANIFEST:
            raise ManagementError("Recovery requires the exact read-fault event pin")
        with (directory / "manifest.json").open("rb") as stream:
            manifest = stream.read(131073)
        if len(manifest) > 131072 or digest(manifest) != RECOVERY_MANIFEST:
            raise ManagementError("Read event manifest changed")
        description = json.loads(manifest)
        if set(description.get("files", {})) != RECOVERY_FILES:
            raise ManagementError("Read event inventory changed")
        files = {}
        for name, expected in description["files"].items():
            with (directory / name).open("rb") as stream:
                data = stream.read(131073)
            if (
                len(data) > 131072
                or len(data) != expected["bytes"]
                or digest(data) != expected["sha256"]
            ):
                raise ManagementError("Read event source file changed")
            files[name] = data
        cls.qualify(files)
        return cls(manifest, files)

    @staticmethod
    def qualify(files: dict[str, bytes]) -> None:
        if set(files) != RECOVERY_FILES:
            raise ManagementError("Incomplete read-fault provenance")
        for name in (
            "start-before.bin",
            "fault-status-image.bin",
            "stop-before.bin",
            "stop-after.bin",
        ):
            if len(files[name]) != 256 or digest(files[name]) != RECOVERY_SEED:
                raise ManagementError("Read event full image changed")
        start_receipt = json.loads(files["start-receipt.json"])
        fault_receipt = json.loads(files["fault-status-receipt.json"])
        stop_receipt = json.loads(files["stop-failure.json"])
        started = start_receipt["result"]
        fault = fault_receipt["result"]
        stop = stop_receipt["result"]
        history = fault.get("irq_history")
        if (
            start_receipt.get("status") != "started_experimental"
            or started.get("firmware") != "i2c-eeprom-emulator-v10"
            or started.get("ok") is not True
            or started.get("emulation_active") is not True
            or not isinstance(history, list)
            or len(history) != 16
            or history[-1].get("flags") != 1024
            or history[-1].get("tx_fifo") != 32
            or fault["metrics"]["rx_bytes"] != 1
            or fault["metrics"]["tx_prefetched_bytes"] != 320
            or fault["metrics"]["tx_completed_bytes"] != 0
            or fault["metrics"]["nacks"] != 1
            or fault["metrics"]["fifo_accounting_errors"] != 1
            or fault["isr_diagnostics"]["isr_calls"] != 38
            or fault["last_fault_code"] != 2
            or fault["last_fault"] != "tx_byte_count_unqualified"
            or stop.get("message") != "detached_saved_with_fault"
            or stop.get("ok") is not False
            or stop["emulation_active"]
            or stop["session_open"]
            or stop["session_active_marker"]
            or stop["dirty"]
            or not stop["pins_input_only"]
            or stop["internal_pulls"]
            or stop["last_fault_code"] != 2
            or stop["last_fault"] != "tx_byte_count_unqualified"
            or stop["image_sha256"] != RECOVERY_SEED
            or stop["seed_sha256"] != RECOVERY_SEED
            or stop["live_generation"] != 1
            or stop["durable_generation"] != 1
        ):
            raise ManagementError("Provenance is not the reviewed closed read-accounting fault")
        require_off(stop)
        for receipt in (start_receipt, fault_receipt, stop_receipt):
            if receipt.get("raw_chatter_lines_saved") != 0:
                raise ManagementError("Read event contains boot or panic chatter")
        for prefix, count, receipt in (
            ("start", 7, start_receipt),
            ("fault", 4, fault_receipt),
            ("stop", 6, stop_receipt),
        ):
            parsed = [json.loads(files[f"{prefix}-raw-{i:03d}.bin"]) for i in range(count)]
            if (
                parsed[6 if prefix == "start" else 2 if prefix == "fault" else 4]
                != receipt["result"]
            ):
                raise ManagementError("Raw event result differs from its receipt")
            if prefix in {"fault", "stop"}:
                snapshot = parsed[3 if prefix == "fault" else 5]
                if (
                    snapshot != receipt["after"]
                    or bytes.fromhex(snapshot["hex"])
                    != files["fault-status-image.bin" if prefix == "fault" else "stop-after.bin"]
                ):
                    raise ManagementError("Raw full postimage differs from receipt or saved bytes")

    def save(self, output: Path) -> None:
        folder = output / "reviewed-read-fault-evidence"
        folder.mkdir()
        durable_bytes(folder / "manifest.json", self.manifest)
        for name, data in self.files.items():
            durable_bytes(folder / name, data)


class Control(evidence.DiagnosticControl):
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
                raise ManagementError("Unexpected V11 identity, CPU or GPIO pin configuration")
        net = self.exchange("NETINFO", "netinfo")
        if (
            net.get("firmware") != FIRMWARE
            or net.get("app_elf_sha256") != self.elf_pin
            or net.get("armed") is not False
            or net.get("update_busy") is not False
        ):
            raise ManagementError("V11 firmware ELF or idle control state differs from pin")
        current = state(self.exchange("EMU_STATUS", "emu_status"))
        if (
            info.get("pins_input_only") != current["pins_input_only"]
            or info.get("internal_pulls") != current["internal_pulls"]
        ):
            raise ManagementError("V11 INFO and emulator pin state disagree")
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
            raise ManagementError("V11 full preimage differs from the exact valid durable pin")
        return frame, data

    def send_once(self, command: str, payload: bytes | None = None) -> None:
        self.command_may_have_reached = True
        self.session.command(command, payload)

    def result(self, op: str) -> dict:
        frame = state(self.reader.receive(self.session.clock() + 10))
        if frame.get("type") != "emu_result" or frame.get("op") != op:
            raise ManagementError("Unexpected V11 operation result")
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
                raise ManagementError("Unexpected V11 detached diagnostic phase")
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
                raise ManagementError("V11 diagnostic result exceeds reviewed bounds")
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
        if software and (powered is not True or not before["pullup_control"]["off_verified"]):
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
                raise ManagementError("START pre-output phase has an unexpected session state")
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

    def recover(self, before: dict, proof: ReadProof | None, generation: int) -> dict:
        passive(before)
        require_off(before)
        if (
            proof is None
            or digest(proof.manifest) != RECOVERY_MANIFEST
            or type(generation) is not int
            or generation != 1
            or self.image_pin != RECOVERY_SEED
            or not before["loaded"]
            or before["dirty"]
            or before["session_active_marker"]
            or before["last_fault_code"] != 2
            or before["last_fault"] != "tx_byte_count_unqualified"
            or before["live_generation"] != 1
            or before["durable_generation"] != 1
            or before["image_sha256"] != RECOVERY_SEED
            or before["seed_sha256"] != RECOVERY_SEED
        ):
            raise ManagementError("Recovery requires the exact unchanged closed fault2 checkpoint")
        ReadProof.qualify(proof.files)
        proof.save(self.output)
        durable_json(
            self.output / "recovery-plan-before-command.json",
            {
                "policy": "V10_READ_UNCHANGED",
                "manifest_sha256": RECOVERY_MANIFEST,
                "image_sha256_before_and_after": RECOVERY_SEED,
                "generation_before_and_after": 1,
                "pointer_byte_value_known": False,
                "counter_or_image_reset": False,
                "before": before,
            },
        )
        self.send_once(
            "EMU_RECOVER_READ",
            f"EMU_RECOVER_READ V10_READ_UNCHANGED {self.image_pin} 1\n".encode("ascii"),
        )
        result = self.result("recover_read")
        self.result_frame = result
        if result.get("ok") is True:
            passive(result)
            require_off(result)
            if (
                result.get("message") != "unchanged_read_fault_metadata_cleared"
                or result.get("prior_fault_code") != 2
                or result.get("recovery_policy") != "V10_READ_UNCHANGED"
                or result["last_fault_code"] != 0
                or result["last_fault"] != "none"
                or result["unclean_boot"]
                or result["session_active_marker"]
            ):
                raise ManagementError("Read recovery policy or metadata clear is unverified")
            for name in evidence.STABLE_FIELDS:
                if (
                    name not in {"last_fault", "last_fault_code", "unclean_boot"}
                    and before[name] != result[name]
                ):
                    raise ManagementError("Read recovery changed checkpoint metadata")
        return result

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

    def run_action(
        self,
        action: str,
        *,
        generation: int = 1,
        sole: bool = False,
        proof: ReadProof | None = None,
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
                            "before-stop-volatile" if action == "stop" else "before-pullup-off"
                        )
                    except Exception as error:  # noqa: BLE001 - Optional export must not block release.
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
                    or result["session_active_marker"] != current["session_active_marker"]
                ):
                    raise ManagementError("Release improperly cleared prior unclean evidence")
                if result["loaded"]:
                    after, postimage = self.save_snapshot(
                        "after-stop-checkpoint" if action == "stop" else "after-pullup-off"
                    )
                    if (
                        after["image_sha256"] != result["image_sha256"]
                        or after["image_generation"] != result["durable_generation"]
                    ):
                        raise ManagementError("Release snapshot differs from checkpoint")
                    if (
                        action == "pullup-off"
                        and before is not None
                        and bytes.fromhex(before["hex"]) != postimage
                    ):
                        raise ManagementError("D19 OFF changed image bytes")
                status = "stopped_checkpointed" if action == "stop" else "pullup_off_verified"
            elif action == "status":
                result = current
                if current["loaded"]:
                    after, _ = self.save_snapshot("status-snapshot")
                status = "status_and_snapshot_saved"
            elif action in {"diag", "recover", "start", "start-pullup", "pullup-on"}:
                before, preimage = self.save_snapshot(f"before-{action}", pinned=True)
                passive(before)
                self.unchanged(current, before)
                if action == "diag":
                    require_off(before)
                    result, _ = self.diag(before)
                    after, postimage = self.save_snapshot("after-diag", pinned=True)
                    require_off(after)
                    self.unchanged(before, after)
                    self.unchanged(after, self.probe())
                    status = "detached_diagnostic_verified"
                elif action == "recover":
                    result = self.recover(before, proof, generation)
                    after, postimage = self.save_snapshot("after-recover", pinned=True)
                    self.unchanged(result, after)
                    require_off(after)
                    self.unchanged(after, self.probe())
                    status = "read_fault_metadata_recovered"
                elif action == "pullup-on":
                    result = self.set_pullup(before, True, powered)
                    after, postimage = self.save_snapshot("after-pullup-on", pinned=True)
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
                raise ManagementError("Unknown explicit V11 action")
            if result.get("ok", True) is not True or self.reader.chatter != chatter:
                raise ManagementError("V11 operation failed or raw boot/panic chatter appeared")
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
                "counter_reset_or_crc_repair": False,
                "transport_qualified": False,
                "pointer_byte_value_known": False,
                "earlier_V7_volatile_history_remains_unknown": True,
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
                except Exception as tail:  # noqa: BLE001 - Preserve the original failure.
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
            "recover",
            "pullup-on",
            "pullup-off",
            "start",
            "start-pullup",
            "stop",
        ),
    )
    parser.add_argument("--port", required=True)
    parser.add_argument("--serial-number", required=True)
    parser.add_argument("--expect-elf-sha256", required=True)
    parser.add_argument("--image-sha256", required=True)
    parser.add_argument("--durable-generation", type=int, default=1)
    parser.add_argument("--sole-0x50", action="store_true")
    parser.add_argument("--printer-powered", action="store_true")
    parser.add_argument("--recovery-evidence", type=Path)
    parser.add_argument("--recovery-manifest-sha256")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    require_hash(args.expect_elf_sha256)
    require_hash(args.image_sha256)
    if args.action in {"start", "start-pullup"} and not args.sole_0x50:
        parser.error("START requires --sole-0x50")
    if args.action in {"start-pullup", "pullup-on"} and not args.printer_powered:
        parser.error("D19 HIGH requires --printer-powered")
    proof = None
    if args.action == "recover":
        if not args.recovery_evidence or not args.recovery_manifest_sha256:
            parser.error("Recovery requires exact read-fault evidence")
        proof = ReadProof.load(args.recovery_evidence, args.recovery_manifest_sha256)
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
                powered=args.printer_powered,
            )
        finally:
            uart.close()
    print(json.dumps({"status": receipt["status"], "result": receipt["result"]}))


if __name__ == "__main__":
    try:
        main()
    except (ManagementError, OSError, ValueError, ImportError):
        print(
            "V11 action unverified; inspect raw evidence before another command.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
