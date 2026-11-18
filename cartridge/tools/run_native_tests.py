"""Compile/run the authored pure C++ models with synthetic EEPROM fixtures.

No captured cartridge identity, target firmware, toolchain binary, GPIO, serial,
printer socket, flash write or network operation is used by this test runner.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from inspect_eeprom import REGIONS, crc16_x25  # noqa: E402


def fixture(remaining: int) -> bytes:
    image = bytearray((i * 37) & 255 for i in range(256))
    image[1] = 2
    image[0x60:0x62] = (6299).to_bytes(2, "little")
    image[0xE8:0xEA] = remaining.to_bytes(2, "little")
    for _, start, length, target in REGIONS:
        image[target:target + 2] = crc16_x25(image[start:start + length]).to_bytes(2, "little")
    return bytes(image)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compiler", default=os.environ.get("CXX", "g++"))
    args = parser.parse_args()
    output = ROOT / ".build/native-tests"
    output.mkdir(parents=True, exist_ok=True)
    inputs = []
    for name, remaining in (("before", 4), ("restored", 6299), ("consumed", 6256)):
        path = output / ("synthetic-" + name + ".bin")
        path.write_bytes(fixture(remaining))
        inputs.append(str(path))
    for source in sorted((ROOT / "tests").glob("test_v*.cpp")):
        executable = output / source.stem
        subprocess.run([args.compiler, "-std=c++17", "-O2", "-g", "-Wall", "-Wextra",
                        "-Werror", "-pedantic", "-fsanitize=address,undefined",
                        "-fno-omit-frame-pointer", str(source), "-o", str(executable)], check=True)
        extra = inputs[:1] if source.stem == "test_v5_bench" else (
            inputs[1:] if source.stem == "test_v6_model" else [])
        subprocess.run([str(executable), *extra], check=True)
    print("PASS: all native models; synthetic fixtures only, peripheral timing remains unqualified")


if __name__ == "__main__":
    main()
