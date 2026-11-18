"""P53-local v2 UART/OTA updater. Importing this module never opens hardware.

Run through the already authenticated P53 SSH session. Secrets are read from a
private JSON file, never from command arguments. Template: capture_serial.py;
OTA transport: unmodified Espressif Arduino ESP32 2.0.17 tools/espota.py.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import io
import ipaddress
import json
import logging
import os
import re
import stat
import struct
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Protocol

FIRMWARE = "i2c-passive-edge-v2"
ESPOTA_SHA256 = "894d128e8a1e755aab5cdf60b17e1066a817c6234b4271c034fa0edeb0643646"
MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_LINE_BYTES = 4096


class ManagementError(RuntimeError):
    """A bounded failure whose message contains no remote data or credentials."""


@dataclass(frozen=True, repr=False)
class Secrets:
    ssid: str
    password: str
    ota_password: str

    @property
    def ota_md5(self) -> str:
        # ArduinoOTA's authentication contract specifically requires MD5.
        return hashlib.md5(self.ota_password.encode("utf-8")).hexdigest()  # noqa: S324

    def wifi_command(self) -> bytes:
        ssid_hex = self.ssid.encode("utf-8").hex()
        password_hex = self.password.encode("utf-8").hex() or "-"
        return f"WIFI_SET {ssid_hex} {password_hex} {self.ota_md5}\n".encode("ascii")

    def redact(self, value: str) -> str:
        tokens = [self.ssid, self.password, self.ota_password, self.ota_md5]
        tokens += [token.encode("utf-8").hex() for token in tokens if token]
        for token in sorted(set(tokens), key=len, reverse=True):
            if token:
                value = value.replace(token, "[REDACTED]")
        return value


def validate_windows_secret_acl(path: Path) -> None:
    """Read local ACLs only; never place file contents in the PowerShell process."""
    quoted_path = str(path.resolve()).replace("'", "''")
    script = (
        "$ErrorActionPreference='Stop';"
        f"$acl=Get-Acl -LiteralPath '{quoted_path}';"
        "$me=[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value;"
        "$owner=$acl.GetOwner([System.Security.Principal.SecurityIdentifier]).Value;"
        "$allowed=@($me,'S-1-5-18','S-1-5-32-544');"
        "$bad=@($acl.GetAccessRules($true,$true,[System.Security.Principal.SecurityIdentifier])"
        "|Where-Object {$_.AccessControlType -eq 'Allow' -and $_.IdentityReference.Value -notin $allowed});"
        "@{private=($bad.Count -eq 0 -and $owner -in $allowed)}|ConvertTo-Json -Compress"
    )
    try:
        powershell = (
            Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))
            / "System32/WindowsPowerShell/v1.0/powershell.exe"
        )
        response = subprocess.run(  # noqa: S603 -- fixed OS ACL reader; no secret data in argv
            [
                str(powershell),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-EncodedCommand",
                base64.b64encode(script.encode("utf-16-le")).decode("ascii"),
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        if json.loads(response.stdout).get("private") is not True:
            raise ManagementError("Secrets file ACL permits another Windows identity")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise ManagementError("Could not verify private Windows secrets-file ACL") from exc


def load_secrets(path: Path) -> Secrets:
    """Require private owner-only data; administrators/SYSTEM may retain ACLs."""
    if path.is_symlink():
        raise ManagementError("Secrets file must not be a symlink")
    if os.name == "nt":
        validate_windows_secret_acl(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
            raise ManagementError("Secrets file must be a small regular file")
        if os.name != "nt" and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
            raise ManagementError("Secrets file must be owned by this user with mode 0600")
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = -1
            try:
                data = json.load(stream)
            except (ValueError, UnicodeError) as exc:
                raise ManagementError("Secrets file is not valid UTF-8 JSON") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(data, dict) or set(data) != {"ssid", "password", "ota_password"}:
        raise ManagementError("Secrets JSON requires exactly ssid, password, ota_password")
    if not all(isinstance(value, str) for value in data.values()):
        raise ManagementError("Secrets fields must be strings")
    ssid, password, ota_password = data["ssid"], data["password"], data["ota_password"]
    if not 1 <= len(ssid.encode("utf-8")) <= 32 or "\0" in ssid:
        raise ManagementError("SSID must contain 1..32 UTF-8 bytes without NUL")
    password_bytes = password.encode("utf-8")
    if (
        not (
            not password_bytes
            or 8 <= len(password_bytes) <= 63
            or re.fullmatch(r"[0-9a-fA-F]{64}", password)
        )
        or "\0" in password
    ):
        raise ManagementError("Wi-Fi password has an unsupported length")
    if not 16 <= len(ota_password.encode("utf-8")) <= 128 or "\0" in ota_password:
        raise ManagementError("OTA password must contain 16..128 UTF-8 bytes without NUL")
    return Secrets(ssid, password, ota_password)


@dataclass(frozen=True)
class AppImage:
    path: Path
    data: bytes = field(repr=False)
    sha256: str

    @property
    def elf_sha256(self) -> str:
        return self.data[0xB0:0xD0].hex()

    @classmethod
    def load(cls, path: Path, expected_sha256: str) -> AppImage:
        if not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256):
            raise ManagementError("An exact compiled-app SHA256 pin is required")
        with path.open("rb") as stream:
            data = stream.read(MAX_IMAGE_BYTES + 1)
        digest = hashlib.sha256(data).hexdigest()
        if digest != expected_sha256.lower():
            raise ManagementError("App image SHA256 does not match the compiled-app pin")
        if not 1024 <= len(data) <= MAX_IMAGE_BYTES:
            raise ManagementError("App image must contain 1024..4194304 bytes")
        if data[0] != 0xE9 or not 1 <= data[1] <= 16:
            raise ManagementError("File is not an ESP application image")
        if struct.unpack_from("<H", data, 12)[0] != 0:
            raise ManagementError("App image targets a different ESP32 chip family")
        if struct.unpack_from("<I", data, 0x20)[0] != 0xABCD5432:
            raise ManagementError("Image is not an application with an ESP app descriptor")
        return cls(path, data, digest)


class UART(Protocol):
    def read(self, size: int = 1) -> bytes: ...
    def write(self, data: bytes) -> int: ...
    def close(self) -> None: ...


class Audit:
    def __init__(self, stream: io.TextIOBase, secrets: Secrets | None = None) -> None:
        self.stream = stream
        self.secrets = secrets

    def record(self, event: str, **fields: object) -> None:
        encoded = json.dumps(self.safe({"event": event, **fields}), ensure_ascii=True)
        self.stream.write(encoded + "\n")
        self.stream.flush()

    def safe(self, value: object) -> object:
        if isinstance(value, str) and self.secrets is not None:
            return self.secrets.redact(value)
        if isinstance(value, dict):
            return {key: self.safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.safe(item) for item in value]
        return value


class Session:
    def __init__(
        self,
        uart: UART,
        audit: Audit,
        clock: Callable[[], float] = time.monotonic,
        *,
        expected_firmware: str = FIRMWARE,
    ) -> None:
        if (
            not isinstance(expected_firmware, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", expected_firmware) is None
        ):
            raise ManagementError("Expected firmware must be one exact revision identifier")
        self.uart, self.audit, self.clock = uart, audit, clock
        self.expected_firmware = expected_firmware

    def write(self, data: bytes, deadline: float) -> None:
        offset = 0
        while offset < len(data):
            if self.clock() >= deadline:
                raise ManagementError("UART transfer deadline expired; do not retry blindly")
            sent = self.uart.write(data[offset : offset + 4096])
            if not isinstance(sent, int) or not 0 < sent <= min(4096, len(data) - offset):
                raise ManagementError("UART transfer was incomplete; do not retry blindly")
            offset += sent

    def command(self, name: str, payload: bytes | None = None) -> None:
        self.audit.record("uart_command", command=name)
        self.write(
            payload if payload is not None else (name + "\n").encode("ascii"), self.clock() + 5
        )

    def receive(self, kind: str, timeout: float) -> dict[str, object]:
        deadline, line = self.clock() + timeout, bytearray()
        while self.clock() < deadline:
            chunk = self.uart.read(1)
            if not chunk:
                continue
            line.extend(chunk)
            if len(line) > MAX_LINE_BYTES:
                raise ManagementError("UART response exceeded the framing bound")
            if chunk != b"\n":
                continue
            try:
                result = json.loads(line)
            except (ValueError, UnicodeError):
                self.audit.record("uart_boot_chatter", bytes=len(line))
                line.clear()
                continue
            line.clear()
            if not isinstance(result, dict):
                raise ManagementError("UART response was not a JSON object")
            response_type = result.get("type")
            if response_type == "error" or result.get("ok") is False:
                self.audit.record("uart_device_error")
                raise ManagementError("Device rejected the operation; no automatic retry")
            if response_type != kind:
                self.audit.record("uart_unexpected_response")
                raise ManagementError("Unexpected UART response type; no automatic retry")
            self.audit.record("uart_response", response_type=kind)
            return result
        raise ManagementError("Expected UART response timed out; no automatic retry")

    def probe(self) -> dict[str, object]:
        self.command("INFO")
        status = self.receive("status", 5)
        if (
            status.get("firmware") != self.expected_firmware
            or status.get("pins_input_only") is not True
            or status.get("internal_pulls") is not False
            or status.get("cpu_mhz") != 240
            or status.get("sda_gpio") != 21
            or status.get("scl_gpio") != 22
        ):
            raise ManagementError("Unexpected firmware or input-only capture configuration")
        self.command("NETINFO")
        network = self.receive("netinfo", 5)
        if status.get("armed") is not False or network.get("armed") is not False:
            raise ManagementError("Capture is armed; stop it explicitly before management")
        if network.get("update_busy") is not False:
            raise ManagementError("Another update is active or update state is unknown")
        return network

    def wifi_set(self, secrets: Secrets) -> dict[str, object]:
        self.command("WIFI_SET", secrets.wifi_command())
        result = self.receive("wifi_saved", 10)
        if result.get("ok") is not True or result.get("reboot") is not True:
            raise ManagementError("Wi-Fi configuration was not confirmed with a reboot")
        return {"status": "WIFI_SAVED_REBOOT_REQUESTED"}

    def update(self, image: AppImage, network: dict[str, object]) -> dict[str, object]:
        capacity = network.get("max_app_bytes")
        if type(capacity) is not int or not 1024 <= len(image.data) <= capacity:
            raise ManagementError("Compiled app does not fit the reported next OTA partition")
        self.command("UPDATE", f"UPDATE {len(image.data)} {image.sha256}\n".encode("ascii"))
        ready = self.receive("update_ready", 10)
        if (
            type(ready.get("bytes")) is not int
            or ready.get("bytes") != len(image.data)
            or ready.get("sha256") != image.sha256
            or ready.get("idle_timeout_ms") != 15000
            or ready.get("total_timeout_ms") != 180000
            or ready.get("chunk_bytes") != 1024
        ):
            raise ManagementError("Update readiness does not match exact app framing")
        deadline = self.clock() + 180
        for offset in range(0, len(image.data), 1024):
            chunk = image.data[offset : offset + 1024]
            self.write(chunk, deadline)
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise ManagementError("UART update exceeded the total deadline")
            progress = self.receive("update_progress", min(15, remaining))
            if type(progress.get("bytes")) is not int or progress.get("bytes") != offset + len(
                chunk
            ):
                raise ManagementError("Update chunk acknowledgement did not match cumulative bytes")
        self.audit.record("uart_binary_sent", bytes=len(image.data), sha256=image.sha256)
        result = self.receive("update_result", min(15, deadline - self.clock()))
        if (
            result.get("ok") is not True
            or type(result.get("bytes")) is not int
            or result.get("bytes") != len(image.data)
            or result.get("sha256") != image.sha256
            or result.get("reboot") is not True
        ):
            raise ManagementError("Firmware update completion was not verified")
        return {
            "status": "USB_UPDATE_VERIFIED_REBOOT_REQUESTED",
            "bytes": len(image.data),
            "sha256": image.sha256,
            "expected_app_elf_sha256": image.elf_sha256,
        }


def validate_device(port: str, candidates: list[object], serial_number: str | None) -> object:
    windows = bool(re.fullmatch(r"COM[1-9][0-9]*", port, re.IGNORECASE))
    posix = port.startswith("/dev/") and not any(c in port for c in "\x00\r\n")
    if not windows and not posix:
        raise ManagementError("An exact COM port or absolute /dev serial path is required")
    def same_port(candidate: str) -> bool:
        return candidate.casefold() == port.casefold() if windows else (
            str(Path(candidate).resolve()) == str(Path(port).resolve())
        )
    matches = [
        item
        for item in candidates
        if same_port(str(getattr(item, "device", "")))
    ]
    if len(matches) != 1:
        raise ManagementError("Exact serial port was not uniquely identified; no fallback")
    device = matches[0]
    if (getattr(device, "vid", None), getattr(device, "pid", None)) != (0x10C4, 0xEA60):
        raise ManagementError("Named serial port is not the expected CP210x device")
    if serial_number is not None and getattr(device, "serial_number", None) != serial_number:
        raise ManagementError("CP210x serial number does not match")
    return device


def load_espota_module(script: Path) -> ModuleType:
    with script.open("rb") as stream:
        source = stream.read(131073)
    if len(source) > 131072 or hashlib.sha256(source).hexdigest() != ESPOTA_SHA256:
        raise ManagementError("espota.py does not match inspected Espressif 2.0.17 source")
    module = ModuleType("sunburn_pinned_espota")
    module.__file__ = str(script)
    # Execute exactly the verified in-memory source, without rereading a mutable path.
    exec(compile(source, str(script), "exec"), module.__dict__)  # noqa: S102 -- pinned vendor API
    return module


def ota_upload(
    image: AppImage,
    secrets: Secrets,
    script: Path,
    target: str,
    local_ip: str,
    host_port: int,
    audit: Audit,
) -> dict[str, object]:
    ipaddress.IPv4Address(target)
    ipaddress.IPv4Address(local_ip)
    if not 1024 <= host_port <= 65535:
        raise ManagementError("OTA host TCP port must be 1024..65535")
    module = load_espota_module(script)
    module.TIMEOUT, module.PROGRESS = 3, False
    # Keep auth in process memory, never in argv or a subprocess environment.
    # Vendor transport has invitation retries; no wrapper retry or image resend.
    previous_logging = logging.root.manager.disable
    try:
        logging.disable(logging.CRITICAL)
        with (
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
            tempfile.TemporaryDirectory(prefix="sunburn-espota-") as temporary,
        ):
            # espota reopens its filename; use immutable validated bytes rather
            # than the original build path, which another build could replace.
            frozen_path = Path(temporary) / "verified-app.bin"
            frozen_path.write_bytes(image.data)
            try:
                result = module.serve(
                    target,
                    local_ip,
                    3232,
                    host_port,
                    secrets.ota_password,
                    str(frozen_path),
                    module.FLASH,
                )
            except (Exception, SystemExit) as exc:
                raise ManagementError("OTA transport failed; no automatic retry") from exc
    finally:
        logging.disable(previous_logging)
    if result != 0:
        raise ManagementError("OTA transport did not confirm success; no automatic retry")
    audit.record("ota_transport_accepted", bytes=len(image.data), sha256=image.sha256)
    return {
        "status": "OTA_UPLOAD_ACCEPTED_BOOT_NOT_YET_VERIFIED",
        "bytes": len(image.data),
        "sha256": image.sha256,
        "expected_app_elf_sha256": image.elf_sha256,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("netinfo", "wifi-set", "update", "ota"))
    parser.add_argument("--port")
    parser.add_argument("--serial-number")
    parser.add_argument(
        "--expect-firmware",
        default=FIRMWARE,
        help=f"Exact running firmware revision required for UART actions (default: {FIRMWARE})",
    )
    parser.add_argument("--secrets", "--auth-file", dest="secrets", type=Path)
    parser.add_argument("--app", type=Path)
    parser.add_argument("--sha256")
    parser.add_argument("--audit", required=True, type=Path)
    parser.add_argument("--espota-script", type=Path)
    parser.add_argument("--target")
    parser.add_argument("--local-ip")
    parser.add_argument("--host-port", type=int, default=32320)
    args = parser.parse_args()
    if args.action != "ota" and not args.port:
        parser.error("UART actions require --port with the exact P53 COM device")
    if args.action in {"wifi-set", "ota"} and args.secrets is None:
        parser.error("This action requires a private --secrets/--auth-file JSON file")
    if args.action in {"update", "ota"} and (args.app is None or args.sha256 is None):
        parser.error("This action requires --app and its exact compiled --sha256")
    if args.action == "ota" and (
        args.espota_script is None or args.target is None or args.local_ip is None
    ):
        parser.error("OTA requires --espota-script, --target and the exact --local-ip")
    secrets = load_secrets(args.secrets) if args.secrets else None
    image = AppImage.load(args.app, args.sha256) if args.app else None
    with args.audit.open("x", encoding="utf-8") as stream:
        audit = Audit(stream, secrets)
        if args.action == "ota":
            if image is None or secrets is None:
                raise ManagementError("OTA image or credentials were not loaded")
            result = ota_upload(
                image,
                secrets,
                args.espota_script,
                args.target,
                args.local_ip,
                args.host_port,
                audit,
            )
        else:
            import serial
            from serial.tools import list_ports

            device = validate_device(args.port, list(list_ports.comports()), args.serial_number)
            audit.record("device_verified", port=device.device, vid=device.vid, pid=device.pid)
            uart = serial.Serial(port=None, baudrate=921600, timeout=0.2, write_timeout=5)
            uart.dtr = False
            uart.rts = False
            uart.port = args.port
            try:
                uart.open()
                time.sleep(1.5)
                uart.reset_input_buffer()
                session = Session(uart, audit, expected_firmware=args.expect_firmware)
                network = session.probe()
                if args.action == "netinfo":
                    # Explicit allowlist: unknown future fields never reach logs/stdout.
                    keys = {
                        "configured",
                        "connected",
                        "ip",
                        "hostname",
                        "ota_port",
                        "ota_enabled",
                        "update_busy",
                        "armed",
                        "max_app_bytes",
                        "free_heap",
                        "min_free_heap",
                        "largest_internal_block",
                        "buffer_ready",
                        "running_label",
                        "running_address",
                        "app_elf_sha256",
                    }
                    result = {
                        "status": "NETINFO",
                        **{key: value for key, value in network.items() if key in keys},
                    }
                elif args.action == "wifi-set":
                    if secrets is None:
                        raise ManagementError("Wi-Fi credentials were not loaded")
                    result = session.wifi_set(secrets)
                else:
                    if image is None:
                        raise ManagementError("Update app was not loaded")
                    result = session.update(image, network)
            finally:
                uart.close()
        audit.record("operation_complete", **result)
        sys.stdout.write(json.dumps(audit.safe(result)) + "\n")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # No traceback or arbitrary exception text can expose device-echoed secrets.
        message = (
            str(error) if isinstance(error, ManagementError) else "Management operation failed"
        )
        sys.stderr.write(
            json.dumps({"status": "FAILED", "message": message, "error_type": type(error).__name__})
            + "\n"
        )
        raise SystemExit(1) from None
