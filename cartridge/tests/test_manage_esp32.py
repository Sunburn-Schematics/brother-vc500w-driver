from __future__ import annotations

import hashlib
import io
import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import manage_esp32 as manager

SECRETS = manager.Secrets("private-network", "wifi-password", "ota-password-very-private")
STATUS = {
    "type": "status",
    "firmware": manager.FIRMWARE,
    "pins_input_only": True,
    "internal_pulls": False,
    "cpu_mhz": 240,
    "sda_gpio": 21,
    "scl_gpio": 22,
    "armed": False,
}
NETWORK = {"type": "netinfo", "armed": False, "update_busy": False, "max_app_bytes": 2097152}


def image_bytes() -> bytes:
    data = bytearray(2048)
    data[0], data[1] = 0xE9, 1
    struct.pack_into("<I", data, 0x20, 0xABCD5432)
    return bytes(data)


def image(path: Path = Path("not-opened.bin")) -> manager.AppImage:
    data = image_bytes()
    return manager.AppImage(path, data, hashlib.sha256(data).hexdigest())


def response(**fields: object) -> bytes:
    return json.dumps(fields).encode("utf-8") + b"\n"


def ready(app: manager.AppImage, **changes: object) -> bytes:
    data = {
        "type": "update_ready",
        "bytes": len(app.data),
        "sha256": app.sha256,
        "idle_timeout_ms": 15000,
        "total_timeout_ms": 180000,
        "chunk_bytes": 1024,
        **changes,
    }
    return response(**data)


def progress(app: manager.AppImage) -> bytes:
    return b"".join(
        response(type="update_progress", bytes=min(offset + 1024, len(app.data)))
        for offset in range(0, len(app.data), 1024)
    )


def complete(app: manager.AppImage, **changes: object) -> bytes:
    return response(
        **{
            "type": "update_result",
            "ok": True,
            "bytes": len(app.data),
            "sha256": app.sha256,
            "reboot": True,
            **changes,
        }
    )


class MemoryUART:
    def __init__(self, incoming: bytes = b"", write_limit: int = 4096) -> None:
        self.incoming = bytearray(incoming)
        self.outgoing = bytearray()
        self.write_limit = write_limit

    def read(self, size: int = 1) -> bytes:
        result = bytes(self.incoming[:size])
        del self.incoming[:size]
        return result

    def write(self, data: bytes) -> int:
        count = min(len(data), self.write_limit)
        self.outgoing.extend(data[:count])
        return count

    def close(self) -> None:
        pass


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        self.now += 0.0001
        return self.now


def session(
    incoming: bytes = b"", write_limit: int = 4096
) -> tuple[manager.Session, MemoryUART, io.StringIO]:
    uart, log = MemoryUART(incoming, write_limit), io.StringIO()
    return manager.Session(uart, manager.Audit(log, SECRETS), Clock()), uart, log


class ManagementTests(unittest.TestCase):
    def test_wifi_hex_framing_handles_spaces_unicode_and_open_network(self) -> None:
        secret = manager.Secrets("lab café", "", "long-private-ota-password")
        command = secret.wifi_command().decode("ascii").split()
        self.assertEqual(command, ["WIFI_SET", "lab café".encode().hex(), "-", secret.ota_md5])
        self.assertTrue(secret.wifi_command().endswith(b"\n"))

    def test_wifi_success_does_not_log_raw_command_or_password(self) -> None:
        client, uart, log = session(response(type="wifi_saved", ok=True, reboot=True))
        self.assertEqual(client.wifi_set(SECRETS)["status"], "WIFI_SAVED_REBOOT_REQUESTED")
        self.assertEqual(bytes(uart.outgoing), SECRETS.wifi_command())
        for secret in (
            SECRETS.ssid,
            SECRETS.password,
            SECRETS.ota_password,
            SECRETS.ota_md5,
            SECRETS.ssid.encode().hex(),
            SECRETS.password.encode().hex(),
        ):
            self.assertNotIn(secret, log.getvalue())

    def test_wifi_refuses_unconfirmed_reboot(self) -> None:
        client, _, _ = session(response(type="wifi_saved", ok=True, reboot=False))
        with self.assertRaises(manager.ManagementError):
            client.wifi_set(SECRETS)

    def test_redaction_preserves_json_with_quote_and_newline_secrets(self) -> None:
        log = io.StringIO()
        secret = manager.Secrets('lab"\nroom', "wifi-secret", "sensitive-ota-password")
        manager.Audit(log, secret).record(
            "safe", nested={"value": secret.ssid, "pass": secret.password}
        )
        record = json.loads(log.getvalue())
        self.assertEqual(record["nested"]["value"], "[REDACTED]")
        self.assertNotIn("wifi-secret", log.getvalue())

    def test_probe_checks_revision_pin_configuration_and_idle_state(self) -> None:
        client, uart, _ = session(response(**STATUS) + response(**NETWORK))
        self.assertEqual(client.probe(), NETWORK)
        self.assertEqual(bytes(uart.outgoing), b"INFO\nNETINFO\n")
        for change in (
            {"firmware": "i2c-passive-edge-v1"},
            {"pins_input_only": False},
            {"sda_gpio": 22},
        ):
            broken, _, _ = session(response(**{**STATUS, **change}))
            with self.assertRaises(manager.ManagementError):
                broken.probe()

    def test_probe_refuses_active_capture_or_unknown_update_state(self) -> None:
        for change in ({"armed": True}, {"update_busy": True}, {"update_busy": None}):
            client, _, _ = session(response(**STATUS) + response(**{**NETWORK, **change}))
            with self.assertRaises(manager.ManagementError):
                client.probe()

    def test_default_revision_pin_rejects_future_firmware(self) -> None:
        client, uart, _ = session(response(**{**STATUS, "firmware": "i2c-passive-rmt-v3"}))
        with self.assertRaisesRegex(manager.ManagementError, "Unexpected firmware"):
            client.probe()
        self.assertEqual(bytes(uart.outgoing), b"INFO\n")

    def test_explicit_future_revision_pin_requires_exact_match(self) -> None:
        expected = "i2c-passive-rmt-v3"
        for actual, passes in (
            (expected, True),
            (manager.FIRMWARE, False),
            (expected + "-other", False),
        ):
            uart = MemoryUART(response(**{**STATUS, "firmware": actual}) + response(**NETWORK))
            client = manager.Session(
                uart, manager.Audit(io.StringIO()), Clock(), expected_firmware=expected
            )
            if passes:
                self.assertEqual(client.probe(), NETWORK)
            else:
                with self.assertRaisesRegex(manager.ManagementError, "Unexpected firmware"):
                    client.probe()

    def test_revision_pin_cannot_be_empty_pattern_or_command(self) -> None:
        for pin in ("", "*", "i2c-passive-*", "INFO\nUPDATE", "revision with spaces", "x" * 65):
            with self.assertRaisesRegex(manager.ManagementError, "exact revision"):
                manager.Session(MemoryUART(), manager.Audit(io.StringIO()), expected_firmware=pin)

    def test_explicit_revision_pin_preserves_input_and_idle_guards(self) -> None:
        expected = "i2c-passive-rmt-v3"
        for status_change, network_change in (
            ({"pins_input_only": False}, {}),
            ({}, {"armed": True}),
            ({}, {"update_busy": True}),
        ):
            uart = MemoryUART(
                response(**{**STATUS, "firmware": expected, **status_change})
                + response(**{**NETWORK, **network_change})
            )
            client = manager.Session(
                uart, manager.Audit(io.StringIO()), Clock(), expected_firmware=expected
            )
            with self.assertRaises(manager.ManagementError):
                client.probe()

    def test_exact_update_framing_with_partial_transport_writes(self) -> None:
        app = image()
        client, uart, _ = session(ready(app) + progress(app) + complete(app), write_limit=53)
        result = client.update(app, NETWORK)
        self.assertEqual(result["status"], "USB_UPDATE_VERIFIED_REBOOT_REQUESTED")
        self.assertEqual(
            bytes(uart.outgoing), f"UPDATE {len(app.data)} {app.sha256}\n".encode() + app.data
        )

    def test_readiness_mismatch_never_sends_app_data(self) -> None:
        app = image()
        for change in (
            {"bytes": len(app.data) + 1},
            {"sha256": "0" * 64},
            {"idle_timeout_ms": 10},
            {"total_timeout_ms": 0},
            {"chunk_bytes": 4096},
        ):
            client, uart, _ = session(ready(app, **change))
            with self.assertRaises(manager.ManagementError):
                client.update(app, NETWORK)
            self.assertEqual(
                bytes(uart.outgoing), f"UPDATE {len(app.data)} {app.sha256}\n".encode()
            )

    def test_completion_mismatch_fails_without_resend(self) -> None:
        app = image()
        for change in ({"bytes": 1}, {"sha256": "f" * 64}, {"ok": False}, {"reboot": False}):
            client, uart, _ = session(ready(app) + progress(app) + complete(app, **change))
            with self.assertRaises(manager.ManagementError):
                client.update(app, NETWORK)
            self.assertEqual(bytes(uart.outgoing).count(b"UPDATE "), 1)
            self.assertTrue(bytes(uart.outgoing).endswith(app.data))

    def test_wrong_chunk_ack_blocks_the_next_chunk_without_retry(self) -> None:
        app = image()
        client, uart, _ = session(ready(app) + response(type="update_progress", bytes=1023))
        with self.assertRaisesRegex(manager.ManagementError, "acknowledgement"):
            client.update(app, NETWORK)
        self.assertEqual(
            bytes(uart.outgoing),
            f"UPDATE {len(app.data)} {app.sha256}\n".encode() + app.data[:1024],
        )

    def test_missing_chunk_ack_blocks_the_next_chunk_without_retry(self) -> None:
        app = image()
        client, uart, _ = session(ready(app))
        with self.assertRaisesRegex(manager.ManagementError, "timed out"):
            client.update(app, NETWORK)
        self.assertEqual(
            bytes(uart.outgoing),
            f"UPDATE {len(app.data)} {app.sha256}\n".encode() + app.data[:1024],
        )

    def test_final_partial_chunk_uses_exact_cumulative_ack(self) -> None:
        data = image_bytes() + b"tail"
        app = manager.AppImage(Path("unused"), data, hashlib.sha256(data).hexdigest())
        client, uart, _ = session(ready(app) + progress(app) + complete(app))
        client.update(app, NETWORK)
        self.assertEqual(
            bytes(uart.outgoing), f"UPDATE {len(app.data)} {app.sha256}\n".encode() + data
        )

    def test_partition_bound_is_checked_before_any_update_command(self) -> None:
        client, uart, _ = session()
        with self.assertRaises(manager.ManagementError):
            client.update(image(), {**NETWORK, "max_app_bytes": 1024})
        self.assertEqual(bytes(uart.outgoing), b"")

    def test_missing_response_is_bounded_and_not_retried(self) -> None:
        client, uart, _ = session()
        with self.assertRaisesRegex(manager.ManagementError, "timed out"):
            client.update(image(), NETWORK)
        self.assertEqual(bytes(uart.outgoing).count(b"UPDATE "), 1)

    def test_device_error_never_echoes_secrets_to_exception_or_audit(self) -> None:
        client, _, log = session(response(type="error", message=SECRETS.password))
        with self.assertRaises(manager.ManagementError) as failure:
            client.receive("netinfo", 1)
        self.assertNotIn(SECRETS.password, str(failure.exception) + log.getvalue())

    def test_boot_chatter_allowed_but_wrong_json_frame_rejected(self) -> None:
        client, _, _ = session(b"boot chatter\n" + response(type="netinfo"))
        self.assertEqual(client.receive("netinfo", 1), {"type": "netinfo"})
        wrong, _, _ = session(response(type="status"))
        with self.assertRaises(manager.ManagementError):
            wrong.receive("netinfo", 1)

    def test_oversized_frame_is_rejected(self) -> None:
        client, _, _ = session(b"x" * (manager.MAX_LINE_BYTES + 1))
        with self.assertRaisesRegex(manager.ManagementError, "framing"):
            client.receive("netinfo", 5)

    def test_zero_length_write_fails_without_looping(self) -> None:
        client, _, _ = session(write_limit=0)
        with self.assertRaises(manager.ManagementError):
            client.command("INFO")

    def test_exact_com_vid_pid_and_optional_serial_guard(self) -> None:
        known = SimpleNamespace(device="COM49", vid=0x10C4, pid=0xEA60, serial_number="known")
        self.assertIs(manager.validate_device("com49", [known], "known"), known)
        for port, candidates, serial in (
            ("COM4", [known], None),
            ("COM49", [known, known], None),
            ("COM49", [known], "other"),
            ("/dev/ttyUSB0", [known], None),
            ("COM49", [SimpleNamespace(device="COM49", vid=1, pid=2)], None),
        ):
            with self.assertRaises(manager.ManagementError):
                manager.validate_device(port, candidates, serial)

    def test_app_pin_header_target_and_size_are_validated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app.bin"
            data = image_bytes()
            path.write_bytes(data)
            self.assertEqual(
                manager.AppImage.load(path, hashlib.sha256(data).hexdigest()).data, data
            )
            with self.assertRaises(manager.ManagementError):
                manager.AppImage.load(path, "0" * 64)
            for offset, value in ((0, 0), (1, 0), (12, 5), (0x20, 0)):
                altered = bytearray(data)
                altered[offset] = value
                path.write_bytes(altered)
                with self.assertRaises(manager.ManagementError):
                    manager.AppImage.load(path, hashlib.sha256(altered).hexdigest())
            path.write_bytes(b"x" * 500)
            with self.assertRaises(manager.ManagementError):
                manager.AppImage.load(path, hashlib.sha256(path.read_bytes()).hexdigest())

    @unittest.skipIf(os.name == "nt", "POSIX mode test")
    def test_private_file_guard_rejects_public_permissions_and_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secrets.json"
            path.write_text(
                json.dumps(
                    {
                        "ssid": SECRETS.ssid,
                        "password": SECRETS.password,
                        "ota_password": SECRETS.ota_password,
                    }
                )
            )
            path.chmod(0o600)
            self.assertEqual(manager.load_secrets(path).ota_md5, SECRETS.ota_md5)
            path.chmod(0o644)
            with self.assertRaises(manager.ManagementError):
                manager.load_secrets(path)
            link = Path(directory) / "link.json"
            link.symlink_to(path)
            with self.assertRaises(manager.ManagementError):
                manager.load_secrets(link)

    def test_ota_import_api_uses_frozen_bytes_and_hides_password(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "espota.py"
            script.write_bytes(b"inspected vendor module placeholder")
            original = Path(directory) / "build.bin"
            app = image(original)
            original.write_bytes(b"changed after validation")
            observed = []

            def serve(*args: object) -> int:
                observed.append(args)
                self.assertEqual(Path(args[5]).read_bytes(), app.data)
                sys.stdout.write(SECRETS.ota_password)
                return 0

            fake = SimpleNamespace(serve=serve, FLASH=0)
            audit_log, output = io.StringIO(), io.StringIO()
            with (
                patch.object(manager, "load_espota_module", return_value=fake),
                manager.contextlib.redirect_stdout(output),
            ):
                result = manager.ota_upload(
                    app,
                    SECRETS,
                    script,
                    "192.0.2.4",
                    "192.0.2.5",
                    32320,
                    manager.Audit(audit_log, SECRETS),
                )
            self.assertEqual(len(observed), 1)
            self.assertEqual(observed[0][4], SECRETS.ota_password)
            self.assertEqual(result["status"], "OTA_UPLOAD_ACCEPTED_BOOT_NOT_YET_VERIFIED")
            self.assertNotIn(SECRETS.ota_password, output.getvalue() + audit_log.getvalue())
            self.assertFalse(Path(observed[0][5]).exists())

    def test_ota_wrong_vendor_script_fails_before_import_or_network(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "espota.py"
            script.write_bytes(b"untrusted code")
            with patch("builtins.exec") as executor:
                with self.assertRaises(manager.ManagementError):
                    manager.ota_upload(
                        image(),
                        SECRETS,
                        script,
                        "192.0.2.4",
                        "192.0.2.5",
                        32320,
                        manager.Audit(io.StringIO(), SECRETS),
                    )
                executor.assert_not_called()

    def test_verified_transport_executes_frozen_source_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "espota.py"
            source = b"PINNED = 42\n"
            script.write_bytes(source)
            with patch.object(manager, "ESPOTA_SHA256", hashlib.sha256(source).hexdigest()):
                loaded = manager.load_espota_module(script)
            self.assertEqual(loaded.PINNED, 42)

    def test_windows_acl_guard_does_not_pass_secret_content_to_process(self) -> None:
        with patch.object(
            manager.subprocess, "run", return_value=SimpleNamespace(stdout='{"private":true}')
        ) as run:
            manager.validate_windows_secret_acl(Path("secrets.json"))
            self.assertNotIn(SECRETS.password, repr(run.call_args))
        with (
            patch.object(
                manager.subprocess, "run", return_value=SimpleNamespace(stdout='{"private":false}')
            ),
            self.assertRaises(manager.ManagementError),
        ):
            manager.validate_windows_secret_acl(Path("secrets.json"))


if __name__ == "__main__":
    unittest.main()
