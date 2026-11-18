"""Operate only the named ESP32 sniffer UART; never send cartridge bus commands."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

MAX_JSON_HEADER_BYTES = 128 * 1024


class JSONUART(Protocol):
    def readline(self, size: int = -1) -> bytes: ...


def receive_json(
    uart: JSONUART,
    kind: str,
    record: Callable[[str, object], None],
    timeout: float = 5,
    clock: Callable[[], float] = time.monotonic,
    max_header_bytes: int = MAX_JSON_HEADER_BYTES,
) -> dict[str, object]:
    """Accumulate timeout fragments without reading beyond the line delimiter."""
    if timeout <= 0 or max_header_bytes < 1:
        raise ValueError("UART JSON framing bounds must be positive")
    deadline = clock() + timeout
    pending = bytearray()
    while clock() < deadline:
        # pyserial readline(size) stops at LF, the bound, or its serial timeout.
        # A timeout may return a partial line; retain it for the next call.
        remaining = max_header_bytes - len(pending)
        chunk = uart.readline(remaining + 1)
        if not chunk:
            continue
        if len(chunk) > remaining:
            raise RuntimeError("UART JSON header exceeded its finite size bound")
        pending.extend(chunk)
        if not pending.endswith(b"\n"):
            continue
        text = pending.decode("utf-8", errors="replace").strip()
        pending.clear()
        record("rx", text)
        try:
            result = json.loads(text)
        except json.JSONDecodeError as error:
            if text.startswith(("{", "[")):
                raise RuntimeError(
                    "Malformed UART JSON header; binary framing is unknown"
                ) from error
            continue
        if not isinstance(result, dict):
            raise RuntimeError("UART response must be a JSON object")
        if result.get("type") == "error":
            raise RuntimeError(str(result))
        if result.get("type") == kind:
            return result
    record("rx_timeout", {"pending_header_bytes": len(pending)})
    raise TimeoutError("No complete expected UART response: " + kind)


def capture_byte_count(metadata: dict[str, object]) -> int:
    size, count = metadata.get("bytes"), metadata.get("event_count")
    if (
        metadata.get("schema") != "sunburn.i2c.raw-edges.v1"
        or metadata.get("record_format") != "<III"
        or type(size) is not int
        or type(count) is not int
        or not 0 <= size <= 16384 * 12
        or size != count * 12
    ):
        raise RuntimeError("Unexpected capture binary framing")
    return size


def validate_capture_binary(binary: bytes, metadata: dict[str, object]) -> None:
    if len(binary) != capture_byte_count(metadata):
        raise RuntimeError("Partial raw capture retained; do not infer memory")
    if f"{zlib.crc32(binary):08x}" != metadata.get("crc32"):
        raise RuntimeError("Raw capture CRC mismatch; do not infer memory")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True)
    parser.add_argument("--serial-number", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--duration", type=float, default=30)
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--expect-firmware", default="i2c-passive-poll-v4")
    args = parser.parse_args()
    if not 0.1 <= args.duration <= 120:
        parser.error("duration must be0.1..120seconds")
    import serial
    from serial.tools import list_ports

    from manage_esp32 import validate_device
    device = validate_device(args.port, list(list_ports.comports()), args.serial_number)
    args.output.mkdir(parents=True, exist_ok=False)
    log = (args.output / "serial-control.jsonl").open("x", encoding="utf-8")
    uart = serial.Serial(port=None, baudrate=921600, timeout=0.5, write_timeout=5)
    uart.dtr = False
    uart.rts = False
    uart.port = args.port

    def record(direction: str, value: object) -> None:
        log.write(
            json.dumps({"at_unix": time.time(), "direction": direction, "value": value}) + "\n"
        )
        log.flush()

    def send(command: str) -> None:
        record("tx", command)
        uart.write((command + "\n").encode("ascii"))
        uart.flush()

    def receive(kind: str, timeout: float = 5) -> dict[str, object]:
        return receive_json(uart, kind, record, timeout)

    try:
        uart.open()
        # Opening USB UART may reset some boards; discard only boot chatter,
        # never raw capture bytes. This happens before any ARM command.
        time.sleep(1.5)
        uart.reset_input_buffer()
        send("INFO")
        info = receive("status")
        if (
            info.get("firmware") != args.expect_firmware
            or info.get("pins_input_only") is not True
            or info.get("internal_pulls") is not False
            or info.get("cpu_mhz") != 240
        ):
            raise RuntimeError("Unexpected firmware or passive capture configuration")
        (args.output / "probe.json").write_text(json.dumps(info, indent=2) + "\n")
        if args.probe:
            print(json.dumps({"status": "PROBE_PASS", "info": info}), flush=True)
            return
        send("ARM " + str(round(args.duration * 1000)))
        armed = receive("status")
        if armed.get("armed") is not True or armed.get("overflow") != 0:
            raise RuntimeError("Sniffer did not arm cleanly")
        (args.output / "armed.json").write_text(json.dumps(armed, indent=2) + "\n")
        print(
            json.dumps(
                {
                    "status": "ARMED",
                    "seconds": args.duration,
                    "output": str(args.output),
                    "levels": armed.get("levels"),
                }
            ),
            flush=True,
        )
        time.sleep(args.duration + 0.1)
        send("STOP")
        stopped = receive("status")
        if stopped.get("armed") is not False:
            raise RuntimeError("Sniffer did not stop")
        send("DUMP")
        metadata = receive("capture", timeout=10)
        size = capture_byte_count(metadata)
        raw = bytearray()
        deadline = time.monotonic() + 15
        while len(raw) < size and time.monotonic() < deadline:
            raw.extend(uart.read(size - len(raw)))
        binary = bytes(raw)
        with (args.output / "edges.bin").open("xb") as stream:
            stream.write(binary)
        validate_capture_binary(binary, metadata)
        receive("dump_end", timeout=5)
        metadata.update(
            serial_device={
                "device": device.device,
                "vid": device.vid,
                "pid": device.pid,
                "serial_number": device.serial_number,
            },
            binary_sha256=hashlib.sha256(binary).hexdigest(),
            serial_crc_verified=True,
            uart_control_only=True,
        )
        with (args.output / "metadata.json").open("x") as stream:
            stream.write(json.dumps(metadata, indent=2) + "\n")
        print(
            json.dumps(
                {
                    "status": "CAPTURE_SAVED",
                    "events": metadata["event_count"],
                    "overflow": metadata["overflow"],
                    "sha256": metadata["binary_sha256"],
                    "lossless_claim": False,
                }
            ),
            flush=True,
        )
    finally:
        uart.close()
        log.close()


if __name__ == "__main__":
    main()
