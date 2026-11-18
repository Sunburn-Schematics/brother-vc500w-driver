"""Statically disassemble selected functions; never load or run the target ELF."""
from pathlib import Path
import struct
import argparse

from capstone import Cs, CS_ARCH_ARM, CS_MODE_ARM
from capstone.arm import ARM_OP_MEM
from elftools.elf.elffile import ELFFile

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument("--module",type=Path,required=True)
parser.add_argument("--output",type=Path,required=True)
args=parser.parse_args()
args.output.mkdir(parents=True,exist_ok=False)
base=args.output
with args.module.open("rb") as stream:
    elf = ELFFile(stream)
    symbols = elf.get_section_by_name(".symtab")
    text = elf.get_section_by_name(".text").data()
    relocations = {}
    for relocation in elf.get_section_by_name(".rel.text").iter_relocations():
        symbol = symbols.get_symbol(relocation["r_info_sym"])
        relocations[relocation["r_offset"]] = (symbol.name, symbol["st_value"], symbol["st_shndx"])
    decoder = Cs(CS_ARCH_ARM, CS_MODE_ARM)
    decoder.detail = True
    selected = [s for s in symbols.iter_symbols() if s["st_info"]["type"] == "STT_FUNC"
                and (s.name.startswith("cartEEPROM_") or s.name.startswith("cartridge_eeprom__")
                     or s.name.startswith("gcs_crc16") or s.name == "_crc16_reflected"
                     or s.name in {"zbe_print_done", "zbe_set_printer_status__consume_paper_by_step",
                                   "zbe_set_printer_status__consume_wristband",
                                   "zbe_set_printer_status__remain", "zbe_get_printer_status__remain",
                                   "zbe_debug_ioctl_cartridge_eeprom", "zbe_debug_cartridge_read_all", "zbe_ioctl", "zbe_debug_ioctl"})]
    lines = []
    for symbol in sorted(selected, key=lambda s: s["st_value"]):
        start, length = symbol["st_value"], symbol["st_size"]
        lines.append(f"\nFUNCTION {symbol.name} offset=0x{start:x} size={length}\n")
        for instruction in decoder.disasm(text[start:start + length], start):
            notes = []
            if instruction.address in relocations:
                notes.append("RELOC:" + str(relocations[instruction.address]))
            for operand in instruction.operands:
                if operand.type == ARM_OP_MEM and instruction.reg_name(operand.mem.base) == "pc":
                    literal = instruction.address + 8 + operand.mem.disp
                    if 0 <= literal < len(text) - 3:
                        value = struct.unpack_from("<I", text, literal)[0]
                        notes.append(f"literal@0x{literal:x}=0x{value:x}")
                        if literal in relocations:
                            name, address, section = relocations[literal]
                            notes.append(f"LITERAL_RELOC:{name}+0x{value:x} section={section}")
                            if isinstance(section, int):
                                target = elf.get_section(section).data()
                                offset = address + value
                                if 0 <= offset < len(target):
                                    chunk = target[offset:offset + 200].split(b"\0", 1)[0]
                                    if chunk and all(c in b"\r\n\t" or 32 <= c < 127 for c in chunk):
                                        notes.append("TEXT:" + repr(chunk.decode("ascii")))
            lines.append(f"{instruction.address:08x}: {instruction.mnemonic:8s} {instruction.op_str:30s} {'; '.join(notes)}")
    out = base / "eeprom-static-disassembly.txt"
    out.write_text("\n".join(lines) + "\n")
    print(out)
