"""Offline V6 artifact/preservation audit; no UART, network or device access.

Template: V5 source-preservation and build-freeze JSON receipts.
"""
from pathlib import Path
import hashlib
import json
import re
import subprocess

B = Path(__file__).resolve().parent.parent
CURRENT = B / "firmware/eeprom_emulator_v6"
OLD = B / "firmware/isolated_bench_i2c"
TOOLS = B / "toolchain/data/packages/esp32/tools/xtensa-esp32-elf-gcc/esp-2021r2-patch5-8.4.0/bin"
ELF = B / "build-v6/eeprom_emulator_v6.ino.elf"
APP = B / "build-v6/eeprom_emulator_v6.ino.bin"


def digest(value):
    return hashlib.sha256(value).hexdigest()


def function_body(source, name):
    match = re.search(r"(?m)^static [^\n]*?\b" + re.escape(name) + r"\([^;]*?\)\s*\{", source)
    if not match:
        raise RuntimeError("Missing function: " + name)
    start = match.end() - 1
    depth, state, escaped = 0, "code", False
    for i in range(start, len(source)):
        c = source[i]
        n = source[i:i + 2]
        if state in {"string", "char"}:
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == ('"' if state == "string" else "'"):
                state = "code"
        elif state == "line":
            if c == "\n":
                state = "code"
        elif state == "block":
            if source[i - 1:i + 1] == "*/":
                state = "code"
        elif n == "//":
            state = "line"
        elif n == "/*":
            state = "block"
        elif c == '"':
            state = "string"
        elif c == "'":
            state = "char"
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if not depth:
                return source[start:i + 1].encode()
    raise RuntimeError("Unbalanced function: " + name)


old_source = (OLD / "isolated_bench_i2c.ino").read_text()
new_source = (CURRENT / "eeprom_emulator_v6.ino").read_text()
names = ["crc32", "commandError", "appCapacity", "networkInfo", "pauseNetworking",
         "resumeNetworking", "serviceNetworking", "configureOTA", "loadNetwork",
         "provisionNetwork", "clearNetwork", "failUsbUpdate", "usbUpdate", "processCommand", "pollWindow"]
preserved = {}
for name in names:
    old, new = function_body(old_source, name), function_body(new_source, name)
    assert old == new, name + " changed"
    preserved[name] = {"unchanged": True, "sha256": digest(new)}
headers = {}
for name in ["control_rules.h", "poll_rules.h", "bench_rules.h", "bench_commands.h"]:
    assert (OLD / name).read_bytes() == (CURRENT / name).read_bytes(), name + " changed"
    headers[name] = True
preservation = {
    "template_source": "Frozen independently reviewed V5 isolated_bench_i2c",
    "management_functions_and_pollWindow": preserved,
    "copied_headers_unchanged": headers,
    "intentional_changes": ["Distinct V6 revision/capability and new emulator headers",
                            "Passive boot loads saved image but never attaches",
                            "New emulator dispatcher before preserved bench/legacy dispatchers",
                            "Session-open guards around automatic networking/capture-stop loop branches"],
    "hardware_actions": False,
}
preservation_path = B / "toolchain/v6-source-preservation.json"
preservation_path.write_text(json.dumps(preservation, indent=2) + "\n")

nm = subprocess.check_output([str(TOOLS / "xtensa-esp32-elf-nm"), "-S", "-C", str(ELF)], text=True)
symbols = [line for line in nm.splitlines() if re.search(r"emu|eeprom_model|pollWindow|captureMux| edges$", line)]
iram_names = ["emuDetachFast()", "emuFault(unsigned int)", "emuFillTx()", "emuPrimeTx()",
              "emuFinishRead()", "emuService()", "emuIsr(void*)", "eeprom_model::Device::stop(bool)",
              "pollWindow(SamplingWindow&, unsigned int)"]
for name in iram_names:
    matches = [line for line in symbols if line.endswith(" " + name)]
    assert len(matches) == 1, name + " missing/ambiguous"
    address = int(matches[0].split()[0], 16)
    assert 0x40080000 <= address < 0x400A0000, name + " outside IRAM"
disassembly = subprocess.check_output([str(TOOLS / "xtensa-esp32-elf-objdump"), "-d", "-C", str(ELF)], text=True)
selected, keep = [], False
for line in disassembly.splitlines():
    match = re.match(r"^([0-9a-f]+) <(.+)>:$", line)
    if match:
        keep = match[2] in iram_names
    if keep:
        selected.append(line)
call_lines = [line for line in selected if re.search(r"\bcall", line)]
for line in call_lines:
    match = re.search(r"\bcall\w*\s+([0-9a-f]+)\s+<", line)
    assert match, "Unresolved ISR call: " + line
    assert 0x40080000 <= int(match[1], 16) < 0x400A0000, "Non-IRAM ISR/poll call: " + line
disassembly_path = B / "toolchain/v6-iram-disassembly.txt"
disassembly_path.write_text("\n".join(selected) + "\n")
elf_hash = digest(ELF.read_bytes())
assert APP.read_bytes()[176:208].hex() == elf_hash, "App embedded ELF hash mismatch"

paths = sorted(CURRENT.glob("*.ino")) + sorted(CURRENT.glob("*.h")) + [CURRENT / "INTERFACE.txt"]
paths += sorted((B / "build-v6").glob("*.bin")) + [ELF]
paths += [B / "toolchain/test_v6_model.cpp", preservation_path, disassembly_path, Path(__file__).resolve()]
artifacts = {str(path.relative_to(B)): {"bytes": path.stat().st_size,
                                      "sha256": digest(path.read_bytes())} for path in paths}
freeze = {
    "status": "AWAITING_INDEPENDENT_REVIEW_FOR_EXPERIMENTAL_HARDWARE_EVALUATION",
    "firmware": "i2c-eeprom-emulator-v6",
    "template_source": "Frozen V5 source/build/interface plus reviewed manufacturer contract and emulator architecture",
    "fqbn": "esp32:esp32:esp32doit-devkit-v1", "core": "esp32:esp32@2.0.17",
    "app_storage_report_bytes": 793933, "static_ram_report_bytes": 58064, "ota_slot_bytes": 1310720,
    "edge_buffer_internal_heap_bytes": 98304, "watchdogs_unchanged": True,
    "hardware_actions": False, "transport_qualified": False,
    "artifacts": artifacts, "embedded_app_elf_sha256": elf_hash,
    "isr_and_poll_symbols": symbols, "isr_and_poll_calls": call_lines,
    "tests": ["Independent native model: C++17/O2 Wall/Wextra/Werror ASan+UBSan PASS, 6 groups/87 assertions",
              "Actual verified restore -> consumed seed 8+8-byte write replay; interim invalid CRC retained",
              "Exact 14 management bodies, pollWindow and 4 predecessor headers preserved",
              "Build succeeds; extracted ISR and poll direct calls all in IRAM; app embeds exact ELF SHA"],
    "seed": {"path": "emulator-seed-after-confirmed-print-01/assembled-emulator-seed-256.bin",
             "sha256": "d7c0db4edb0db564e8980ba26b8ebf6f95082685aa4e0b40c8c8630b4715e091",
             "provenance_sha256": "f50cec4b7074c5b798029ceb3382f9b7e62614db31896eb2369798e75b241850",
             "remaining": 6256, "initial": 6299, "assembled_not_fresh_full_dump": True},
    "qualification_gates": ["Classic ESP32 SLAVE_TRAN_COMP/FIFO/shifter/final-NACK accounting must agree on hardware",
                            "Actual printer immediate repeated-START/full256 startup and 8+8 write/16 readback timing",
                            "Generic short-read and page-edge current-address behavior remains unqualified"],
    "persistence_limit": "Bus ACK is volatile RAM acceptance; clean EMU_STOP detaches before durable NVS save. Unexpected reset boots passive/unclean; no discard or automatic counter reset.",
}
freeze_path = B / "toolchain/v6-build-freeze.json"
freeze_path.write_text(json.dumps(freeze, indent=2) + "\n")
print(json.dumps({"status": freeze["status"], "app": artifacts[str(APP.relative_to(B))],
                  "elf_sha256": elf_hash, "preserved_functions": len(names),
                  "iram_calls": len(call_lines), "freeze": str(freeze_path)}, indent=2))
