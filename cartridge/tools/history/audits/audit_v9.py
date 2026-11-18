"""Offline V9 delta/binary audit using the frozen V6 parser/V8 receipts."""
from pathlib import Path
import ast
import hashlib
import json
import re
import subprocess
B = Path(__file__).resolve().parent.parent
OLD, CURRENT = B / "firmware/eeprom_emulator_v8", B / "firmware/eeprom_emulator_v9"
TOOLS = B / "toolchain/data/packages/esp32/tools/xtensa-esp32-elf-gcc/esp-2021r2-patch5-8.4.0/bin"
ELF, APP = B / "build-v9/eeprom_emulator_v9.ino.elf", B / "build-v9/eeprom_emulator_v9.ino.bin"
tree = ast.parse((B / "toolchain/audit_v6.py").read_text())
pure = ast.Module(body=[n for n in tree.body if isinstance(n, ast.FunctionDef)
                       and n.name in {"digest", "function_body"}], type_ignores=[])
ns = {"hashlib": hashlib, "re": re}
exec(compile(pure, "pure-frozen-parser-template", "exec"), ns)
digest, body = ns["digest"], ns["function_body"]
preserved = {}

def same(file_old, file_new, names):
    a, b = (OLD / file_old).read_text(), (CURRENT / file_new).read_text()
    for name in names:
        assert body(a, name) == body(b, name), name
        preserved[name] = digest(body(b, name))
    return a, b

same("eeprom_emulator_v8.ino", "eeprom_emulator_v9.ino",
     ["crc32", "commandError", "appCapacity", "networkInfo", "pauseNetworking", "resumeNetworking",
      "serviceNetworking", "configureOTA", "loadNetwork", "provisionNetwork", "clearNetwork",
      "failUsbUpdate", "usbUpdate", "processCommand", "pollWindow"])
old_hw, new_hw = same("emulator_hw.h", "emulator_hw.h",
     ["emuDetachFast", "emuFillTx", "emuPrimeTx", "emuFinishRead", "emuFault", "emuIsr", "emuRecordStage"])
old_commands, new_commands = same("emulator_commands.h", "emulator_commands.h",
     ["emuStoredValid", "emuSave", "emuLoadStored", "emuLoadImage", "emuRecover", "emuSnapshot", "emuLoadFailure"])
same("detached_diag.h", "detached_diag.h", ["emuDiagIsr"])
headers = ["control_rules.h", "poll_rules.h", "bench_rules.h", "bench_commands.h", "eeprom_model.h", "recovery_rules.h", "irq_rules.h"]
for name in headers:
    assert (OLD / name).read_bytes() == (CURRENT / name).read_bytes(), name
assert re.search(r"struct EmuStored \{.*?\n\};", old_commands, re.S)[0] == re.search(r"struct EmuStored \{.*?\n\};", new_commands, re.S)[0]

def code(text):
    return re.sub(r"\s+", "", re.sub(r"/\*.*?\*/|//[^\n]*", "", text, flags=re.S))

assert code(body(new_hw, "emuConfigureDetached").decode()).replace("rx_fifo_full_thrhd=1", "rx_fifo_full_thrhd=0") == code(body(old_hw, "emuConfigureDetached").decode())
adopt = body(new_commands, "emuAdoptDurable").decode()
for forbidden in ["emuDevice.load(", "emuDevice.receive(", "emuDevice.stop(", "completedRead(", "generation="]:
    # The parser's local generation pointer is named in the argument declaration;
    # generation mutation is separately excluded by exact model/store preservation.
    if forbidden != "generation=":
        assert forbidden not in adopt, forbidden
assert "pre_reset_volatile_history_unknown\\\":true" in adopt
diag = (CURRENT / "detached_diag.h").read_text()
assert diag.index("// releaseInputs AND its destructor") > diag.index('} else message=networkSuspended?')
assert diag.index("finalRegisters=emuDiagRegisters();", diag.index("// releaseInputs AND")) > diag.index("// releaseInputs AND")
preservation = {"template_source": "Frozen independently reviewed V8 and actual detached data",
               "preserved_function_body_sha256": preserved, "headers_unchanged": headers,
               "peripheral_configuration_only_threshold0_to1": True,
               "nvs_schema_load_save_and_model_unchanged": True,
               "legacy_recovery_unchanged": True, "adoption_no_image_or_generation_write": True,
               "intentional_changes": ["Empirical RX threshold1 trial", "Post-destructor strict GPIO/OEN detach then final actual register capture",
                                       "Exact last-durable adoption with explicit unknown volatile history", "Two pre-output START phases",
                                       "16-entry DRAM IRQ observations exported only inactive"], "hardware_actions": False}
preservation_path = B / "toolchain/v9-source-preservation.json"
preservation_path.write_text(json.dumps(preservation, indent=2) + "\n")
nm = subprocess.check_output([str(TOOLS / "xtensa-esp32-elf-nm"), "-S", "-C", str(ELF)], text=True)
symbols = [line for line in nm.splitlines() if re.search(r"emu|eeprom_model|irq_rules|pollWindow|captureMux| edges$", line)]
iram_names = ["emuDetachFast()", "emuFault(unsigned int)", "emuFillTx()", "emuPrimeTx()", "emuFinishRead()", "emuService()",
              "emuIsr(void*)", "emuDiagIsr(void*)", "emuRecordStage(unsigned int)", "eeprom_model::Device::stop(bool)", "pollWindow(SamplingWindow&, unsigned int)"]
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
dis_path = B / "toolchain/v9-iram-disassembly.txt"
dis_path.write_text("\n".join(selected) + "\n")
elf_hash = digest(ELF.read_bytes())
assert APP.read_bytes()[176:208].hex() == elf_hash
paths = sorted(CURRENT.iterdir()) + sorted((B / "build-v9").glob("*.bin")) + [ELF, preservation_path, dis_path,
        B / "toolchain/test_v9_adoption.cpp", B / "emulator-v8-detached-diagnostic-01/independent-analysis.json", Path(__file__).resolve()]
artifacts = {str(p.relative_to(B)): {"bytes": p.stat().st_size, "sha256": digest(p.read_bytes())} for p in paths if p.is_file()}
freeze = {"status": "AWAITING_INDEPENDENT_REVIEW_FOR_TARGETED_EXPERIMENTAL_EVALUATION",
          "firmware": "i2c-eeprom-emulator-v9", "template_source": "Frozen reviewed V8; actual V8 detached register/IRQ evidence",
          "fqbn": "esp32:esp32:esp32doit-devkit-v1", "core": "esp32:esp32@2.0.17",
          "app_storage_report_bytes": 801033, "static_ram_report_bytes": 58424, "ota_slot_bytes": 1310720,
          "hardware_actions": False, "transport_qualified": False, "artifacts": artifacts,
          "embedded_app_elf_sha256": elf_hash, "isr_and_poll_symbols": symbols, "isr_and_poll_calls": calls,
          "tests": ["28 last-durable adoption guard assertions C++17/O2 strict warnings ASan+UBSan PASS",
                    "105 prior IRQ-bound/time/wrap tests and6group87assertion EEPROM model still apply to identical headers",
                    "30 predecessor function bodies/7headers exact, store/layout/model unchanged",
                    "All11 ISR/poll functions and17 calls IRAM; history256bytes internal DRAM; app embedded ELF exact"],
          "adoption": {"policy": "V7_START_RESET ACK_VOLATILE_UNKNOWN", "last_durable_sha256": "d7c0db4edb0db564e8980ba26b8ebf6f95082685aa4e0b40c8c8630b4715e091",
                       "generation": 1, "remaining": 6256, "pre_reset_volatile_history_unknown": True,
                       "no_image_count_or_generation_rewrite": True, "host_must_pin_actual_timeout_stop_and_current_diagnostic_evidence": True},
          "hardware_gates": ["Threshold1 detached comparison", "First-pointer FIFO service still empirical/unqualified",
                             "Actual full256 startup and normal8+8write/16readback", "No full-cartridge endurance result claimed"]}
out = B / "toolchain/v9-build-freeze.json"
out.write_text(json.dumps(freeze, indent=2) + "\n")
print(json.dumps({"status": freeze["status"], "app": artifacts[str(APP.relative_to(B))], "elf_sha256": elf_hash,
                  "unchanged_functions": len(preserved), "iram_calls": len(calls)}, indent=2))
