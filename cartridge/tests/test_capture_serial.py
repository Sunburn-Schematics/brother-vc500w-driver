from __future__ import annotations

import io
import json
import unittest
import zlib
from collections import deque

from capture_serial import capture_byte_count, receive_json, validate_capture_binary


class FragmentedUART:
    def __init__(self, data: bytes, fragments: list[int] | None = None) -> None:
        self.stream = io.BytesIO(data)
        self.fragments = deque(fragments or [])
        self.read_limits = []

    def readline(self, size: int = -1) -> bytes:
        self.read_limits.append(size)
        if self.fragments:
            fragment = self.fragments.popleft()
            if fragment == 0:
                return b""
            size = min(size, fragment)
        return self.stream.readline(size)

    def read(self, size: int) -> bytes:
        return self.stream.read(size)


class Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        self.value += 0.01
        return self.value


def encoded(value: object) -> bytes:
    return json.dumps(value).encode("utf-8") + b"\n"


def read_response(uart: FragmentedUART, kind: str, **options: object) -> dict[str, object]:
    return receive_json(uart, kind, lambda direction, value: None, clock=Clock(), **options)


def metadata(binary: bytes) -> dict[str, object]:
    return {
        "type": "capture",
        "schema": "sunburn.i2c.raw-edges.v1",
        "record_format": "<III",
        "event_count": len(binary) // 12,
        "bytes": len(binary),
        "crc32": f"{zlib.crc32(binary):08x}",
    }


class CaptureSerialTests(unittest.TestCase):
    def test_json_header_fragments_survive_multiple_serial_timeouts(self) -> None:
        expected = {
            "type": "capture",
            "sampling_windows": [{"first_event": 0, "event_count": 3}],
            "description": "café",
        }
        uart = FragmentedUART(encoded(expected), [3, 0, 7, 0, 11, 0, 2, 0])
        self.assertEqual(read_response(uart, "capture"), expected)

    def test_large_window_header_is_accumulated_with_finite_bound(self) -> None:
        expected = {"type": "capture", "padding": "w" * 70000}
        uart = FragmentedUART(encoded(expected), [2000, 0] * 40)
        self.assertEqual(read_response(uart, "capture"), expected)
        self.assertTrue(all(0 < limit <= 128 * 1024 + 1 for limit in uart.read_limits))

    def test_binary_starts_at_exact_byte_after_header_newline(self) -> None:
        binary = bytes(range(256)) * 3
        header = metadata(binary)
        uart = FragmentedUART(
            encoded(header) + binary + b"\n" + encoded({"type": "dump_end"}), [2, 0, 17, 0, 4]
        )
        captured = read_response(uart, "capture")
        self.assertEqual(captured, header)
        raw = uart.read(capture_byte_count(captured))
        self.assertEqual(raw, binary)
        validate_capture_binary(raw, captured)
        self.assertEqual(read_response(uart, "dump_end"), {"type": "dump_end"})
        self.assertEqual(uart.read(1), b"")

    def test_binary_leading_newlines_and_nul_are_not_consumed_by_header(self) -> None:
        binary = b"\n\n\x00\xff" * 3
        uart = FragmentedUART(encoded(metadata(binary)) + binary)
        read_response(uart, "capture")
        self.assertEqual(uart.read(len(binary)), binary)

    def test_partial_header_timeout_never_returns_or_reads_binary(self) -> None:
        uart = FragmentedUART(b'{"type":"capture"')
        with self.assertRaises(TimeoutError):
            read_response(uart, "capture", timeout=0.1)

    def test_oversized_header_is_rejected_before_complete_line(self) -> None:
        uart = FragmentedUART(b'{"type":"capture","padding":"' + b"a" * 100)
        with self.assertRaisesRegex(RuntimeError, "finite size bound"):
            read_response(uart, "capture", max_header_bytes=64)
        self.assertEqual(uart.stream.tell(), 65)

    def test_malformed_json_fails_closed_before_binary(self) -> None:
        binary = b"\x00\xff\n\x01"
        uart = FragmentedUART(b'{"type":"capture",bad}\n' + binary)
        with self.assertRaisesRegex(RuntimeError, "Malformed UART JSON"):
            read_response(uart, "capture")
        self.assertEqual(uart.read(len(binary)), binary)

    def test_boot_chatter_and_blank_delimiters_can_precede_response(self) -> None:
        uart = FragmentedUART(b"bootloader text\r\n\n" + encoded({"type": "status"}))
        self.assertEqual(read_response(uart, "status"), {"type": "status"})

    def test_firmware_error_frame_is_not_silently_skipped(self) -> None:
        uart = FragmentedUART(encoded({"type": "error", "message": "not_ready"}))
        with self.assertRaisesRegex(RuntimeError, "not_ready"):
            read_response(uart, "capture")

    def test_crc_and_length_validation_reject_damaged_capture(self) -> None:
        binary = bytes(range(12))
        header = metadata(binary)
        validate_capture_binary(binary, header)
        with self.assertRaisesRegex(RuntimeError, "CRC mismatch"):
            validate_capture_binary(binary[:-1] + b"\xff", header)
        with self.assertRaisesRegex(RuntimeError, "Partial raw capture"):
            validate_capture_binary(binary[:-1], header)

    def test_capture_size_count_and_record_format_are_strict(self) -> None:
        header = metadata(bytes(12))
        for changes in (
            {"bytes": True},
            {"event_count": True},
            {"bytes": 13},
            {"bytes": 16384 * 12 + 12},
            {"record_format": ">III"},
        ):
            with self.subTest(changes=changes), self.assertRaisesRegex(RuntimeError, "framing"):
                capture_byte_count({**header, **changes})


if __name__ == "__main__":
    unittest.main()
