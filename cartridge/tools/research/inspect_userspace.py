"""Static ARM ELF analysis only: target bytes are never executed."""
from pathlib import Path
import struct,json,argparse
from capstone import Cs,CS_ARCH_ARM,CS_MODE_ARM
from capstone.arm import ARM_OP_MEM,ARM_OP_IMM
from elftools.elf.elffile import ELFFile
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument("--elf",type=Path,required=True)
parser.add_argument("--output",type=Path,required=True)
args=parser.parse_args();r=args.output;r.mkdir(parents=True,exist_ok=False)
with args.elf.open('rb') as f:
 e=ELFFile(f);sections=[s for s in e.iter_sections() if s['sh_type']!='SHT_NOBITS' and s['sh_size'] and s['sh_flags']&2];syms=list(e.get_section_by_name('.symtab').iter_symbols());funcs=[s for s in syms if s['st_info']['type']=='STT_FUNC' and s['st_size']];addrs={s['st_value']:s.name for s in syms if s.name and not s.name.startswith('$') and s['st_value']};d=Cs(CS_ARCH_ARM,CS_MODE_ARM);d.detail=True
 def data(addr,n=200):
  for s in sections:
   start=s['sh_addr']
   if start<=addr<start+s['sh_size']:return s.data()[addr-start:addr-start+n]
  return b''
 def target(addr):
  if addr in addrs:return addrs[addr]
  b=data(addr).split(b'\0',1)[0]
  if b and all(x in b'\r\n\t' or 32<=x<127 for x in b):return repr(b.decode())
  return ''
 selected=[s for s in funcs if any(v in s.name for v in ('eeprom','cartridge_read','cartridge_get_driver','cartridge_write_contents','zprotocol','vhfs','xml','server','virtual_path','handle_command','handle_read_request','handle_write_request','handle_write_data_received','write_param_file','protected_file','protected_process'))]
 lines=[];calls=[]
 for s in sorted(funcs,key=lambda s:s['st_value']):
  a=s['st_value'];ins=list(d.disasm(data(a,s['st_size']),a))
  for i in ins:
   if i.mnemonic in ('bl','blx') and i.operands and i.operands[0].type==ARM_OP_IMM:
    to=i.operands[0].imm;calls.append({'from':s.name,'at':hex(i.address),'to':target(to),'to_addr':hex(to)})
  if s not in selected:continue
  lines.append(f'\nFUNCTION {s.name} 0x{a:x} size {s["st_size"]}')
  for i in ins:
   notes=[]
   if i.mnemonic in ('bl','blx','b') and i.operands and i.operands[0].type==ARM_OP_IMM:notes.append(target(i.operands[0].imm))
   for o in i.operands:
    if o.type==ARM_OP_MEM and i.reg_name(o.mem.base)=='pc':
     a2=i.address+8+o.mem.disp;b=data(a2,4)
     if len(b)==4:
      v=struct.unpack('<I',b)[0];notes.append(f'literal@{a2:x}={v:x} '+target(v))
   lines.append(f'{i.address:08x}: {i.mnemonic:8s} {i.op_str:32s} '+ '; '.join(notes))
 (r/'userspace-disassembly.txt').write_text('\n'.join(lines)+'\n');(r/'calls.json').write_text(json.dumps(calls,indent=2));print('selected functions',len(selected),'direct calls',len(calls));print('\n'.join(f'{s.name} 0x{s["st_value"]:x} {s["st_size"]}' for s in selected if 'eeprom' in s.name or 'cartridge_read' in s.name));print('callers');print(json.dumps([c for c in calls if 'eeprom' in c['to'] or 'cartridge_read' in c['to']],indent=2))
