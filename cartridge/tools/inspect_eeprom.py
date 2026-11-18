#!/usr/bin/env python3
"""Inspect one complete 256-byte cartridge dump without hardware access or writes.

The tentative layout comes from static analysis of a public Brother/ZINK engine,
not from a physical EEPROM read. CRC agreement is an integrity check, not proof
of cartridge identity, remaining physical tape, or compatibility with a printer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
from pathlib import Path


SOURCE = {
    "url": "https://cdn.zinkapps.com/brotherupgrade-2022071302.tgz",
    "archive_revision": "2022071302",
    "archive_sha256": "8a307f78b9aec092910e0d2089e077ca00f864def08d101ca3b936cff15e4312",
    "member": "./etc/zbe_printer_W.ko",
    "module_sha256": "793c973d1ce14f6ee33fde2db74db8bf1ad5d7f31851b739d62968490ab254b1",
    "observed_printer_revision": "2022071322",
    "layout_verified_against_physical_dump": False,
    "caveat": (
        "The public archive revision differs from the observed printer revision. "
        "Confirm this tentative layout against actual captures before using it "
        "to explain printer behavior. This tool never changes a cartridge."
    ),
}

REGIONS = (
    ("R0", 0x00, 0xAE, 0xAE),
    ("RW1", 0xD0, 0x16, 0xE6),
    ("RW2", 0xE8, 0x0E, 0xF6),
)


def crc16_x25(data: bytes) -> int:
    """CRC-16/IBM-SDLC (X-25): reflected 0x8408, init/xorout 0xFFFF."""
    crc = 0xFFFF
    for value in data:
        crc ^= value
        for _ in range(8):
            crc = (crc >> 1) ^ (0x8408 if crc & 1 else 0)
    return crc ^ 0xFFFF


def _u16(dump: bytes, offset: int) -> int:
    return struct.unpack_from("<H", dump, offset)[0]


def inspect_eeprom(dump: bytes) -> dict[str, object]:
    if len(dump) != 256:
        raise ValueError(f"A complete EEPROM dump must contain exactly 256 bytes; got {len(dump)}")
    checks = []
    for name, offset, length, crc_offset in REGIONS:
        stored = _u16(dump, crc_offset)
        calculated = crc16_x25(dump[offset : offset + length])
        checks.append(
            {
                "region": name,
                "data_offset": offset,
                "data_length": length,
                "crc_offset": crc_offset,
                "stored_crc_le": stored,
                "stored_crc_hex": f"{stored:04X}",
                "calculated_crc": calculated,
                "calculated_crc_hex": f"{calculated:04X}",
                "matches": calculated == stored,
            }
        )
    product_revision = dump[1]
    remaining = _u16(dump, 0xE8)
    initial = _u16(dump, 0x60)
    wristband = product_revision == 3
    media = {
        "interpretation_is_tentative": True,
        "remaining_raw_u16_le_at_0xE8": remaining,
        "initial_raw_u16_le_at_0x60": initial,
        "remaining_unit_from_public_engine": "bands" if wristband else "1/32 inch",
        "remaining_bands": remaining if wristband else None,
        "remaining_inches": None if wristband else remaining / 32,
        "remaining_mm": None if wristband else remaining * 127 / 160,
        "initial_inches": None if wristband else initial / 32,
        "initial_mm": None if wristband else initial * 127 / 160,
        "physical_remaining_tape_verified": False,
        "note": (
            "Revision 3 uses bands in the public display routine; no length conversion is inferred."
            if wristband
            else "The public display routine divides the raw counter by 32 for whole inches."
        ),
    }
    return {
        "schema": "sunburn.brother.eeprom-inspection.v1",
        "input_bytes": len(dump),
        "input_sha256": hashlib.sha256(dump).hexdigest(),
        "input_hex": dump.hex(),
        "unobserved_bytes_inferred": False,
        "source": SOURCE.copy(),
        "observed_header": {
            "format_byte_at_0x00": dump[0],
            "product_revision_byte_at_0x01": product_revision,
            "packed_flags_byte_at_0x03": dump[3],
            "lot_code_bytes_at_0x10_length_10_hex": dump[0x10:0x1A].hex(),
        },
        "tentative_media_fields": media,
        "crc_algorithm": {
            "name": "CRC-16/IBM-SDLC (X-25)",
            "normal_polynomial": "1021",
            "reflected_polynomial": "8408",
            "initial": "FFFF",
            "xorout": "FFFF",
            "check_123456789": "906E",
            "stored_byte_order": "little-endian",
        },
        "crc_checks": checks,
        "all_three_crc_regions_match": all(check["matches"] for check in checks),
        "crc_notice": "CRC agreement does not authenticate a cartridge or establish physical tape length.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dump", type=Path, help="Exactly 256 observed bytes; input is opened read-only")
    arguments = parser.parse_args()
    try:
        result = inspect_eeprom(arguments.dump.read_bytes())
    except ValueError as error:
        parser.error(str(error))
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
