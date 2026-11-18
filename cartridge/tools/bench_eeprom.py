"""P53-local isolated-chip evidence helper; imports never access hardware.

Template: the frozen manage_esp32.py Session/device guards and inspect_eeprom.py
CRC/layout inspector in this directory. Active bus commands require the explicit
--isolated flag. A restore requires a prior qualified two-read receipt and saves
the exact preimage/expected postimage/plan durably before sending its command.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import struct
import sys
import time
from pathlib import Path

from inspect_eeprom import crc16_x25, inspect_eeprom
from manage_esp32 import Audit, ManagementError, Session, validate_device

FIRMWARE = "i2c-isolated-bench-v5"
CAPABILITY = "isolated_restore_initial_v1"
SCHEMA = "sunburn.brother.isolated-bench.v1"
ALLOWED_CHANGES = {0xE8, 0xE9, 0xF6, 0xF7}
PHASES = (
    "pre_read_1",
    "pre_read_2",
    "precheck",
    "counter_write",
    "counter_ack_poll",
    "counter_verify",
    "crc_write",
    "crc_ack_poll",
    "post_read_1",
    "post_read_2",
    "complete",
)
STATES = {"none", "possible_partial", "counter_verified_crc_pending", "verified", "unchanged"}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def require_hash(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ManagementError("An exact lowercase SHA256 pin is required")
    return value


def durable_bytes(path: Path, data: bytes) -> None:
    """Exclusive write, flush, OS sync and readback before proceeding."""
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    if path.read_bytes() != data:
        raise ManagementError("Evidence file readback did not match")


def durable_json(path: Path, value: object) -> None:
    durable_bytes(path, (json.dumps(value, indent=2, allow_nan=False) + "\n").encode())


def qualify_image(frame: dict[str, object]) -> bytes:
    value = frame.get("hex")
    if (
        frame.get("type") != "bench_image"
        or type(frame.get("bytes")) is not int
        or frame.get("bytes") != 256
        or not isinstance(value, str)
        or re.fullmatch(r"[0-9a-f]{512}", value) is None
    ):
        raise ManagementError("Bench image is not exactly 256 observed bytes")
    data = bytes.fromhex(value)
    if require_hash(frame.get("sha256")) != digest(data):
        raise ManagementError("Bench image hash mismatch")
    if not all(frame.get(name) is True for name in ("crc_r0", "crc_rw1", "crc_rw2")):
        raise ManagementError("Device did not confirm all three EEPROM CRCs")
    if not inspect_eeprom(data)["all_three_crc_regions_match"]:
        raise ManagementError("Independent host EEPROM CRC validation failed")
    return data


def restoration_plan(preimage: bytes) -> tuple[dict[str, object], bytes]:
    if len(preimage) != 256 or not inspect_eeprom(preimage)["all_three_crc_regions_match"]:
        raise ManagementError("Restore requires a complete all-CRC-valid preimage")
    initial = struct.unpack_from("<H", preimage, 0x60)[0]
    remaining = struct.unpack_from("<H", preimage, 0xE8)[0]
    if preimage[1] == 3 or initial in (0, 0xFFFF) or remaining > initial:
        raise ManagementError("EEPROM is outside the reviewed label-counter restore scope")
    expected = bytearray(preimage)
    struct.pack_into("<H", expected, 0xE8, initial)
    struct.pack_into("<H", expected, 0xF6, crc16_x25(expected[0xE8:0xF6]))
    changed = [index for index in range(256) if expected[index] != preimage[index]]
    if not set(changed) <= ALLOWED_CHANGES:
        raise ManagementError("Restore plan would change an unapproved byte")
    return {
        "preimage_sha256": digest(preimage),
        "expected_sha256": digest(expected),
        "initial_u16": initial,
        "old_remaining_u16": remaining,
        "new_remaining_u16": initial,
        "changed_offsets": changed,
    }, bytes(expected)


def load_qualified_preimage(directory: Path, pin: str) -> bytes:
    require_hash(pin)
    receipt_path = directory / "receipt.json"
    if receipt_path.stat().st_size > 65536:
        raise ManagementError("Prior read receipt exceeded its size bound")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if (
        not isinstance(receipt, dict)
        or receipt.get("schema") != SCHEMA
        or receipt.get("action") != "read"
        or receipt.get("status") != "verified_read"
        or receipt.get("firmware") != FIRMWARE
        or receipt.get("image_sha256") != pin
        or receipt.get("reads_identical") is not True
        or receipt.get("all_crcs_valid") is not True
        or receipt.get("pins_input_only") is not True
        or receipt.get("internal_pulls") is not False
    ):
        raise ManagementError("Restore requires a prior successful isolated two-read receipt")
    images = []
    for phase in ("pre_read_1", "pre_read_2"):
        path = directory / f"{phase}.bin"
        with path.open("rb") as stream:
            data = stream.read(257)
        if len(data) != 256 or digest(data) != pin:
            raise ManagementError("Prior isolated preimage bytes do not match their exact pin")
        if not inspect_eeprom(data)["all_three_crc_regions_match"]:
            raise ManagementError("Prior isolated preimage failed independent CRC validation")
        images.append(data)
    if images[0] != images[1]:
        raise ManagementError("Prior isolated reads differ")
    return images[0]


class DurableAudit(Audit):
    def record(self, event: str, **fields: object) -> None:
        super().record(event, **fields)
        os.fsync(self.stream.fileno())


def receive_frame(session: Session, deadline: float) -> dict[str, object]:
    """Unlike management's single-response parser, a bench command streams types."""
    line = bytearray()
    while session.clock() < deadline:
        value = session.uart.read(1)
        if not value:
            continue
        line.extend(value)
        if len(line) > 4096:
            raise ManagementError("Bench JSON response exceeded its finite bound")
        if value != b"\n":
            continue
        try:
            result = json.loads(line)
        except (ValueError, UnicodeError) as error:
            raise ManagementError("Malformed bench JSON; no automatic retry") from error
        if not isinstance(result, dict):
            raise ManagementError("Bench response must be a JSON object")
        return result
    raise ManagementError("Bench operation timed out; no automatic retry")


def run_bench(
    session: Session,
    output: Path,
    action: str,
    pullups: str,
    image_pin: str,
    elf_pin: str,
    preimage: bytes | None = None,
) -> dict[str, object]:
    require_hash(image_pin)
    require_hash(elf_pin)
    if action not in {"read", "restore"} or pullups not in {"NONE", "WEAK"}:
        raise ManagementError("Unsupported bench operation or pullup mode")
    network = session.probe()
    if network.get("app_elf_sha256") != elf_pin:
        raise ManagementError("Running bench firmware ELF does not match its exact pin")
    session.command("INFO")
    info = session.receive("status", 5)
    if info.get("firmware") != FIRMWARE or info.get("bench_capability") != CAPABILITY:
        raise ManagementError("Running firmware lacks the reviewed isolated-bench capability")
    plan = None
    expected = None
    if action == "restore":
        if preimage is None or digest(preimage) != image_pin:
            raise ManagementError("Restore preimage does not match its exact pin")
        plan, expected = restoration_plan(preimage)
        durable_bytes(output / "qualified-preimage.bin", preimage)
        durable_bytes(output / "expected-postimage.bin", expected)
        durable_json(output / "restore-plan-before-command.json", plan)
    intent = {
        "schema": SCHEMA,
        "action": action,
        "firmware": FIRMWARE,
        "app_elf_sha256": elf_pin,
        "pullups": pullups,
        "expected_preimage_sha256": image_pin,
        "physical_isolation_asserted_by_operator": True,
        "template_source": "Frozen manage_esp32.py and inspect_eeprom.py in this directory",
        "unknown_bytes_filled": False,
        "automatic_retry": False,
    }
    durable_json(output / "intent-before-command.json", intent)
    command = f"BENCH_{action.upper()} ISOLATED {pullups}"
    if action == "restore":
        command += f" {image_pin}"
    sent = False
    images: list[bytes] = []
    image_phases = ["pre_read_1", "pre_read_2"] + (
        ["post_read_1", "post_read_2"] if action == "restore" else []
    )
    plan_seen = False
    phase_index = -1
    final = None
    try:
        # A failed/partial UART send may have reached the firmware. Mark uncertain first.
        sent = True
        session.command(f"BENCH_{action.upper()}", (command + "\n").encode("ascii"))
        deadline = session.clock() + 15
        for number in range(48):
            frame = receive_frame(session, deadline)
            durable_json(output / f"frame-{number:02d}.json", frame)
            kind = frame.get("type")
            if number == 0:
                if (
                    kind != "bench_start"
                    or frame.get("op") != action
                    or frame.get("pullups") != pullups
                ):
                    raise ManagementError("Bench start does not match the requested operation")
                continue
            if kind == "bench_phase":
                phase = frame.get("phase")
                if phase not in PHASES or PHASES.index(phase) <= phase_index:
                    raise ManagementError("Unexpected or reordered bench phase")
                phase_index = PHASES.index(phase)
                if phase in {
                    "counter_write",
                    "counter_ack_poll",
                    "counter_verify",
                    "crc_write",
                    "crc_ack_poll",
                } and (action != "restore" or not plan_seen or len(images) != 2):
                    raise ManagementError(
                        "Write phase arrived before qualified matching preimages and plan"
                    )
                continue
            if kind == "bench_image":
                if (
                    len(images) >= len(image_phases)
                    or frame.get("phase") != image_phases[len(images)]
                ):
                    raise ManagementError("Unexpected or reordered bench image")
                data = qualify_image(frame)
                wanted = preimage if len(images) < 2 and action == "restore" else expected
                if len(images) < 2:
                    if digest(data) != image_pin or (images and data != images[0]):
                        raise ManagementError(
                            "Fresh isolated preimages do not match the exact expected image"
                        )
                elif not plan_seen or data != expected:
                    raise ManagementError("Postimage differs from the exact reviewed 256-byte plan")
                if wanted is not None and data != wanted:
                    raise ManagementError("Observed image differs from the frozen expected bytes")
                durable_bytes(output / f"{frame['phase']}.bin", data)
                images.append(data)
                continue
            if kind == "bench_plan":
                if action != "restore" or plan_seen or len(images) != 2 or plan is None:
                    raise ManagementError("Unexpected or premature bench plan")
                if any(frame.get(key) != value for key, value in plan.items()):
                    raise ManagementError(
                        "Device restore plan differs from the plan saved before command"
                    )
                plan_seen = True
                continue
            if kind == "bench_result":
                final = frame
                break
            raise ManagementError("Unexpected bench response type; no automatic retry")
        if final is None:
            raise ManagementError("Bench response count exceeded its finite bound")
        if (
            final.get("op") != action
            or final.get("ok") is not True
            or final.get("phase") != "complete"
            or final.get("mutation_state") not in STATES
            or final.get("reads_identical") is not True
            or final.get("crcs_valid") is not True
            or final.get("pins_input_only") is not True
            or final.get("internal_pulls") is not False
            or final.get("preimage_sha256") != image_pin
            or len(images) != len(image_phases)
        ):
            raise ManagementError("Bench completion was not fully verified")
        if action == "read":
            if final.get("mutation_state") != "none":
                raise ManagementError("Read-only command reported a mutation")
        elif (
            not plan_seen
            or expected is None
            or final.get("expected_sha256") != digest(expected)
            or final.get("postimage_sha256") != digest(expected)
            or final.get("mutation_state") != ("unchanged" if expected == preimage else "verified")
        ):
            raise ManagementError("Restore result does not match the exact expected image")
        post_network = session.probe()
        if post_network.get("app_elf_sha256") != elf_pin:
            raise ManagementError("Post-operation firmware identity changed")
        session.command("INFO")
        post_info = session.receive("status", 5)
        if post_info.get("bench_capability") != CAPABILITY:
            raise ManagementError("Post-operation bench capability was not confirmed")
        receipt = {
            **intent,
            "status": "verified_read" if action == "read" else "verified_restore",
            "image_sha256": image_pin,
            "postimage_sha256": digest(expected) if expected else None,
            "reads_identical": True,
            "all_crcs_valid": True,
            "pins_input_only": True,
            "internal_pulls": False,
            "device_result": final,
        }
        durable_json(output / "receipt.json", receipt)
        return receipt
    except Exception:
        failure = {
            **intent,
            "status": "unverified",
            "command_may_have_reached_device": sent,
            "eeprom_mutation_possible": sent and action == "restore",
            "pins_restored_verified": bool(
                final
                and final.get("pins_input_only") is True
                and final.get("internal_pulls") is False
            ),
            "device_result": final,
            "automatic_retry": False,
        }
        durable_json(output / "failure.json", failure)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("read", "restore"))
    parser.add_argument(
        "--isolated",
        action="store_true",
        help="Operator confirms printer SDA/SCL physically disconnected",
    )
    parser.add_argument("--port", required=True)
    parser.add_argument("--serial-number", required=True)
    parser.add_argument("--pullups", choices=("NONE", "WEAK"), default="NONE")
    parser.add_argument("--expect-image-sha256", required=True)
    parser.add_argument("--expect-elf-sha256", required=True)
    parser.add_argument("--preimage-dir", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if not args.isolated:
        parser.error("--isolated requires confirmed physical printer SDA/SCL isolation")
    if (args.action == "restore") != (args.preimage_dir is not None):
        parser.error("Only restore requires --preimage-dir from a prior successful isolated read")
    require_hash(args.expect_image_sha256)
    require_hash(args.expect_elf_sha256)
    preimage = (
        load_qualified_preimage(args.preimage_dir, args.expect_image_sha256)
        if args.preimage_dir
        else None
    )
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
            receipt = run_bench(
                Session(uart, audit, expected_firmware=FIRMWARE),
                args.output,
                args.action,
                args.pullups,
                args.expect_image_sha256,
                args.expect_elf_sha256,
                preimage,
            )
        finally:
            uart.close()
    print(
        json.dumps(
            {
                key: receipt.get(key)
                for key in (
                    "status",
                    "image_sha256",
                    "postimage_sha256",
                    "pins_input_only",
                    "internal_pulls",
                )
            }
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print(
            "Bench operation failed; retained evidence must be reviewed before any retry.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
