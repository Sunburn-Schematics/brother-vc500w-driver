#!/usr/bin/env python3
"""Decode passive GPIO snapshots; never communicates with any hardware.

Input is JSON capture metadata plus little-endian <III records:
esp_timer microseconds low32, CPU cycle counter low32, levels (SCL=1,SDA=2).
All byte meanings are observations/hypotheses, not an authenticated device dump.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import struct
import sys
import zlib
from dataclasses import asdict, dataclass, field
from itertools import pairwise
from pathlib import Path

RECORD = struct.Struct("<III")
MOD32 = 1 << 32


@dataclass(frozen=True)
class Event:
    index: int
    timestamp_us_raw: int
    cycle_raw: int
    levels: int
    timestamp_us: int
    cycle: int


@dataclass
class Byte:
    value: int
    acknowledged: bool
    first_event: int
    ack_event: int


@dataclass
class Transaction:
    index: int
    first_event: int
    last_event: int
    start_us: int
    end_us: int
    end_condition: str
    bytes: list[Byte] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    partial_bits: list[int] = field(default_factory=list)
    address: int | None = None
    direction: str | None = None
    operation: str = "unclassified"
    pointer: int | None = None


@dataclass(frozen=True)
class SamplingWindow:
    index: int
    first_event: int
    event_count: int
    start_us: int
    end_us: int
    overflow: bool
    missing: bool


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value


def cpu_frequency(metadata: dict[str, object]) -> float:
    frequency = metadata.get("cpu_hz")
    if frequency is None:
        mhz = metadata.get("cpu_mhz")
        if isinstance(mhz, bool) or not isinstance(mhz, (int, float)):
            raise ValueError("cpu_hz or cpu_mhz is required")
        frequency = mhz * 1_000_000
    if isinstance(frequency, bool) or not isinstance(frequency, (int, float)):
        raise ValueError("CPU frequency must be numeric")
    if not math.isfinite(frequency) or frequency <= 0:
        raise ValueError("CPU frequency must be positive and finite")
    return float(frequency)


def sampling_windows(metadata: dict[str, object], record_count: int) -> list[SamplingWindow] | None:
    declared = (
        metadata.get("firmware") == "i2c-passive-poll-v3"
        or "sampling_windows" in metadata
        or metadata.get("capture_mode") == "windowed_polling"
        or metadata.get("sampling_gaps_possible") is True
    )
    if not declared:
        return None
    if (
        metadata.get("capture_mode") != "windowed_polling"
        or metadata.get("sampling_gaps_possible") is not True
    ):
        raise ValueError(
            "Windowed captures require explicit capture_mode and sampling_gaps_possible"
        )
    source = metadata.get("sampling_windows")
    if not isinstance(source, list):
        raise ValueError(
            "Windowed capture requires sampling_windows; continuity cannot be inferred"
        )
    result: list[SamplingWindow] = []
    next_event = 0
    for index, item in enumerate(source):
        if not isinstance(item, dict):
            raise ValueError("Each sampling window must be an object")
        first = _integer(item.get("first_event"), "window first_event")
        count = _integer(item.get("event_count"), "window event_count")
        start = _integer(item.get("start_us"), "window start_us")
        end = _integer(item.get("end_us"), "window end_us")
        if first != next_event or count < 1 or first + count > record_count:
            raise ValueError("Sampling window slices must cover records exactly once in order")
        if start < 0 or end < start or (result and start < result[-1].end_us):
            raise ValueError("Sampling window times must be ordered and nonoverlapping")
        overflow, missing = item.get("overflow", False), item.get("missing", False)
        if not isinstance(overflow, bool) or not isinstance(missing, bool):
            raise ValueError("Window overflow/missing flags must be boolean")
        result.append(SamplingWindow(index, first, count, start, end, overflow, missing))
        next_event += count
    if next_event != record_count:
        raise ValueError("Sampling window metadata does not cover every raw record")
    metadata_overflow = metadata.get("sampling_metadata_overflow", False)
    if not isinstance(metadata_overflow, bool):
        raise ValueError("sampling_metadata_overflow must be boolean")
    return result


def unpack_events(
    raw: bytes, metadata: dict[str, object], window_starts: set[int] | None = None
) -> tuple[list[Event], list[str]]:
    warnings: list[str] = []
    expected_crc = metadata.get("crc32")
    if expected_crc is not None:
        if (
            not isinstance(expected_crc, str)
            or re.fullmatch(r"[0-9A-Fa-f]{8}", expected_crc) is None
        ):
            raise ValueError("crc32 must be exactly eight hexadecimal characters")
        actual_crc = f"{zlib.crc32(raw):08x}"
        if expected_crc.lower() != actual_crc:
            warnings.append(f"crc32_mismatch:metadata={expected_crc.lower()},actual={actual_crc}")
    if "bytes" in metadata and _integer(metadata["bytes"], "bytes") != len(raw):
        warnings.append("metadata_byte_count_mismatch")
    if (
        "record_bytes" in metadata
        and _integer(metadata["record_bytes"], "record_bytes") != RECORD.size
    ):
        raise ValueError("record_bytes must be 12 for the supported record layout")
    trailing = len(raw) % RECORD.size
    if trailing:
        warnings.append(f"binary_truncated_record:{trailing}_trailing_bytes")
    records = list(RECORD.iter_unpack(raw[: len(raw) - trailing if trailing else None]))
    count = _integer(metadata.get("event_count"), "event_count")
    if count != len(records):
        warnings.append(f"event_count_mismatch:metadata={count},binary={len(records)}")
    overflow = metadata.get("overflow")
    if isinstance(overflow, bool):
        overflow = int(overflow)
    if not isinstance(overflow, int) or overflow < 0:
        raise ValueError("overflow must be a nonnegative integer or boolean")
    if overflow:
        warnings.append(f"capture_overflow:{overflow}")
    frequency = cpu_frequency(metadata)
    cycles_per_us = frequency / 1_000_000
    events: list[Event] = []
    start_us = metadata.get("capture_start_us")
    end_us = metadata.get("capture_end_us")
    if start_us is not None:
        start_us = _integer(start_us, "capture_start_us")
    if end_us is not None:
        end_us = _integer(end_us, "capture_end_us")
    if isinstance(start_us, int) and isinstance(end_us, int) and end_us < start_us:
        warnings.append("metadata_capture_end_precedes_start")
    for index, (raw_us, raw_cycle, levels) in enumerate(records):
        if levels & ~3:
            warnings.append(f"invalid_levels:event={index},value={levels}")
        levels &= 3
        us, cycle = raw_us, raw_cycle
        if not events and isinstance(start_us, int):
            us = start_us + ((raw_us - start_us) % MOD32)
        if events:
            previous = events[-1]
            delta_us = (raw_us - previous.timestamp_us_raw) % MOD32
            if delta_us > MOD32 // 2:
                warnings.append(f"timestamp_order_or_ambiguous_wrap:event={index}")
            us = previous.timestamp_us + delta_us
            modulo_delta = (raw_cycle - previous.cycle_raw) % MOD32
            expected = delta_us * cycles_per_us
            # Microseconds disambiguate multiple cycle-counter wraps in long idle gaps.
            wraps = max(0, round((expected - modulo_delta) / MOD32))
            delta_cycle = modulo_delta + wraps * MOD32
            cycle = previous.cycle + delta_cycle
            boundary = window_starts is not None and index in window_starts
            if not boundary and abs(delta_cycle - expected) > max(
                5 * cycles_per_us, expected * 0.01
            ):
                warnings.append(f"cycle_timestamp_mismatch:event={index}")
            if not boundary and previous.levels == levels:
                warnings.append(f"duplicate_snapshot:event={index}")
        events.append(Event(index, raw_us, raw_cycle, levels, us, cycle))
    if events and isinstance(end_us, int) and events[-1].timestamp_us > end_us + 5:
        warnings.append("event_timestamp_exceeds_capture_end")
    if not events:
        warnings.append("empty_capture")
    return events, warnings


def reconstruct_transactions(
    events: list[Event],
    end_condition: str = "capture_end",
) -> tuple[list[Transaction], list[str], list[float]]:
    transactions: list[Transaction] = []
    warnings: list[str] = []
    current: Transaction | None = None
    bits: list[int] = []
    first_bit_event = 0
    rises: list[float] = []
    previous_rise: float | None = None

    def finish(event: Event, condition: str) -> None:
        nonlocal current, bits, previous_rise
        if current is None:
            return
        current.last_event = event.index
        current.end_us = event.timestamp_us
        current.end_condition = condition
        # Raising SCL before changing SDA produces one preparatory sample for
        # STOP/repeated START. This is not an incomplete data byte.
        setup_bit = (condition == "stop" and bits == [0]) or (
            condition == "repeated_start" and bits == [1]
        )
        current.partial_bits = [] if setup_bit else bits.copy()
        if bits and not setup_bit:
            current.warnings.append(f"truncated_byte:{len(bits)}_bits")
        if not current.bytes:
            current.warnings.append("no_complete_address_byte")
        if condition in {"capture_end", "sampling_window_end"}:
            current.warnings.append(f"no_stop_or_repeated_start_before_{condition}")
        transactions.append(current)
        current, bits, previous_rise = None, [], None

    if events and events[0].levels != 3:
        warnings.append("capture_started_mid_bus_activity")
    for previous, event in pairwise(events):
        changed = previous.levels ^ event.levels
        if changed == 0:
            continue
        if changed == 3:
            message = f"ambiguous_simultaneous_scl_sda_change:event={event.index}"
            warnings.append(message)
            if current is not None:
                current.warnings.append(message)
            # A missed intermediate snapshot cannot be resolved by choosing an order.
            continue
        scl, sda = event.levels & 1, (event.levels >> 1) & 1
        if changed == 2 and scl:
            if not sda:
                finish(event, "repeated_start")
                current = Transaction(
                    len(transactions),
                    event.index,
                    event.index,
                    event.timestamp_us,
                    event.timestamp_us,
                    "pending",
                )
                bits, previous_rise = [], None
            else:
                if current is None:
                    warnings.append(f"stop_without_start:event={event.index}")
                finish(event, "stop")
            continue
        if changed != 1 or not scl:
            continue
        if current is None:
            warnings.append(f"clock_without_start:event={event.index}")
            continue
        if previous_rise is not None:
            rises.append(float(event.cycle - previous_rise))
        previous_rise = float(event.cycle)
        if not bits:
            first_bit_event = event.index
        bits.append(sda)
        if len(bits) == 9:
            value = sum(bit << (7 - position) for position, bit in enumerate(bits[:8]))
            current.bytes.append(Byte(value, bits[8] == 0, first_bit_event, event.index))
            bits = []
    if events:
        finish(events[-1], end_condition)
    return transactions, warnings, rises


def interpret_eeprom(
    transactions: list[Transaction], capture_damaged: bool
) -> list[dict[str, object]]:
    pointer: int | None = None
    observations: list[dict[str, object]] = []
    for transaction in transactions:
        if transaction.warnings:
            pointer = None
        if not transaction.bytes:
            pointer = None
            continue
        address_byte, *payload = transaction.bytes
        transaction.address = address_byte.value >> 1
        transaction.direction = "read" if address_byte.value & 1 else "write"
        if transaction.address != 0x50:
            transaction.operation = "other_device"
            continue
        if not address_byte.acknowledged:
            transaction.operation = "address_nack_or_busy_poll"
            if payload:
                transaction.warnings.append("data_after_unacknowledged_address")
                pointer = None
            continue
        if not payload:
            transaction.operation = "address_ack_only"
            continue
        transaction.pointer = pointer
        if transaction.direction == "read":
            transaction.operation = (
                "sequential_read" if len(payload) > 1 else "current_address_read"
            )
            for position, byte in enumerate(payload):
                if not byte.acknowledged and position != len(payload) - 1:
                    transaction.warnings.append("read_data_after_master_nack")
            if payload[-1].acknowledged:
                transaction.warnings.append("read_not_terminated_with_master_nack")
            values = payload
            offsets = [
                None if pointer is None else (pointer + i) & 0xFF for i in range(len(values))
            ]
            kind = "observed_read"
            if pointer is None:
                transaction.warnings.append("unknown_memory_pointer_no_offsets_inferred")
        else:
            pointer_byte, *values = payload
            transaction.pointer = pointer_byte.value
            if not pointer_byte.acknowledged:
                transaction.operation = "memory_pointer_nack"
                pointer = None
                continue
            pointer = pointer_byte.value
            if not values:
                transaction.operation = "pointer_setup_only_no_eeprom_write"
                if transaction.warnings or capture_damaged:
                    pointer = None
                continue
            transaction.operation = (
                "page_write_candidate" if len(values) > 1 else "byte_write_candidate"
            )
            if any(not byte.acknowledged for byte in values):
                transaction.warnings.append("unacknowledged_write_data_not_committed")
            if transaction.end_condition != "stop":
                transaction.warnings.append("write_missing_stop_not_committed")
            offsets = [(pointer & 0xF0) | ((pointer + i) & 0x0F) for i in range(len(values))]
            kind = "acknowledged_write_candidate"
        for position, (offset, byte) in enumerate(zip(offsets, values, strict=True)):
            observations.append(
                {
                    "offset": offset,
                    "value": byte.value,
                    "hex": f"{byte.value:02X}",
                    "kind": "unacknowledged_write_observation"
                    if kind == "acknowledged_write_candidate" and not byte.acknowledged
                    else kind,
                    "transaction": transaction.index,
                    "data_byte_index": position,
                    "first_event": byte.first_event,
                    "ack_event": byte.ack_event,
                    "acknowledged": byte.acknowledged,
                    "integrity": "invalid_capture"
                    if capture_damaged
                    else "transaction_warning"
                    if transaction.warnings
                    else "observed_only",
                    "committed_write_verified": False,
                }
            )
        if capture_damaged or transaction.warnings:
            pointer = None
        elif offsets and offsets[-1] is not None:
            pointer = (offsets[-1] + 1) & 0xFF
    return observations


def clock_measurements(
    events: list[Event], clock_cycles: list[float], frequency: float
) -> tuple[list[float], dict[str, list[float]], list[str]]:
    warnings: list[str] = []
    # Long inter-byte pauses are legal; report observed rates rather than rejecting them.
    rates = [frequency / cycles for cycles in clock_cycles if cycles > 0]
    if any(rate > 440_000 for rate in rates):
        warnings.append("clock_rate_exceeds_400khz_device_spec_with_10pct_margin")
    if any(cycles <= 0 for cycles in clock_cycles):
        warnings.append("nonpositive_clock_period")
    pulse_durations: dict[str, list[float]] = {"high": [], "low": []}
    last_clock_edge: Event | None = None
    for previous, event in pairwise(events):
        if not (previous.levels ^ event.levels) & 1:
            continue
        if last_clock_edge is not None:
            duration = (event.cycle - last_clock_edge.cycle) / (frequency / 1_000_000)
            phase = "high" if last_clock_edge.levels & 1 else "low"
            pulse_durations[phase].append(duration)
            if duration < 0.3:
                warnings.append(f"very_short_scl_{phase}_pulse:event={event.index}")
        last_clock_edge = event
    return rates, pulse_durations, warnings


def decode(raw: bytes, metadata: dict[str, object]) -> dict[str, object]:
    windows = sampling_windows(metadata, len(raw) // RECORD.size)
    window_starts = {window.first_event for window in windows} if windows is not None else None
    events, warnings = unpack_events(raw, metadata, window_starts)
    if windows is not None and metadata.get("sampling_metadata_overflow"):
        warnings.append("sampling_window_metadata_overflow")
    global_damage = bool(warnings)
    frequency = cpu_frequency(metadata)
    transactions: list[Transaction] = []
    observations: list[dict[str, object]] = []
    rates: list[float] = []
    pulse_durations: dict[str, list[float]] = {"high": [], "low": []}
    window_summaries: list[dict[str, object]] = []
    transaction_windows: dict[int, int] = {}
    slices = (
        [(None, events)]
        if windows is None
        else [
            (window, events[window.first_event : window.first_event + window.event_count])
            for window in windows
        ]
    )
    for window, segment in slices:
        condition = "capture_end" if window is None else "sampling_window_end"
        decoded, local_warnings, clock_cycles = reconstruct_transactions(segment, condition)
        local_rates, local_pulses, clock_warnings = clock_measurements(
            segment, clock_cycles, frequency
        )
        local_warnings.extend(clock_warnings)
        if window is not None:
            if (
                segment[0].timestamp_us < window.start_us
                or segment[-1].timestamp_us > window.end_us + 5
            ):
                local_warnings.append("event_timestamp_outside_sampling_window")
            if window.overflow:
                local_warnings.append("sampling_window_overflow")
            if window.missing:
                local_warnings.append("sampling_window_missing_data")
        for transaction in decoded:
            transaction.index = len(transactions)
            transactions.append(transaction)
            if window is not None:
                transaction_windows[transaction.index] = window.index
        # interpret_eeprom starts with pointer=None on EVERY window, even after
        # a clean STOP. Unsampled traffic could have changed the memory pointer.
        local_observations = interpret_eeprom(decoded, global_damage or bool(local_warnings))
        if window is None:
            warnings.extend(local_warnings)
        else:
            for observation in local_observations:
                observation["sampling_window"] = window.index
            warnings.extend(
                f"sampling_window={window.index}:{warning}" for warning in local_warnings
            )
            window_summaries.append(
                {
                    **asdict(window),
                    "warnings": local_warnings,
                    "transaction_count": len(decoded),
                    "pointer_reset_at_start": True,
                }
            )
        observations.extend(local_observations)
        rates.extend(local_rates)
        for phase in pulse_durations:
            pulse_durations[phase].extend(local_pulses[phase])
    transaction_warning_count = sum(len(transaction.warnings) for transaction in transactions)
    result = {
        "schema": "sunburn.brother.passive-i2c-decode.v1",
        "input_sha256": hashlib.sha256(raw).hexdigest(),
        "input_bytes": len(raw),
        "event_count": len(events),
        "metadata": metadata,
        "quality": {
            "status": "anomalies_detected"
            if warnings or transaction_warning_count
            else "no_detected_anomalies",
            "lossless_proven": False,
            "notice": "GPIO snapshots can miss edges without an overflow; validate against measured bus timing or a logic analyzer.",
            "warnings": warnings,
            "transaction_warning_count": transaction_warning_count,
        },
        "time": {
            "first_us": events[0].timestamp_us if events else None,
            "last_us": events[-1].timestamp_us if events else None,
            "first_cycle": events[0].cycle if events else None,
            "last_cycle": events[-1].cycle if events else None,
        },
        "clock": {
            "observed_period_count": len(rates),
            "minimum_observed_hz": min(rates) if rates else None,
            "maximum_observed_hz": max(rates) if rates else None,
            "minimum_high_us": min(pulse_durations["high"], default=None),
            "minimum_low_us": min(pulse_durations["low"], default=None),
            "includes_legal_inter_byte_pauses": True,
        },
        "transactions": [asdict(transaction) for transaction in transactions],
        "offset_updates": observations,
        "memory_image": None,
        "memory_image_notice": "No unobserved byte is initialized or inferred; writes remain candidates until a later matching read.",
    }
    if windows is not None:
        for transaction in result["transactions"]:
            transaction["sampling_window"] = transaction_windows[transaction["index"]]
        gaps = [
            {
                "after_window": left.index,
                "before_window": right.index,
                "start_us": left.end_us,
                "end_us": right.start_us,
                "duration_us": right.start_us - left.end_us,
                "pointer_invalidated": True,
                "transaction_continuity": False,
            }
            for left, right in pairwise(windows)
        ]
        result["sampling"] = {
            "mode": "windowed_polling",
            "continuous": False,
            "windows": window_summaries,
            "unsampled_gaps": gaps,
            "notice": "Every window begins a separate decode with unknown EEPROM pointer. No edge, transaction or clock period is inferred across a gap.",
        }
        result["quality"]["sampling_notices"] = [
            f"documented_unsampled_gap:after_window={gap['after_window']},before_window={gap['before_window']}"
            for gap in gaps
        ]
        if not warnings and not transaction_warning_count:
            result["quality"]["status"] = "windowed_sampling_no_detected_in_window_anomalies"
        complete_bytes = len(raw) - len(raw) % RECORD.size
        result["raw_events"] = [
            {"index": index, "timestamp_us_low32": us, "cycles_low32": cycles, "levels": levels}
            for index, (us, cycles, levels) in enumerate(RECORD.iter_unpack(raw[:complete_bytes]))
        ]
        result["trailing_record_bytes_hex"] = raw[complete_bytes:].hex()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("metadata", type=Path)
    parser.add_argument("capture", type=Path)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    metadata = json.loads(arguments.metadata.read_text())
    if not isinstance(metadata, dict):
        parser.error("metadata JSON must be an object")
    try:
        result = decode(arguments.capture.read_bytes(), metadata)
    except ValueError as error:
        parser.error(str(error))
    text = json.dumps(result, indent=2, allow_nan=False) + "\n"
    if arguments.output:
        with arguments.output.open("x") as output:
            output.write(text)
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
