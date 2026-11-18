"""Offline V8 diagnostic-only audit, using the frozen V6 pure-parser template."""
from pathlib import Path
import ast
import hashlib
import json
import re
import subprocess

B = Path(__file__).resolve().parent.parent
OLD = B / "firmware/eeprom_emulator_v7"
CURRENT = B / "firmware/eeprom_emulator_v8"
TOOLS = B / "toolchain/data/packages/esp32/tools/xtensa-esp32-elf-gcc/esp-2021r2-patch5-8.4.0/bin"
ELF = B / "build-v8/eeprom_emulator_v8.ino.elf"
APP = B / "build-v8/eeprom_emulator_v8.ino.bin"
tree = ast.parse((B / "toolchain/audit_v6.py").read_text())
pure = ast.Module(body=[node for node in tree.body if isinstance(node, ast.FunctionDef)
                       and node.name in {"digest", "function_body"}], type_ignores=[])
ns = {"hashlib": hashlib, "re": re}
exec(compile(pure, "pure-frozen-audit-template", "exec"), ns)
digest, body = ns["digest"], ns["function_body"]

old_main, new_main = (OLD / "eeprom_emulator_v7.ino").read_text(), (CURRENT / "eeprom_emulator_v8.ino").read_text()
preserved = {}
for name in ["crc32", "commandError", "appCapacity", "networkInfo", "pauseNetworking", "resumeNetworking",
             "serviceNetworking", "configureOTA", "loadNetwork", "provisionNetwork", "clearNetwork",
             "failUsbUpdate", "usbUpdate", "processCommand", "pollWindow"]:
    assert body(old_main, name) == body(new_main, name), name
    preserved[name] = digest(body(new_main, name))
old_hw, new_hw = (OLD / "emulator_hw.h").read_text(), (CURRENT / "emulator_hw.h").read_text()
for name in ["emuDetachFast", "emuFillTx", "emuPrimeTx", "emuFinishRead"]:
    assert body(old_hw, name) == body(new_hw, name), name
    preserved[name] = digest(body(new_hw, name))
old_commands, new_commands = (OLD / "emulator_commands.h").read_text(), (CURRENT / "emulator_commands.h").read_text()
for name in ["emuStoredValid", "emuSave", "emuLoadStored", "emuLoadImage", "emuRecover", "emuSnapshot", "emuLoadFailure"]:
    assert body(old_commands, name) == body(new_commands, name), name
    preserved[name] = digest(body(new_commands, name))
headers = ["control_rules.h", "poll_rules.h", "bench_rules.h", "bench_commands.h", "eeprom_model.h", "recovery_rules.h"]
for name in headers:
    assert (OLD / name).read_bytes() == (CURRENT / name).read_bytes(), name
assert re.search(r"struct EmuStored \{.*?\n\};", old_commands, re.S)[0] == re.search(r"struct EmuStored \{.*?\n\};", new_commands, re.S)[0]
old_config = old_hw[old_hw.index("  periph_module_enable("):old_hw.index("  emuDevice.start(); emuRxByteIrqs=0; emuPrimeTx();")].strip()
assert body(new_hw, "emuConfigureDetached").decode()[1:-1].strip() == old_config
diagnostic = (CURRENT / "detached_diag.h").read_text()
for forbidden in ["gpio_matrix_out(", "emuSave(", "emuDevice.load(", "emuDevice.receive(", "emuDevice.stop(", "Preferences"]:
    assert forbidden not in diagnostic, "Detached diagnostic contains " + forbidden
assert "ADOPT_V7_DURABLE" not in new_commands
assert "if (emulationActive && !emuReadFinished)" in new_hw
assert diagnostic.count("emuDiagOutputsDetached()") >= 4
preservation = {"template_source": "Frozen independently reviewed V7",
                "preserved_function_body_sha256": preserved, "headers_unchanged": headers,
                "peripheral_configuration_ahb_alias_ctr_threshold_timing_unchanged": True,
                "nvs_layout_namespace_load_save_unchanged": True, "legacy_recovery_unchanged_no_adoption": True,
                "detached_diagnostic_no_output_mapping_or_image_nvs_mutation": True,
                "intentional_delta": ["Distinct V8 revision", "Detached one-shot IRQ/FIFO/register diagnostics with combined matrix/OEN high-Z guards",
                                      "Reset reason and diagnostic RTC stage/inverse only", "Bounded IRQ entry/time and empty-RX guards",
                                      "TX watermark remains disabled after completed read until transaction reset"], "hardware_actions": False}
preservation_path = B / "toolchain/v8-source-preservation.json"
preservation_path.write_text(json.dumps(preservation, indent=2) + "\n")

nm = subprocess.check_output([str(TOOLS / "xtensa-esp32-elf-nm"), "-S", "-C", str(ELF)], text=True)
symbols = [line for line in nm.splitlines() if re.search(r"emu|eeprom_model|irq_rules|pollWindow|captureMux| edges$", line)]
iram_names = ["emuDetachFast()", "emuFault(unsigned int)", "emuFillTx()", "emuPrimeTx()", "emuFinishRead()",
              "emuService()", "emuIsr(void*)", "emuDiagIsr(void*)", "emuRecordStage(unsigned int)",
              "eeprom_model::Device::stop(bool)", "pollWindow(SamplingWindow&, unsigned int)"]
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
dis_path = B / "toolchain/v8-iram-disassembly.txt"
dis_path.write_text("\n".join(selected) + "\n")
elf_hash = digest(ELF.read_bytes())
assert APP.read_bytes()[176:208].hex() == elf_hash
paths = sorted(CURRENT.iterdir()) + sorted((B / "build-v8").glob("*.bin")) + [ELF, preservation_path, dis_path,
        B / "toolchain/test_v8_irq.cpp", Path(__file__).resolve()]
artifacts = {str(path.relative_to(B)): {"bytes": path.stat().st_size, "sha256": digest(path.read_bytes())}
             for path in paths if path.is_file()}
freeze = {"status": "AWAITING_INDEPENDENT_REVIEW_FOR_DETACHED_DIAGNOSTIC_EVALUATION",
          "firmware": "i2c-eeprom-emulator-v8", "template_source": "Frozen reviewed V7, actual reset-after-START marker evidence",
          "fqbn": "esp32:esp32:esp32doit-devkit-v1", "core": "esp32:esp32@2.0.17",
          "app_storage_report_bytes": 798677, "static_ram_report_bytes": 58160, "ota_slot_bytes": 1310720,
          "hardware_actions": False, "transport_qualified": False, "artifacts": artifacts,
          "embedded_app_elf_sha256": elf_hash, "isr_and_poll_symbols": symbols, "isr_and_poll_calls": calls,
          "tests": ["105 IRQ entry/time-boundary/CCOUNT-wrap assertions, C++17 strict warnings ASan/UBSan PASS",
                    "26 exact predecessor function bodies and6headers retained, including model and legacy recovery",
                    "Detached diagnostic has no peripheral output mapping or image/NVS writes; combined high-Z guard before setup and IRQ",
                    "All extracted ISR/poll calls IRAM; exact app embedded ELF SHA"],
          "preserved_durable_image": {"sha256": "d7c0db4edb0db564e8980ba26b8ebf6f95082685aa4e0b40c8c8630b4715e091",
                                      "generation": 1, "remaining": 6256, "unclean_active_marker_retained": True},
          "scope": "Passive upload then detached diagnostics only. No current unclean-marker recovery/adoption, no START retry authorized by this receipt.",
          "uncertainty": "V7 reset happened after active-marker write; cause/possible unseen volatile writes unknown. Empty RX threshold is not established cause; TRM documents RXcount>threshold. TX IRQ re-enable lifecycle bug is source-proven but not established reset cause."}
out = B / "toolchain/v8-build-freeze.json"
out.write_text(json.dumps(freeze, indent=2) + "\n")
print(json.dumps({"status": freeze["status"], "app": artifacts[str(APP.relative_to(B))],
                  "elf_sha256": elf_hash, "unchanged_functions": len(preserved), "iram_calls": len(calls)}, indent=2))
