"""Audited, detached V8 I2C diagnostics; imports never access hardware.

Templates: frozen manage_esp32.py UART/device guards, bench_eeprom.py durable
evidence, and manage_emulator_v7.py pure state/snapshot validators. This helper
issues only INFO, NETINFO, EMU_STATUS, EMU_SNAPSHOT and one EMU_DIAG DETACHED.
It never starts emulation, recovers state, arms capture or retries a command.
Reset reason describes the current V8 boot, not the earlier V7 timeout. A valid
boot breadcrumb is retained V8 evidence; the first V8 boot may have no valid one.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import manage_emulator_v7 as v7
from bench_eeprom import DurableAudit, digest, durable_bytes, durable_json, require_hash
from manage_esp32 import ManagementError, Session, validate_device

FIRMWARE = "i2c-eeprom-emulator-v8"
CAPABILITY = "eeprom_256_experimental_v1"
SCHEMA = "sunburn.brother.detached-diagnostic.v1"
PHASES = ("reset", "fifo_loaded", "pre_enable")
REGISTER_FIELDS = (
    "raw_interrupts",
    "enabled_interrupts",
    "masked_interrupts",
    "hw_status",
    "ctr",
    "fifo_conf",
    "rx_fifo",
    "tx_fifo",
    "levels",
    "gpio_output_enable",
    "sda_output_select",
    "scl_output_select",
)
STABLE_FIELDS = (
    "firmware",
    "capability",
    "loaded",
    "address",
    "image_sha256",
    "seed_sha256",
    "live_generation",
    "durable_generation",
    "dirty",
    "unclean_boot",
    "session_active_marker",
    "last_fault",
    "last_fault_code",
    "reset_reason",
    "boot_breadcrumb",
)
MAX_LINE_BYTES = 8192
MAX_TOTAL_BYTES = 131072
MAX_LINES = 256


def state(frame: dict[str, object]) -> dict[str, object]:
    if frame.get("firmware") != FIRMWARE:
        raise ManagementError("Unexpected V8 firmware identity")
    # Reuse only the frozen, pure common-field validator after the exact V8
    # identity gate. Neither the original frame nor v7.FIRMWARE is mutated.
    v7.state({**frame, "firmware": v7.FIRMWARE})
    if frame["emulation_active"] or frame["session_open"]:
        raise ManagementError("Detached diagnostics require a closed passive session")
    if frame["pins_input_only"] is not True or frame["internal_pulls"] is not False:
        raise ManagementError("Detached diagnostic pins are not passive")
    v7.integer(frame.get("reset_reason"), "reset_reason")
    breadcrumb = frame.get("boot_breadcrumb")
    if not isinstance(breadcrumb, dict) or type(breadcrumb.get("valid")) is not bool:
        raise ManagementError("Reset breadcrumb is missing or malformed")
    stage = v7.integer(breadcrumb.get("stage"), "breadcrumb.stage")
    inverse = v7.integer(breadcrumb.get("stage_inverse"), "breadcrumb.stage_inverse")
    if breadcrumb["valid"] and stage ^ inverse != 0xFFFFFFFF:
        raise ManagementError("Valid reset breadcrumb has a damaged inverse")
    diagnostics = frame.get("isr_diagnostics")
    if not isinstance(diagnostics, dict):
        raise ManagementError("V8 ISR diagnostics are missing")
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


def registers(frame: dict[str, object]) -> None:
    for name in REGISTER_FIELDS:
        v7.integer(frame.get(name), name)
    if frame["rx_fifo"] > 32 or frame["tx_fifo"] > 32 or frame["levels"] > 3:
        raise ManagementError(
            "Diagnostic FIFO/line observation exceeded hardware bounds"
        )
    if (
        frame["gpio_output_enable"] & ((1 << 21) | (1 << 22))
        or frame["sda_output_select"] != 1280
        or frame["scl_output_select"] != 1280
    ):
        raise ManagementError(
            "Diagnostic GPIO output-enable/matrix release is unverified"
        )


class EvidenceReader:
    """Bounded ASCII framing that retains exact raw lines before parsing them."""

    def __init__(self, session: Session, output: Path) -> None:
        self.session, self.output = session, output
        self.lines = self.frames = self.chatter = self.total_bytes = 0
        self.last_frame: dict[str, object] | None = None

    def retain(self, data: bytes, partial: bool = False) -> None:
        suffix = "-partial" if partial else ""
        name = f"raw-line-{self.lines:03d}{suffix}.bin"
        durable_bytes(self.output / name, data)
        self.lines += 1
        self.session.audit.record(
            "uart_raw_line",
            file=name,
            bytes=len(data),
            sha256=digest(data),
            partial=partial,
        )
        if self.lines > MAX_LINES:
            raise ManagementError("Diagnostic UART exceeded its finite line bound")

    def parse_line(
        self, raw: bytes, *, startup: bool = False
    ) -> dict[str, object] | None:
        self.retain(raw)
        if not raw.lstrip().startswith((b"{", b"[")):
            self.chatter += 1
            self.session.audit.record(
                "uart_raw_chatter", line=self.lines - 1, startup=startup
            )
            return None
        try:
            frame = json.loads(raw)
        except (ValueError, UnicodeError) as error:
            if startup:
                self.chatter += 1
                self.session.audit.record(
                    "uart_startup_damaged_json", line=self.lines - 1
                )
                return None
            raise ManagementError(
                "Malformed diagnostic JSON; raw bytes retained"
            ) from error
        if not isinstance(frame, dict):
            if startup:
                self.chatter += 1
                return None
            raise ManagementError("Diagnostic JSON must be one object")
        durable_json(self.output / f"frame-{self.frames:03d}.json", frame)
        self.session.audit.record("uart_saved_json", frame=self.frames, startup=startup)
        self.frames += 1
        self.last_frame = frame
        return frame

    def drain_startup(self) -> dict[str, object]:
        """Keep buffered startup output, then require a bounded quiet interval."""
        started = self.session.clock()
        hard_deadline, quiet_deadline = started + 2, started + 0.3
        line = bytearray()
        initial_lines, initial_frames = self.lines, self.frames
        while True:
            now = self.session.clock()
            if now >= hard_deadline:
                if line:
                    self.retain(bytes(line), partial=True)
                raise ManagementError(
                    "UART startup did not become quiet within the bounded drain"
                )
            if now >= quiet_deadline:
                partial = bool(line)
                if line:
                    self.retain(bytes(line), partial=True)
                    self.chatter += 1
                    self.session.audit.record(
                        "uart_startup_partial", line=self.lines - 1
                    )
                summary = {
                    "raw_lines_saved": self.lines - initial_lines,
                    "json_frames_saved": self.frames - initial_frames,
                    "partial_fragment_retained": partial,
                    "quiet_interval_ms": 300,
                    "hard_limit_ms": 2000,
                }
                durable_json(self.output / "startup-drain.json", summary)
                return summary
            value = self.session.uart.read(1)
            if not value:
                continue
            line.extend(value)
            self.total_bytes += len(value)
            quiet_deadline = self.session.clock() + 0.3
            if len(line) > MAX_LINE_BYTES or self.total_bytes > MAX_TOTAL_BYTES:
                self.retain(bytes(line), partial=True)
                raise ManagementError("UART startup exceeded its finite evidence bound")
            if value == b"\n":
                self.parse_line(bytes(line), startup=True)
                line.clear()

    def receive(self, deadline: float) -> dict[str, object]:
        line = bytearray()
        while self.session.clock() < deadline:
            value = self.session.uart.read(1)
            if not value:
                continue
            line.extend(value)
            self.total_bytes += len(value)
            if len(line) > MAX_LINE_BYTES or self.total_bytes > MAX_TOTAL_BYTES:
                self.retain(bytes(line), partial=True)
                raise ManagementError(
                    "Diagnostic UART exceeded its finite evidence bound"
                )
            if value != b"\n":
                continue
            raw = bytes(line)
            line.clear()
            frame = self.parse_line(raw)
            if frame is not None:
                return frame
        if line:
            self.retain(bytes(line), partial=True)
        raise ManagementError("Diagnostic UART timed out; no automatic retry")


class DiagnosticControl:
    def __init__(
        self, session: Session, output: Path, elf_pin: str, image_pin: str
    ) -> None:
        self.session, self.output = session, output
        self.elf_pin, self.image_pin = require_hash(elf_pin), require_hash(image_pin)
        self.reader = EvidenceReader(session, output)
        self.command_may_have_reached = False

    def exchange(self, command: str, kind: str) -> dict[str, object]:
        chatter_before = self.reader.chatter
        self.session.command(command)
        frame = self.reader.receive(self.session.clock() + 5)
        if self.reader.chatter != chatter_before:
            raise ManagementError(
                "Raw boot/chatter appeared after the startup quiet interval"
            )
        if frame.get("type") != kind or frame.get("ok", True) is not True:
            raise ManagementError("Unexpected or rejected diagnostic probe response")
        return frame

    def probe(self) -> dict[str, object]:
        info = self.exchange("INFO", "status")
        for name, value in (
            ("firmware", FIRMWARE),
            ("emulator_capability", CAPABILITY),
            ("cpu_mhz", 240),
            ("sda_gpio", 21),
            ("scl_gpio", 22),
            ("armed", False),
            ("pins_input_only", True),
            ("internal_pulls", False),
        ):
            if type(info.get(name)) is not type(value) or info[name] != value:
                raise ManagementError(
                    "Unexpected V8 identity, CPU or passive GPIO state"
                )
        network = self.exchange("NETINFO", "netinfo")
        if (
            network.get("firmware") != FIRMWARE
            or network.get("app_elf_sha256") != self.elf_pin
            or network.get("armed") is not False
            or network.get("update_busy") is not False
        ):
            raise ManagementError(
                "Diagnostic firmware ELF or idle state differs from the pin"
            )
        current = state(self.exchange("EMU_STATUS", "emu_status"))
        if not current["loaded"] or current["dirty"]:
            raise ManagementError(
                "Diagnostic evidence requires the loaded durable image"
            )
        return current

    def save_snapshot(self, label: str) -> tuple[dict[str, object], bytes]:
        frame = self.exchange("EMU_SNAPSHOT", "emu_snapshot")
        state(frame)
        data, inspection = v7.snapshot({**frame, "firmware": v7.FIRMWARE})
        durable_bytes(self.output / f"{label}.bin", data)
        durable_json(self.output / f"{label}-inspection.json", inspection)
        if (
            digest(data) != self.image_pin
            or not frame["snapshot_is_current"]
            or frame["image_generation"] != frame["durable_generation"]
        ):
            raise ManagementError(
                "Full diagnostic snapshot differs from the exact durable pin"
            )
        return frame, data

    @staticmethod
    def unchanged(before: dict[str, object], after: dict[str, object]) -> None:
        if any(before[name] != after[name] for name in STABLE_FIELDS):
            raise ManagementError(
                "Detached diagnostics changed saved image/session/reset metadata"
            )

    def run(self) -> dict[str, object]:
        before = result = after = None
        phases: list[dict[str, object]] = []
        startup = None
        try:
            startup = self.reader.drain_startup()
            current = self.probe()
            durable_json(
                self.output / "intent-before-command.json",
                {
                    "schema": SCHEMA,
                    "firmware": FIRMWARE,
                    "app_elf_sha256": self.elf_pin,
                    "image_sha256": self.image_pin,
                    "before": current,
                    "command": "EMU_DIAG DETACHED",
                    "automatic_retry": False,
                    "start_or_recovery_or_capture_requested": False,
                },
            )
            before, preimage = self.save_snapshot("before-diag")
            self.unchanged(current, before)
            chatter_before = self.reader.chatter
            self.command_may_have_reached = True
            self.session.command("EMU_DIAG", b"EMU_DIAG DETACHED\n")
            deadline = self.session.clock() + 10
            while True:
                frame = self.reader.receive(deadline)
                if frame.get("type") == "emu_diag":
                    result = frame
                    break
                if frame.get("type") != "emu_diag_phase" or frame.get("op") != "diag":
                    raise ManagementError("Unexpected detached diagnostic stream frame")
                if len(phases) >= 3 or frame.get("phase") != PHASES[len(phases)]:
                    raise ManagementError(
                        "Diagnostic phases are missing, duplicated or out of order"
                    )
                registers(frame)
                if frame["enabled_interrupts"] != 0:
                    raise ManagementError(
                        "Diagnostic phase unexpectedly has interrupts enabled"
                    )
                phases.append(frame)
            state(result)
            self.unchanged(before, result)
            if result.get("ok") is True:
                registers(result)
                if (
                    tuple(frame["phase"] for frame in phases) != PHASES
                    or result.get("message") != "detached_diagnostic_complete"
                    or type(result.get("outputs_enabled")) is not bool
                    or result["outputs_enabled"]
                    or type(result.get("observation_ms")) is not int
                    or result["observation_ms"] != 5
                    or type(result.get("one_shot_irq_count")) is not int
                    or result["one_shot_irq_count"] not in (0, 1)
                    or result["enabled_interrupts"] != 0
                ):
                    raise ManagementError(
                        "Detached diagnostic result exceeds the reviewed scope"
                    )
                v7.integer(result.get("observed_irq"), "observed_irq")
                v7.integer(result.get("observed_status"), "observed_status")
            after, postimage = self.save_snapshot("after-diag")
            self.unchanged(before, after)
            if preimage != postimage:
                raise ManagementError(
                    "Detached diagnostic changed a complete EEPROM image byte"
                )
            final = self.probe()
            self.unchanged(before, final)
            if result.get("ok") is not True or self.reader.chatter != chatter_before:
                raise ManagementError(
                    "Diagnostic failed or raw boot/chatter appeared during operation"
                )
            receipt = {
                "schema": SCHEMA,
                "status": "detached_diagnostic_verified",
                "firmware": FIRMWARE,
                "app_elf_sha256": self.elf_pin,
                "image_sha256": self.image_pin,
                "before": before,
                "phases": phases,
                "result": result,
                "after": after,
                "post_probe": final,
                "image_bytes_unchanged": True,
                "saved_metadata_unchanged": True,
                "automatic_retry": False,
                "command_may_have_reached_device": True,
                "transport_qualified": False,
                "printer_activity_proven": False,
                "reset_reason_scope": "current V8 boot only; does not identify the prior V7 reset cause",
                "boot_breadcrumb_scope": "retained previous V8 stage only when valid; may be unavailable after first V8 installation",
                "raw_lines_saved": self.reader.lines,
                "raw_chatter_lines_saved": self.reader.chatter,
                "startup_capture": startup,
            }
            durable_json(self.output / "receipt.json", receipt)
            return receipt
        except Exception as error:
            durable_json(
                self.output / "failure.json",
                {
                    "schema": SCHEMA,
                    "status": "unverified",
                    "firmware": FIRMWARE,
                    "app_elf_sha256": self.elf_pin,
                    "image_sha256": self.image_pin,
                    "command_may_have_reached_device": self.command_may_have_reached,
                    "before": before,
                    "phases": phases,
                    "result": result,
                    "after": after,
                    "last_device_frame": self.reader.last_frame,
                    "error_type": type(error).__name__,
                    "error": str(error),
                    "raw_lines_saved": self.reader.lines,
                    "raw_chatter_lines_saved": self.reader.chatter,
                    "startup_capture": startup,
                    "automatic_retry": False,
                },
            )
            raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True)
    parser.add_argument("--serial-number", required=True)
    parser.add_argument("--expect-elf-sha256", required=True)
    parser.add_argument("--image-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    require_hash(args.expect_elf_sha256)
    require_hash(args.image_sha256)
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
            # Retain boot/panic bytes already buffered; never reset_input_buffer.
            receipt = DiagnosticControl(
                Session(uart, audit, expected_firmware=FIRMWARE),
                args.output,
                args.expect_elf_sha256,
                args.image_sha256,
            ).run()
        finally:
            uart.close()
    print(json.dumps({"status": receipt["status"], "result": receipt["result"]}))


if __name__ == "__main__":
    try:
        main()
    except (ManagementError, OSError, ValueError, ImportError):
        print(
            "Detached diagnostic unverified; inspect saved raw evidence before any retry.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
