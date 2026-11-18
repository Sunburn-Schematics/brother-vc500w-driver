"""Public adapter regressions; fake devices/requests only, never real hardware."""
import io
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

import manage_esp32 as manager
import manage_emulator_v12 as v12
import test_manage_emulator_v12 as original
import test_manage_emulator_v9 as common
from test_bench_eeprom import Tick, valid_image


class ExplicitDeviceTests(unittest.TestCase):
    def device(self, port="/dev/ttyUSB0", serial="example-serial"):
        return SimpleNamespace(device=port, vid=0x10C4, pid=0xEA60, serial_number=serial)

    def test_explicit_posix_cp210x_and_serial_are_required_to_match(self):
        device = self.device()
        self.assertIs(manager.validate_device(device.device, [device], "example-serial"), device)
        for port, serial in (("/dev/ttyUSB1", "example-serial"), (device.device, "other")):
            with self.subTest(port=port, serial=serial), self.assertRaises(manager.ManagementError):
                manager.validate_device(port, [device], serial)

    def test_port_alias_resolves_without_opening_device(self):
        with tempfile.TemporaryDirectory() as directory:
            # This temporary path is used as a mocked enumerated device only.
            from unittest.mock import patch
            with patch.object(Path, "resolve", side_effect=lambda: Path(directory)):
                device = self.device()
                self.assertIs(manager.validate_device("/dev/serial/by-id/example", [device], "example-serial"), device)

    def test_windows_selection_remains_case_insensitive(self):
        device = self.device("COM7")
        self.assertIs(manager.validate_device("com7", [device], "example-serial"), device)

    def test_wrong_usb_chip_or_duplicate_device_never_falls_back(self):
        device = self.device()
        with self.assertRaises(manager.ManagementError):
            manager.validate_device(device.device, [device, device], "example-serial")
        device.vid = 0x1234
        with self.assertRaises(manager.ManagementError):
            manager.validate_device(device.device, [device], "example-serial")

    def test_relative_or_embedded_command_port_rejected(self):
        for port in ("ttyUSB0", "auto", "COM7\nINFO", "/dev/ttyUSB0\nINFO"):
            with self.subTest(port=port), self.assertRaises(manager.ManagementError):
                manager.validate_device(port, [], "example-serial")


class PublicV12Tests(unittest.TestCase):
    """Re-run nine current protocol regressions using generated, non-cartridge bytes.

    This is host parsing/control logic coverage. It does not replace the original
    event-pinned recovery replay or qualify the modified public adapter on hardware.
    """
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)
        self.data = valid_image(remaining=6256)
        self.clean = original.current(self.data)
        self.fault = original.current(self.data, fault=True)

    def control(self, frames, callback=None):
        uart = original.UART(frames, callback)
        session = manager.Session(uart, manager.Audit(io.StringIO()), Tick(), expected_firmware=v12.FIRMWARE)
        return v12.Control(session, self.output, common.ELF, v12.digest(self.data)), uart

    start_frames = original.Tests.start_frames
    test_read_accounting_is_typed_but_irq_count_is_not_a_byte_count = original.Tests.test_read_accounting_is_typed_but_irq_count_is_not_a_byte_count
    test_pending_and_committed_write_bytes_remain_separate_from_image = original.Tests.test_pending_and_committed_write_bytes_remain_separate_from_image
    test_large_record_and_panic_are_retained_before_parsing = original.Tests.test_large_record_and_panic_are_retained_before_parsing
    test_oversized_record_stops_at_16k_and_preserves_partial = original.Tests.test_oversized_record_stops_at_16k_and_preserves_partial
    test_pullup_on_requires_explicit_power_assertion = original.Tests.test_pullup_on_requires_explicit_power_assertion
    test_pullup_off_works_with_fault_unclean_and_optional_snapshot_busy = original.Tests.test_pullup_off_works_with_fault_unclean_and_optional_snapshot_busy
    test_combined_start_is_one_command_with_off_then_on_phases = original.Tests.test_combined_start_is_one_command_with_off_then_on_phases
    test_combined_start_rejects_off_fifo_phase_without_retry = original.Tests.test_combined_start_rejects_off_fifo_phase_without_retry
    test_stop_rejects_a_result_that_left_d19_enabled = original.Tests.test_stop_rejects_a_result_that_left_d19_enabled


if __name__ == "__main__":
    unittest.main()
