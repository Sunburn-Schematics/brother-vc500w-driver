from __future__ import annotations

import struct
import unittest

from inspect_eeprom import REGIONS, crc16_x25, inspect_eeprom


class InspectEepromTests(unittest.TestCase):
    @staticmethod
    def all_ff_with_known_crcs() -> bytes:
        # Values independently calculated from all 256 entries of the firmware's
        # table, rather than using the implementation under test to build them.
        dump = bytearray(b"\xFF" * 256)
        for offset, value in ((0xAE, 0xA684), (0xE6, 0xB532), (0xF6, 0x7B3C)):
            struct.pack_into("<H", dump, offset, value)
        return bytes(dump)

    def test_standard_crc_check_vector_and_empty_input(self) -> None:
        self.assertEqual(crc16_x25(b"123456789"), 0x906E)
        self.assertEqual(crc16_x25(b""), 0)

    def test_independently_known_crc_regions_and_endianness(self) -> None:
        result = inspect_eeprom(self.all_ff_with_known_crcs())
        self.assertTrue(result["all_three_crc_regions_match"])
        self.assertEqual(
            [check["calculated_crc_hex"] for check in result["crc_checks"]],
            ["A684", "B532", "7B3C"],
        )
        self.assertFalse(result["unobserved_bytes_inferred"])
        self.assertEqual(len(result["input_hex"]), 512)

    def test_damage_in_each_protected_region_is_detected(self) -> None:
        original = self.all_ff_with_known_crcs()
        for name, start, _, _ in REGIONS:
            with self.subTest(region=name):
                damaged = bytearray(original)
                damaged[start] ^= 1
                result = inspect_eeprom(bytes(damaged))
                self.assertFalse(result["all_three_crc_regions_match"])
                self.assertEqual(
                    [check["region"] for check in result["crc_checks"] if not check["matches"]],
                    [name],
                )

    def test_damage_in_stored_crc_is_detected(self) -> None:
        damaged = bytearray(self.all_ff_with_known_crcs())
        damaged[0xF6] ^= 1
        self.assertFalse(inspect_eeprom(bytes(damaged))["crc_checks"][2]["matches"])

    def test_short_and_long_dumps_are_rejected_without_padding(self) -> None:
        for size in (0, 255, 257, 512):
            with self.subTest(size=size), self.assertRaises(ValueError):
                inspect_eeprom(b"\xFF" * size)

    def test_non_wristband_counter_units_and_manufacturer_five_metre_value(self) -> None:
        dump = bytearray(self.all_ff_with_known_crcs())
        dump[1] = 2
        struct.pack_into("<H", dump, 0xE8, 32)
        struct.pack_into("<H", dump, 0x60, 0x189B)
        result = inspect_eeprom(bytes(dump))
        media = result["tentative_media_fields"]
        self.assertEqual(media["remaining_inches"], 1)
        self.assertEqual(media["remaining_mm"], 25.4)
        self.assertAlmostEqual(media["initial_mm"], 4999.83125)
        self.assertFalse(media["physical_remaining_tape_verified"])
        self.assertFalse(result["source"]["layout_verified_against_physical_dump"])

    def test_revision_three_is_bands_and_never_converted_to_length(self) -> None:
        dump = bytearray(self.all_ff_with_known_crcs())
        dump[1] = 3
        struct.pack_into("<H", dump, 0xE8, 37)
        media = inspect_eeprom(bytes(dump))["tentative_media_fields"]
        self.assertEqual(media["remaining_bands"], 37)
        self.assertEqual(media["remaining_unit_from_public_engine"], "bands")
        self.assertIsNone(media["remaining_inches"])
        self.assertIsNone(media["remaining_mm"])
        self.assertIsNone(media["initial_mm"])


if __name__ == "__main__":
    unittest.main()
