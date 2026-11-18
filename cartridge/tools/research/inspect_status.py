"""Static manufacturer ELF/module inspection; never execute vendor bytes."""
from pathlib import Path
import json,struct,argparse
from capstone import Cs,CS_ARCH_ARM,CS_MODE_ARM
from capstone.arm import ARM_OP_MEM,ARM_OP_IMM
from elftools.elf.elffile import ELFFile
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument("--elf",type=Path,required=True)
parser.add_argument("--module",type=Path,required=True)
parser.add_argument("--output",type=Path,required=True)
args=parser.parse_args();r=args.output;r.mkdir(parents=True,exist_ok=False)
wanted={'update_online_status','update_status_online','update_status_xml','check_status','z9100_socket_loop','gusb_connect','zprotocol_reset','handle_read_request','virtual_path_fix','update_eeprom_readable_text','cartridge_write_contents_file'}
with args.elf.open('rb') as f:
 e=ELFFile(f); sections=[s for s in e.iter_sections() if s['sh_type']!='SHT_NOBITS' and s['sh_size'] and s['sh_flags']&2]; syms=list(e.get_section_by_name('.symtab').iter_symbols());addrs={s['st_value']:s.name for s in syms if s.name and not s.name.startswith('$') and s['st_value']}
 d=Cs(CS_ARCH_ARM,CS_MODE_ARM);d.detail=True
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
 for s in syms:
  if s.name not in wanted:continue
  lines=[f'FUNCTION {s.name} 0x{s["st_value"]:x} size {s["st_size"]}']
  for i in d.disasm(data(s['st_value'],s['st_size']),s['st_value']):
   notes=[]
   if i.mnemonic in ('bl','blx','b') and i.operands and i.operands[0].type==ARM_OP_IMM:notes.append(target(i.operands[0].imm))
   for o in i.operands:
    if o.type==ARM_OP_MEM and i.reg_name(o.mem.base)=='pc':
     a=i.address+8+o.mem.disp;b=data(a,4)
     if len(b)==4:
      v=struct.unpack('<I',b)[0];notes.append(f'literal@{a:x}={v:x} '+target(v))
   lines.append(f'{i.address:08x}: {i.mnemonic:8s} {i.op_str:32s} '+ '; '.join(notes))
  (r/(s.name+'.txt')).write_text('\n'.join(lines)+'\n')
with args.module.open('rb') as f:
 e=ELFFile(f);st=e.get_section_by_name('.symtab');funcs=[s for s in st.iter_symbols() if s['st_info']['type']=='STT_FUNC' and s['st_shndx']==1 and s['st_size']];rel={}
 for x in e.get_section_by_name('.rel.text').iter_relocations():
  sym=st.get_symbol(x['r_info_sym']);rel[x['r_offset']]=(sym.name,sym['st_value'],sym['st_shndx'])
 calls=[]
 for s in funcs:
  for a,to in rel.items():
   if s['st_value']<=a<s['st_value']+s['st_size']:
    calls.append({'from':s.name,'at':hex(a),'to':to[0],'value':hex(to[1]),'section':to[2]})
 (r/'module-relocation-callers.json').write_text(json.dumps(calls,indent=2)+'\n')
 for x in calls:
  if x['to'] in {'cartridge_eeprom__read_all','cartEEPROM_read_all','cartEEPROM_read_data','zbe_update_printer_status','zbe_get_printer_status','zbe_get_status'}:print(json.dumps(x))
 # Kernel read/status/cassette paths, preserving relocations and literal targets.
 text=e.get_section_by_name('.text').data()
 wanted_module={'zbe_read','zbe_get_printer_status','zbe_update_printer_status','zbe_open','zbe_release','zbe_ioctl','zbe_debug_ioctl','apdl_initialize_done','system_monitor__cartridge_door_update_status','T.366'}
 for s in funcs:
  if s.name not in wanted_module:continue
  lines=[f'FUNCTION {s.name} offset0x{s["st_value"]:x} size{s["st_size"]}']
  for i in d.disasm(text[s['st_value']:s['st_value']+s['st_size']],s['st_value']):
   notes=[]
   if i.address in rel:notes.append('RELOC:'+str(rel[i.address]))
   for o in i.operands:
    if o.type==ARM_OP_MEM and i.reg_name(o.mem.base)=='pc':
     a=i.address+8+o.mem.disp
     if 0<=a<len(text)-3:
      v=struct.unpack_from('<I',text,a)[0];notes.append(f'literal@{a:x}={v:x}')
      if a in rel:
       n,off,section=rel[a];notes.append(f'LITERAL_RELOC:{n}+{v:x}section{section}')
       if isinstance(section,int):
        target_bytes=e.get_section(section).data();offset=off+v
        chunk=target_bytes[offset:offset+200].split(b'\0',1)[0] if 0<=offset<len(target_bytes) else b''
        if chunk and all(c in b'\r\n\t' or 32<=c<127 for c in chunk):notes.append(repr(chunk.decode()))
   lines.append(f'{i.address:08x}: {i.mnemonic:8s} {i.op_str:32s} '+ '; '.join(notes))
  (r/('kernel-'+s.name+'.txt')).write_text('\n'.join(lines)+'\n')
print('Static status evidence saved',r)
