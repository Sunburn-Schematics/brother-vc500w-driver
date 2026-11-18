"""Build source with an isolated, version-pinned Arduino installation. Never flash.

Linux x86_64 can bootstrap the checksum-pinned official CLI automatically. Other
hosts can supply the official Arduino CLI 1.6.0 executable with --arduino-cli.
All SDK downloads, configuration, user libraries and build products remain local
to cartridge/.cache and cartridge/.build; no global Arduino configuration changes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLI_VERSION = "1.6.0"
CORE = "esp32:esp32@2.0.17"
FQBN = "esp32:esp32:esp32doit-devkit-v1"
INDEX = "https://espressif.github.io/arduino-esp32/package_esp32_index.json"
CLI_URL = (
    "https://github.com/arduino/arduino-cli/releases/download/v1.6.0/"
    "arduino-cli_1.6.0_Linux_64bit.tar.gz"
)
CLI_SHA256 = "321ee249f44c78053234fec0f36101092bc6021bf24939e15e0cc2449c5ed250"


def cli_executable(explicit: Path | None, install: bool) -> Path:
    if explicit is not None:
        return explicit.resolve(strict=True)
    local = ROOT / ".cache/bin/arduino-cli"
    if local.is_file():
        return local
    found = shutil.which("arduino-cli")
    if found:
        return Path(found).resolve()
    if not install:
        raise RuntimeError("Run make bootstrap or supply --arduino-cli first")
    if platform.system() != "Linux" or platform.machine().lower() not in {"x86_64", "amd64"}:
        raise RuntimeError("Install official Arduino CLI 1.6.0 and supply --arduino-cli on this host")
    local.parent.mkdir(parents=True, exist_ok=True)
    archive = ROOT / ".cache/arduino-cli_1.6.0_Linux_64bit.tar.gz"
    # Pin the release bytes before opening the archive; do not use a shell installer.
    with urllib.request.urlopen(CLI_URL, timeout=60) as response:
        data = response.read(64 * 1024 * 1024 + 1)
    if len(data) > 64 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != CLI_SHA256:
        raise RuntimeError("Official Arduino CLI archive failed the pinned checksum")
    archive.write_bytes(data)
    with tarfile.open(archive, "r:gz") as bundle:
        member = bundle.getmember("arduino-cli")
        stream = bundle.extractfile(member) if member.isfile() else None
        if stream is None:
            raise RuntimeError("CLI archive has no regular arduino-cli executable")
        # Copy one known member; never extract arbitrary archive paths.
        local.write_bytes(stream.read())
    local.chmod(0o755)
    return local


def prepare_config() -> Path:
    cache = ROOT / ".cache"
    cache.mkdir(exist_ok=True)
    config = cache / "arduino-cli.yaml"
    paths = {name: str((cache / name).resolve()) for name in ("data", "downloads", "user")}
    # JSON is valid YAML, and quoting keeps Windows paths and spaces unambiguous.
    config.write_text(json.dumps({"board_manager": {"additional_urls": [INDEX]},
                                  "directories": paths}, indent=2) + "\n")
    return config


def run(arguments: list[str]) -> None:
    subprocess.run(arguments, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--arduino-cli", type=Path)
    parser.add_argument("--sketch", default="eeprom_emulator_v12")
    args = parser.parse_args()
    allowed = {p.name for p in (ROOT / "firmware").iterdir() if p.is_dir()}
    if args.sketch not in allowed:
        parser.error("Choose an included sketch: " + ", ".join(sorted(allowed)))
    cli = cli_executable(args.arduino_cli, args.install)
    version = subprocess.check_output([str(cli), "version", "--format", "json"], text=True)
    if json.loads(version).get("VersionString") != CLI_VERSION:
        raise RuntimeError("Exactly Arduino CLI 1.6.0 is required")
    config = prepare_config()
    command = [str(cli), "--config-file", str(config)]
    if args.install:
        run(command + ["core", "update-index"])
        run(command + ["core", "install", CORE])
        return
    installed = json.loads(subprocess.check_output(command + ["core", "list", "--format", "json"], text=True))
    platforms = installed.get("platforms", []) if isinstance(installed, dict) else installed
    if not any(p.get("id") == "esp32:esp32" and
               p.get("installed_version") == "2.0.17" for p in platforms):
        raise RuntimeError("Run make bootstrap to install the pinned ESP32 2.0.17 core")
    output = ROOT / ".build" / args.sketch
    output.mkdir(parents=True, exist_ok=True)
    run(command + ["compile", "--fqbn", FQBN, "--build-path", str(output),
                   str(ROOT / "firmware" / args.sketch)])
    app = output / (args.sketch + ".ino.bin")
    elf = output / (args.sketch + ".ino.elf")
    receipt = {"sketch": args.sketch, "fqbn": FQBN, "core": CORE,
               "arduino_cli": CLI_VERSION, "application_bytes": app.stat().st_size,
               "app_sha256": hashlib.sha256(app.read_bytes()).hexdigest(),
               "elf_sha256": hashlib.sha256(elf.read_bytes()).hexdigest(),
               "hardware_actions": False, "bit_identical_to_original_experiment_claimed": False}
    (output / "build-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()
