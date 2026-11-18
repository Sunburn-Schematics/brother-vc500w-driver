"""Offline V7 delta/binary audit. Reuses the reviewed V6 audit parser template."""
from pathlib import Path
import ast
import hashlib
import json
import re
import subprocess

B = Path(__file__).resolve().parent.parent
OLD = B / "firmware/eeprom_emulator_v6"
CURRENT = B / "firmware/eeprom_emulator_v7"
TOOLS = B / "toolchain/data/packages/esp32/tools/xtensa-esp32-elf-gcc/esp-2021r2-patch5-8.4.0/bin"
ELF = B / "build-v7/eeprom_emulator_v7.ino.elf"
APP = B / "build-v7/eeprom_emulator_v7.ino.bin"

# Load only the pure parser/hash function definitions, not the V6 audit's
# top-level execution. Frozen V6 files/receipts must not be rewritten here.
template = ast.parse((B / "toolchain/audit_v6.py").read_text())
functions = ast.Module(body=[node for node in template.body
                            if isinstance(node, ast.FunctionDef) and node.name in {"digest", "function_body"}],
                       type_ignores=[])
namespace = {"hashlib": hashlib, "re": re}
exec(compile(functions, "v6-pure-audit-template", "exec"), namespace)
digest, function_body = namespace["digest"], namespace["function_body"]

old_main = (OLD / "eeprom_emulator_v6.ino").read_text()
new_main = (CURRENT / "eeprom_emulator_v7.ino").read_text()
names = ["crc32", "commandError", "appCapacity", "networkInfo", "pauseNetworking",
         "resumeNetworking", "serviceNetworking", "configureOTA", "loadNetwork", "provisionNetwork",
         "clearNetwork", "failUsbUpdate", "usbUpdate", "processCommand", "pollWindow"]
preserved = {}
for name in names:
    old, new = function_body(old_main, name), function_body(new_main, name)
    assert old == new, name + " changed"
    preserved[name] = digest(new)
old_hw, new_hw = (OLD / "emulator_hw.h").read_text(), (CURRENT / "emulator_hw.h").read_text()
for name in ["emuDetachFast", "emuFault", "emuFillTx", "emuPrimeTx", "emuFinishRead", "emuService", "emuIsr"]:
    old, new = function_body(old_hw, name), function_body(new_hw, name)
    assert old == new, "ISR changed: " + name
    preserved[name] = digest(new)
headers = ["control_rules.h", "poll_rules.h", "bench_rules.h", "bench_commands.h", "eeprom_model.h"]
for name in headers:
    assert (OLD / name).read_bytes() == (CURRENT / name).read_bytes(), name + " changed"
old_commands = (OLD / "emulator_commands.h").read_text()
new_commands = (CURRENT / "emulator_commands.h").read_text()
for name in ["emuStoredValid", "emuSave", "emuLoadStored", "emuLoadImage"]:
    assert function_body(old_commands, name) == function_body(new_commands, name), name + " changed"
assert re.search(r"struct EmuStored \{.*?\n\};", old_commands, re.S)[0] == re.search(r"struct EmuStored \{.*?\n\};", new_commands, re.S)[0]
assert not re.search(r"emuFaultCode\s*=\s*10\b", new_commands), "New V7 code still creates legacy fault10"

preservation = {"template_source": "Frozen independently reviewed V6 emulator",
                "preserved_function_body_sha256": preserved, "copied_headers_unchanged": headers,
                "nvs_layout_namespace_load_save_unchanged": True,
                "new_v7_faults_never_assign_legacy10": True, "hardware_actions": False,
                "intentional_changes": ["Distinct V7 revision", "Task-only attach diagnostics and pre-marker preflight",
                                        "New attach failure12/GPIO release failure13", "Explicit exact legacy10 acknowledgement only"]}
preservation_path = B / "toolchain/v7-source-preservation.json"
preservation_path.write_text(json.dumps(preservation, indent=2) + "\n")

nm = subprocess.check_output([str(TOOLS / "xtensa-esp32-elf-nm"), "-S", "-C", str(ELF)], text=True)
symbols = [line for line in nm.splitlines() if re.search(r"emu|eeprom_model|pollWindow|captureMux| edges$", line)]
iram_names = ["emuDetachFast()", "emuFault(unsigned int)", "emuFillTx()", "emuPrimeTx()", "emuFinishRead()",
              "emuService()", "emuIsr(void*)", "eeprom_model::Device::stop(bool)", "pollWindow(SamplingWindow&, unsigned int)"]
for name in iram_names:
    found = [line for line in symbols if line.endswith(" " + name)]
    assert len(found) == 1 and 0x40080000 <= int(found[0].split()[0], 16) < 0x400A0000, name
obj = subprocess.check_output([str(TOOLS / "xtensa-esp32-elf-objdump"), "-d", "-C", str(ELF)], text=True)
selected, keep = [], False
for line in obj.splitlines():
    match = re.match(r"^([0-9a-f]+) <(.+)>:$", line)
    if match:
        keep = match[2] in iram_names
    if keep:
        selected.append(line)
calls = [line for line in selected if re.search(r"\bcall", line)]
for line in calls:
    match = re.search(r"\bcall\w*\s+([0-9a-f]+)\s+<", line)
    assert match and 0x40080000 <= int(match[1], 16) < 0x400A0000, line
dis_path = B / "toolchain/v7-iram-disassembly.txt"
dis_path.write_text("\n".join(selected) + "\n")
elf_hash = digest(ELF.read_bytes())
assert APP.read_bytes()[176:208].hex() == elf_hash

paths = sorted(CURRENT.iterdir()) + sorted((B / "build-v7").glob("*.bin")) + [ELF, preservation_path, dis_path,
        B / "toolchain/test_v7_recovery.cpp", B / "research/v7-attach-diagnosis.json", Path(__file__).resolve()]
artifacts = {str(path.relative_to(B)): {"bytes": path.stat().st_size, "sha256": digest(path.read_bytes())}
             for path in paths if path.is_file()}
freeze = {"status": "AWAITING_INDEPENDENT_REVIEW_FOR_EXPLICIT_EXPERIMENTAL_HARDWARE_EVALUATION",
          "firmware": "i2c-eeprom-emulator-v7", "template_source": "Frozen reviewed V6 with exact actual failed-attach provenance",
          "fqbn": "esp32:esp32:esp32doit-devkit-v1", "core": "esp32:esp32@2.0.17",
          "app_storage_report_bytes": 795669, "static_ram_report_bytes": 58096, "ota_slot_bytes": 1310720,
          "hardware_actions": False, "transport_qualified": False, "artifacts": artifacts,
          "embedded_app_elf_sha256": elf_hash, "isr_and_poll_symbols": symbols, "isr_and_poll_calls": calls,
          "tests": ["25 exact legacy-attach recovery guard assertions, C++17 strict warnings ASan/UBSan PASS",
                    "V6 pure EEPROM model/ISR bodies unchanged; prior 6 groups/87 assertions remain applicable",
                    "14 management bodies/pollWindow/bench helpers/NVS load-save-layout preserved",
                    "All extracted ISR/poll calls IRAM; app embeds exact ELF SHA"],
          "unchanged_image": {"sha256": "d7c0db4edb0db564e8980ba26b8ebf6f95082685aa4e0b40c8c8630b4715e091",
                              "generation": 1, "remaining": 6256},
          "recovery_scope": "Explicit legacy V6 fault10 only, host-pinned original failed START+closed STOP provenance, exact clean durable seed. No data/generation reset, auto recovery, idle-guard bypass, bus clear or claim of durable rollback on checkpoint failure.",
          "hardware_qualification_pending": "Same V6 classic FIFO/byte IRQ/repeated-START/full-read/write timing gates; external SDA low still requires physical diagnosis."}
out = B / "toolchain/v7-build-freeze.json"
out.write_text(json.dumps(freeze, indent=2) + "\n")
print(json.dumps({"status": freeze["status"], "app": artifacts[str(APP.relative_to(B))],
                  "elf_sha256": elf_hash, "unchanged_functions": len(preserved), "iram_calls": len(calls)}, indent=2))
