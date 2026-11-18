from __future__ import annotations

import struct
import unittest
import zlib

from decode_capture import decode


class Trace:
    def __init__(self, start_us: int = 0, start_cycle: int = 0) -> None:
        self.time = start_us
        self.start_time = start_us
        self.start_cycle = start_cycle
        self.levels = 3
        self.records = [(self.time & 0xFFFFFFFF, start_cycle & 0xFFFFFFFF, 3)]

    def set(self, *, scl: int | None = None, sda: int | None = None, delay: int = 5) -> None:
        self.time += delay
        levels = self.levels
        if scl is not None:
            levels = (levels & ~1) | scl
        if sda is not None:
            levels = (levels & ~2) | (sda << 1)
        if levels != self.levels:
            self.records.append(
                (
                    self.time & 0xFFFFFFFF,
                    (self.start_cycle + (self.time - self.start_time) * 240) & 0xFFFFFFFF,
                    levels,
                )
            )
            self.levels = levels

    def start(self) -> None:
        if self.levels == 3:
            self.set(sda=0)
            self.set(scl=0)
            return
        self.set(scl=0)
        self.set(sda=1)
        self.set(scl=1)
        self.set(sda=0)
        self.set(scl=0)

    def byte(self, value: int, ack: bool = True) -> None:
        for bit in range(7, -1, -1):
            self.set(sda=(value >> bit) & 1)
            self.set(scl=1)
            self.set(scl=0)
        self.set(sda=0 if ack else 1)
        self.set(scl=1)
        self.set(scl=0)

    def stop(self) -> None:
        self.set(sda=0)
        self.set(scl=1)
        self.set(sda=1)

    def result(self, *, overflow: int = 0) -> dict[str, object]:
        raw = b"".join(struct.pack("<III", *record) for record in self.records)
        return decode(raw, {"cpu_mhz": 240, "event_count": len(self.records), "overflow": overflow})


def windowed(traces: list[Trace], **overrides: object) -> dict[str, object]:
    records = []
    windows = []
    for trace in traces:
        windows.append(
            {
                "first_event": len(records),
                "event_count": len(trace.records),
                "start_us": trace.start_time,
                "end_us": trace.time,
                "overflow": False,
                "missing": False,
            }
        )
        records.extend(trace.records)
    raw = b"".join(struct.pack("<III", *record) for record in records)
    metadata = {
        "cpu_mhz": 240,
        "event_count": len(records),
        "overflow": 0,
        "firmware": "i2c-passive-poll-v3",
        "capture_mode": "windowed_polling",
        "sampling_gaps_possible": True,
        "sampling_windows": windows,
        "sampling_metadata_overflow": False,
        "capture_start_us": traces[0].start_time if traces else 0,
        "capture_end_us": traces[-1].time if traces else 0,
        **overrides,
    }
    return decode(raw, metadata)


def write_trace(offset: int, value: int, start_us: int = 0) -> Trace:
    trace = Trace(start_us, start_us * 240)
    trace.start()
    for byte in (0xA0, offset, value):
        trace.byte(byte)
    trace.stop()
    return trace


class DecodeTests(unittest.TestCase):
    def test_acknowledged_byte_write_is_candidate_with_exact_offset_provenance(self) -> None:
        trace = Trace()
        trace.start()
        for value in (0xA0, 0x26, 0x73):
            trace.byte(value)
        trace.stop()
        result = trace.result()
        self.assertEqual(result["transactions"][0]["operation"], "byte_write_candidate")
        self.assertEqual(result["transactions"][0]["warnings"], [])
        update = result["offset_updates"][0]
        self.assertEqual((update["offset"], update["value"]), (0x26, 0x73))
        self.assertEqual(update["integrity"], "observed_only")
        self.assertFalse(update["committed_write_verified"])
        self.assertGreater(update["ack_event"], update["first_event"])
        self.assertIsNone(result["memory_image"])

    def test_random_address_read_tracks_pointer_across_repeated_start(self) -> None:
        trace = Trace()
        trace.start()
        trace.byte(0xA0)
        trace.byte(0x40)
        trace.start()
        trace.byte(0xA1)
        trace.byte(0x11)
        trace.byte(0x22, ack=False)
        trace.stop()
        result = trace.result()
        self.assertEqual(
            [transaction["operation"] for transaction in result["transactions"]],
            ["pointer_setup_only_no_eeprom_write", "sequential_read"],
        )
        self.assertEqual(
            [(update["offset"], update["value"]) for update in result["offset_updates"]],
            [(0x40, 0x11), (0x41, 0x22)],
        )
        self.assertEqual(result["quality"]["transaction_warning_count"], 0)

    def test_current_read_without_observed_pointer_does_not_invent_offsets(self) -> None:
        trace = Trace()
        trace.start()
        trace.byte(0xA1)
        trace.byte(0x99, ack=False)
        trace.stop()
        result = trace.result()
        self.assertIsNone(result["offset_updates"][0]["offset"])
        self.assertIn(
            "unknown_memory_pointer_no_offsets_inferred", result["transactions"][0]["warnings"]
        )

    def test_sequential_read_wraps_at_256_bytes(self) -> None:
        trace = Trace()
        trace.start()
        trace.byte(0xA0)
        trace.byte(0xFF)
        trace.start()
        trace.byte(0xA1)
        trace.byte(1)
        trace.byte(2, ack=False)
        trace.stop()
        self.assertEqual(
            [update["offset"] for update in trace.result()["offset_updates"]], [255, 0]
        )

    def test_page_write_wraps_inside_16_byte_page(self) -> None:
        trace = Trace()
        trace.start()
        for value in (0xA0, 0x2F, 0x81, 0x82, 0x83):
            trace.byte(value)
        trace.stop()
        result = trace.result()
        self.assertEqual(
            [update["offset"] for update in result["offset_updates"]], [0x2F, 0x20, 0x21]
        )

    def test_pointer_setup_is_not_an_eeprom_write(self) -> None:
        trace = Trace()
        trace.start()
        trace.byte(0xA0)
        trace.byte(0x12)
        trace.stop()
        result = trace.result()
        self.assertEqual(result["offset_updates"], [])
        self.assertEqual(
            result["transactions"][0]["operation"], "pointer_setup_only_no_eeprom_write"
        )

    def test_ack_poll_nack_is_normal_not_data_write(self) -> None:
        trace = Trace()
        trace.start()
        trace.byte(0xA0, ack=False)
        trace.stop()
        trace.start()
        trace.byte(0xA0)
        trace.stop()
        result = trace.result()
        self.assertEqual(result["offset_updates"], [])
        self.assertEqual(
            [transaction["operation"] for transaction in result["transactions"]],
            ["address_nack_or_busy_poll", "address_ack_only"],
        )
        self.assertEqual(result["quality"]["transaction_warning_count"], 0)

    def test_data_nack_does_not_claim_write_commit(self) -> None:
        trace = Trace()
        trace.start()
        trace.byte(0xA0)
        trace.byte(0x10)
        trace.byte(0x35, ack=False)
        trace.stop()
        result = trace.result()
        self.assertIn(
            "unacknowledged_write_data_not_committed", result["transactions"][0]["warnings"]
        )
        self.assertEqual(result["offset_updates"][0]["integrity"], "transaction_warning")

    def test_write_followed_by_restart_is_not_committed(self) -> None:
        trace = Trace()
        trace.start()
        for value in (0xA0, 0x10, 0x42):
            trace.byte(value)
        trace.start()
        trace.byte(0xA1)
        trace.byte(0xAA, ack=False)
        trace.stop()
        result = trace.result()
        self.assertIn("write_missing_stop_not_committed", result["transactions"][0]["warnings"])
        self.assertIsNone(result["offset_updates"][-1]["offset"])

    def test_capture_end_flags_no_stop_and_partial_byte(self) -> None:
        trace = Trace()
        trace.start()
        trace.byte(0xA0)
        trace.set(sda=1)
        trace.set(scl=1)
        trace.set(scl=0)
        result = trace.result()
        self.assertIn("truncated_byte:1_bits", result["transactions"][0]["warnings"])
        self.assertIn(
            "no_stop_or_repeated_start_before_capture_end", result["transactions"][0]["warnings"]
        )

    def test_simultaneous_changes_are_flagged_without_guessing_order(self) -> None:
        raw = b"".join(struct.pack("<III", *record) for record in [(0, 0, 3), (5, 1200, 0)])
        result = decode(raw, {"event_count": 2, "overflow": 0, "cpu_mhz": 240})
        self.assertTrue(
            any("ambiguous_simultaneous" in warning for warning in result["quality"]["warnings"])
        )
        self.assertEqual(result["transactions"], [])

    def test_declared_overflow_invalidates_offset_inferences(self) -> None:
        trace = Trace()
        trace.start()
        for value in (0xA0, 0x10, 0x55):
            trace.byte(value)
        trace.stop()
        result = trace.result(overflow=1)
        self.assertEqual(result["offset_updates"][0]["integrity"], "invalid_capture")
        self.assertFalse(result["quality"]["lossless_proven"])

    def test_us_and_cycle_wraps_unroll_without_losing_time(self) -> None:
        trace = Trace(start_us=0xFFFFFFF0, start_cycle=0xFFFFFF00)
        trace.start()
        trace.byte(0xA0, ack=False)
        trace.stop()
        result = trace.result()
        self.assertGreater(result["time"]["last_us"], 0xFFFFFFFF)
        self.assertGreater(result["time"]["last_cycle"], 0xFFFFFFFF)
        self.assertFalse(
            any("cycle_timestamp" in warning for warning in result["quality"]["warnings"])
        )

    def test_long_idle_gap_resolves_multiple_cycle_wraps(self) -> None:
        trace = Trace()
        trace.set(sda=0, delay=40_000_000)
        trace.set(scl=0)
        trace.byte(0xA0, ack=False)
        trace.stop()
        result = trace.result()
        self.assertGreater(result["time"]["last_cycle"], 2 * (1 << 32))
        self.assertFalse(
            any("cycle_timestamp" in warning for warning in result["quality"]["warnings"])
        )

    def test_binary_record_truncation_and_count_mismatch_are_reported(self) -> None:
        result = decode(
            struct.pack("<III", 0, 0, 3) + b"abc", {"cpu_mhz": 240, "overflow": 0, "event_count": 2}
        )
        self.assertIn("binary_truncated_record:3_trailing_bytes", result["quality"]["warnings"])
        self.assertIn("event_count_mismatch:metadata=2,binary=1", result["quality"]["warnings"])

    def test_empty_trace_is_explicit(self) -> None:
        result = decode(b"", {"cpu_mhz": 240, "overflow": 0, "event_count": 0})
        self.assertIn("empty_capture", result["quality"]["warnings"])
        self.assertIsNone(result["clock"]["maximum_observed_hz"])

    def test_invalid_metadata_is_rejected(self) -> None:
        for metadata in (
            {"event_count": True, "overflow": 0, "cpu_mhz": 240},
            {"event_count": 0, "overflow": -1, "cpu_mhz": 240},
            {"event_count": 0, "overflow": 0, "cpu_mhz": float("nan")},
        ):
            with self.subTest(metadata=metadata), self.assertRaises(ValueError):
                decode(b"", metadata)

    def test_submicrosecond_clock_glitch_is_reported_even_if_average_period_is_slow(self) -> None:
        records = [
            (0, 0, 3),
            (5, 1200, 1),
            (10, 2400, 0),
            (20, 4800, 1),
            (20, 4824, 0),
            (30, 7200, 1),
        ]
        raw = b"".join(struct.pack("<III", *record) for record in records)
        result = decode(raw, {"cpu_hz": 240_000_000, "event_count": 6, "overflow": False})
        self.assertTrue(
            any("very_short_scl_high" in warning for warning in result["quality"]["warnings"])
        )
        self.assertAlmostEqual(result["clock"]["minimum_high_us"], 0.1)

    def test_legacy_cpu_hz_metadata_does_not_require_cpu_mhz(self) -> None:
        result = decode(
            struct.pack("<III", 0, 0, 3),
            {"cpu_hz": 240_000_000, "cpu_mhz": None, "event_count": 1, "overflow": 0},
        )
        self.assertEqual(result["quality"]["warnings"], [])

    def test_full_capture_start_aligns_low32_timestamps_after_71_minute_wrap(self) -> None:
        start = (1 << 32) + 100
        raw = struct.pack("<III", 100, 20, 3) + struct.pack("<III", 105, 1220, 1)
        result = decode(
            raw,
            {
                "cpu_mhz": 240,
                "event_count": 2,
                "overflow": 0,
                "capture_start_us": start,
                "capture_end_us": start + 10,
            },
        )
        self.assertEqual(result["time"]["first_us"], start)
        self.assertEqual(result["time"]["last_us"], start + 5)

    def test_timestamp_outside_capture_metadata_is_invalid(self) -> None:
        raw = struct.pack("<III", 100, 20, 3)
        result = decode(
            raw,
            {
                "cpu_mhz": 240,
                "event_count": 1,
                "overflow": 0,
                "capture_start_us": 100,
                "capture_end_us": 90,
            },
        )
        self.assertIn("metadata_capture_end_precedes_start", result["quality"]["warnings"])

    def test_bad_pointer_transaction_invalidates_previous_pointer(self) -> None:
        trace = Trace()
        trace.start()
        trace.byte(0xA0)
        trace.byte(0x44)
        trace.stop()
        trace.start()
        trace.byte(0xA0)
        for bit in (1, 0):
            trace.set(sda=bit)
            trace.set(scl=1)
            trace.set(scl=0)
        trace.stop()
        trace.start()
        trace.byte(0xA1)
        trace.byte(0x99, ack=False)
        trace.stop()
        result = trace.result()
        self.assertIsNone(result["offset_updates"][0]["offset"])

    def test_unknown_device_values_are_not_interpreted_as_cartridge_data(self) -> None:
        trace = Trace()
        trace.start()
        for value in (0xB0, 0x55, 0x66):
            trace.byte(value)
        trace.stop()
        result = trace.result()
        self.assertEqual(result["transactions"][0]["operation"], "other_device")
        self.assertEqual(result["offset_updates"], [])

    def test_crc_mismatch_invalidates_otherwise_well_formed_capture(self) -> None:
        trace = Trace()
        trace.start()
        for value in (0xA0, 0x10, 0x55):
            trace.byte(value)
        trace.stop()
        raw = b"".join(struct.pack("<III", *record) for record in trace.records)
        metadata = {
            "cpu_mhz": 240,
            "event_count": len(trace.records),
            "overflow": 0,
            "crc32": f"{zlib.crc32(raw) ^ 1:08x}",
            "bytes": len(raw),
            "record_bytes": 12,
        }
        result = decode(raw, metadata)
        self.assertTrue(
            any("crc32_mismatch" in warning for warning in result["quality"]["warnings"])
        )
        self.assertEqual(result["offset_updates"][0]["integrity"], "invalid_capture")
        metadata["crc32"] = f"{zlib.crc32(raw):08x}"
        self.assertEqual(decode(raw, metadata)["quality"]["warnings"], [])

    def test_unrecognized_crc_and_record_formats_are_rejected(self) -> None:
        for extra in ({"crc32": 1234}, {"crc32": "0x12345678"}, {"record_bytes": 16}):
            metadata = {"cpu_mhz": 240, "event_count": 0, "overflow": 0, **extra}
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                decode(b"", metadata)

    def test_clean_frames_within_separate_windows_remain_observed_only(self) -> None:
        traces = [write_trace(0x10, 0x22), write_trace(0x20, 0x33, 10000)]
        result = windowed(traces)
        self.assertEqual(
            [item["integrity"] for item in result["offset_updates"]], ["observed_only"] * 2
        )
        self.assertEqual([item["sampling_window"] for item in result["offset_updates"]], [0, 1])
        self.assertEqual(result["quality"]["warnings"], [])
        self.assertEqual(len(result["quality"]["sampling_notices"]), 1)
        self.assertFalse(result["sampling"]["continuous"])
        self.assertFalse(result["quality"]["lossless_proven"])
        self.assertIsNone(result["memory_image"])

    def test_pointer_after_clean_stop_is_not_carried_over_sampling_gap(self) -> None:
        pointer = Trace()
        pointer.start()
        pointer.byte(0xA0)
        pointer.byte(0x40)
        pointer.stop()
        read = Trace(10000, 10000 * 240)
        read.start()
        read.byte(0xA1)
        read.byte(0x99, ack=False)
        read.stop()
        result = windowed([pointer, read])
        self.assertIsNone(result["offset_updates"][0]["offset"])
        self.assertIn(
            "unknown_memory_pointer_no_offsets_inferred", result["transactions"][1]["warnings"]
        )

    def test_repeated_start_pointer_and_read_within_one_window_still_work(self) -> None:
        trace = Trace()
        trace.start()
        trace.byte(0xA0)
        trace.byte(0x40)
        trace.start()
        trace.byte(0xA1)
        trace.byte(0x5A, ack=False)
        trace.stop()
        result = windowed([trace])
        self.assertEqual(result["offset_updates"][0]["offset"], 0x40)
        self.assertEqual(result["offset_updates"][0]["integrity"], "observed_only")

    def test_split_byte_is_not_joined_across_window_boundary(self) -> None:
        trace = write_trace(0x10, 0x55)
        split = len(trace.records) // 2
        windows = [
            {
                "first_event": 0,
                "event_count": split,
                "start_us": 0,
                "end_us": trace.records[split - 1][0],
            },
            {
                "first_event": split,
                "event_count": len(trace.records) - split,
                "start_us": trace.records[split][0],
                "end_us": trace.time,
            },
        ]
        result = windowed([trace], sampling_windows=windows)
        self.assertTrue(
            any(item["end_condition"] == "sampling_window_end" for item in result["transactions"])
        )
        self.assertTrue(
            any(
                "before_sampling_window_end" in warning
                for item in result["transactions"]
                for warning in item["warnings"]
            )
        )
        self.assertFalse(
            any(item["integrity"] == "observed_only" for item in result["offset_updates"])
        )
        self.assertEqual(len(result["raw_events"]), len(trace.records))
        self.assertEqual(
            [item["levels"] for item in result["raw_events"]], [item[2] for item in trace.records]
        )

    def test_identical_initial_snapshots_at_gap_are_not_duplicate_edges(self) -> None:
        first, second = Trace(), Trace(10000, 10000 * 240)
        result = windowed([first, second])
        self.assertEqual(result["quality"]["warnings"], [])
        self.assertEqual(result["transactions"], [])
        self.assertEqual(len(result["raw_events"]), 2)

    def test_level_change_across_gap_does_not_fabricate_start_or_clock(self) -> None:
        first, second = Trace(), Trace(10000, 10000 * 240)
        second.records[0] = (10000, 10000 * 240, 1)
        second.levels = 1
        result = windowed([first, second])
        self.assertEqual(result["transactions"], [])
        self.assertEqual(result["clock"]["observed_period_count"], 0)
        self.assertEqual(result["offset_updates"], [])

    def test_clock_pulses_are_never_measured_across_windows(self) -> None:
        single = windowed([write_trace(0x10, 0x22)])
        both = windowed([write_trace(0x10, 0x22), write_trace(0x20, 0x33, 10000000)])
        self.assertEqual(both["clock"]["minimum_high_us"], single["clock"]["minimum_high_us"])
        self.assertEqual(both["clock"]["minimum_low_us"], single["clock"]["minimum_low_us"])
        self.assertEqual(
            both["clock"]["observed_period_count"], single["clock"]["observed_period_count"] * 2
        )

    def test_window_damage_does_not_poison_a_later_independent_clean_window(self) -> None:
        traces = [write_trace(0x10, 0x22), write_trace(0x20, 0x33, 10000)]
        first_count, second_count = len(traces[0].records), len(traces[1].records)
        windows = [
            {
                "first_event": 0,
                "event_count": first_count,
                "start_us": 0,
                "end_us": traces[0].time,
                "overflow": True,
            },
            {
                "first_event": first_count,
                "event_count": second_count,
                "start_us": 10000,
                "end_us": traces[1].time,
            },
        ]
        result = windowed(traces, sampling_windows=windows)
        self.assertEqual(
            [item["integrity"] for item in result["offset_updates"]],
            ["invalid_capture", "observed_only"],
        )

    def test_global_crc_or_window_metadata_overflow_invalidates_all_windows(self) -> None:
        traces = [write_trace(0x10, 0x22), write_trace(0x20, 0x33, 10000)]
        for override in ({"crc32": "00000000"}, {"sampling_metadata_overflow": True}):
            result = windowed(traces, **override)
            self.assertEqual(
                [item["integrity"] for item in result["offset_updates"]], ["invalid_capture"] * 2
            )

    def test_v3_without_complete_window_metadata_is_rejected(self) -> None:
        trace = Trace()
        for override in (
            {"sampling_windows": None},
            {"sampling_windows": []},
            {"capture_mode": "continuous"},
            {"sampling_gaps_possible": False},
        ):
            with self.assertRaises(ValueError):
                windowed([trace], **override)

    def test_malformed_window_slices_or_times_are_rejected(self) -> None:
        trace = Trace()
        valid = {"first_event": 0, "event_count": 1, "start_us": 0, "end_us": 0}
        for change in (
            {"first_event": 1},
            {"event_count": 0},
            {"event_count": 2},
            {"start_us": 2},
            {"end_us": -1},
            {"missing": 1},
            {"first_event": True},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                windowed([trace], sampling_windows=[{**valid, **change}])

    def test_out_of_window_timestamp_invalidates_affected_observation(self) -> None:
        trace = write_trace(0x10, 0x55)
        window = {"first_event": 0, "event_count": len(trace.records), "start_us": 0, "end_us": 1}
        result = windowed([trace], sampling_windows=[window])
        self.assertEqual(result["offset_updates"][0]["integrity"], "invalid_capture")


if __name__ == "__main__":
    unittest.main()
