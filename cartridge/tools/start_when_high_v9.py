"""Prevalidate a V9 START, then wait boundedly for observed SDA LOW to idle HIGH.

Template: frozen manage_emulator_v9.Control. Its full preimage, identity and
sole-address guards run before this wrapper's send_once interception. Only INFO
is polled while waiting; one START is sent with the UART already open. There is
no firmware change, automatic retry, reset or implicit STOP.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import manage_emulator_v9 as v9
from bench_eeprom import DurableAudit, durable_json, require_hash
from manage_esp32 import ManagementError, Session, validate_device


class Control(v9.Control):
    def __init__(
        self, *args, wait_ms: int = 45000, sleep=time.sleep, ready=None, **kwargs
    ):
        super().__init__(*args, **kwargs)
        if type(wait_ms) is not int or not 1000 <= wait_ms <= 45000:
            raise ManagementError("Wait must be1000..45000ms")
        self.wait_ms, self.sleep = wait_ms, sleep
        self.ready = ready or (lambda value: print(json.dumps(value), flush=True))
        self.wait_result = None

    def send_once(self, command: str, payload: bytes | None = None) -> None:
        if command != "EMU_START" or self.wait_result is not None:
            raise ManagementError("The bounded wait wrapper permits one START only")
        # Frozen parent START has already checked the exact clean full preimage
        # and durably saved its ownership intent before calling this method.
        started = self.session.clock()
        deadline = started + self.wait_ms / 1000
        self.wait_result = {
            "type": "waiting_idle_high",
            "wait_ms": self.wait_ms,
            "require_observed_levels1_before_levels3": True,
            "started_monotonic": started,
            "low_observed_monotonic": None,
            "high_observed_monotonic": None,
            "start_send_begin_monotonic": None,
            "start_send_complete_monotonic": None,
            "polls": 0,
            "preimage_and_sole_address_intent_saved": True,
            "physical_printer_open_feedback_verified_by_wrapper": False,
        }
        durable_json(self.output / "wait-ready.json", self.wait_result)
        self.ready(dict(self.wait_result))
        while self.session.clock() < deadline:
            chatter = self.reader.chatter
            self.session.command("INFO")
            frame = self.reader.receive(min(deadline, self.session.clock() + 1))
            self.wait_result["polls"] += 1
            if self.session.clock() >= deadline:
                raise ManagementError("INFO completed after the bounded wait expired")
            for name, value in (
                ("type", "status"),
                ("firmware", v9.FIRMWARE),
                ("emulator_capability", v9.CAPABILITY),
                ("cpu_mhz", 240),
                ("sda_gpio", 21),
                ("scl_gpio", 22),
                ("armed", False),
                ("pins_input_only", True),
                ("internal_pulls", False),
                ("update_busy", False),
                ("recovery_required", False),
            ):
                if type(frame.get(name)) is not type(value) or frame[name] != value:
                    raise ManagementError(
                        "Wait INFO changed identity or passive control state"
                    )
            if self.reader.chatter != chatter:
                raise ManagementError("Raw boot/panic chatter appeared while waiting")
            levels = v9.v7.integer(frame.get("levels"), "levels")
            if levels > 3:
                raise ManagementError("Wait INFO has invalid GPIO levels")
            if levels == 1 and self.wait_result["low_observed_monotonic"] is None:
                self.wait_result["low_observed_monotonic"] = self.session.clock()
            if levels == 3 and self.wait_result["low_observed_monotonic"] is not None:
                self.wait_result["high_observed_monotonic"] = self.session.clock()
                self.wait_result["start_send_begin_monotonic"] = self.session.clock()
                super().send_once(command, payload)
                self.wait_result["start_send_complete_monotonic"] = self.session.clock()
                self.wait_result["high_to_start_send_begin_ms"] = 1000 * (
                    self.wait_result["start_send_begin_monotonic"]
                    - self.wait_result["high_observed_monotonic"]
                )
                self.wait_result["high_to_start_send_complete_ms"] = 1000 * (
                    self.wait_result["start_send_complete_monotonic"]
                    - self.wait_result["high_observed_monotonic"]
                )
                self.wait_result["timing_scope"] = (
                    "Host monotonic bounds around UART write, not measured wire edges or EEPROM ACK latency"
                )
                return
            remaining = deadline - self.session.clock()
            if remaining > 0:
                self.sleep(min(0.25, remaining))
        raise ManagementError(
            "No observed LOW-to-idle-HIGH transition within finite wait"
        )

    def run_start(self, generation: int, sole: bool) -> dict:
        try:
            return super().run_action("start", generation=generation, sole=sole)
        finally:
            if self.wait_result is not None:
                durable_json(self.output / "wait-summary.json", self.wait_result)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True)
    parser.add_argument("--serial-number", required=True)
    parser.add_argument("--expect-elf-sha256", required=True)
    parser.add_argument("--image-sha256", required=True)
    parser.add_argument("--durable-generation", required=True, type=int)
    parser.add_argument("--sole-0x50", required=True, action="store_true")
    parser.add_argument("--wait-idle-ms", type=int, default=45000)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    require_hash(args.expect_elf_sha256)
    require_hash(args.image_sha256)
    if not 1000 <= args.wait_idle_ms <= 45000:
        parser.error("--wait-idle-ms must be1000..45000")
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
            control = Control(
                Session(uart, audit, expected_firmware=v9.FIRMWARE),
                args.output,
                args.expect_elf_sha256,
                args.image_sha256,
                wait_ms=args.wait_idle_ms,
            )
            receipt = control.run_start(args.durable_generation, args.sole_0x50)
        finally:
            uart.close()
    print(json.dumps({"status": receipt["status"], "wait": control.wait_result}))


if __name__ == "__main__":
    try:
        main()
    except (ManagementError, OSError, ValueError, ImportError):
        print(
            "Bounded START unverified; inspect saved raw evidence before another command.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
