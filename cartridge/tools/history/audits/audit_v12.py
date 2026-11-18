"""Offline V12 source/binary freeze; frozen V11 is the approved template."""
from pathlib import Path
import ast
import hashlib
import json
import re
import subprocess

B=Path(__file__).resolve().parent.parent
OLD=B/'firmware/eeprom_emulator_v11'
CURRENT=B/'firmware/eeprom_emulator_v12'
TOOLS=B/'toolchain/data/packages/esp32/tools/xtensa-esp32-elf-gcc/esp-2021r2-patch5-8.4.0/bin'
ELF=B/'build-v12/eeprom_emulator_v12.ino.elf'
APP=B/'build-v12/eeprom_emulator_v12.ino.bin'
tree=ast.parse((B/'toolchain/audit_v6.py').read_text())
pure=ast.Module(body=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in {'digest','function_body'}],type_ignores=[])
ns={'hashlib':hashlib,'re':re}
exec(compile(pure,'frozen-parser-template','exec'),ns)
digest,body=ns['digest'],ns['function_body']
preserved={}
def same(old,new,names):
    a,b=(OLD/old).read_text(),(CURRENT/new).read_text()
    for name in names:
        assert body(a,name)==body(b,name),name
        preserved[name]=digest(body(b,name))
    return a,b
same('eeprom_emulator_v11.ino','eeprom_emulator_v12.ino',[
    'crc32','commandError','appCapacity','networkInfo','pauseNetworking','resumeNetworking',
    'serviceNetworking','configureOTA','loadNetwork','provisionNetwork','clearNetwork',
    'failUsbUpdate','usbUpdate','processCommand','pollWindow'])
oldhw,newhw=same('emulator_hw.h','emulator_hw.h',[
    'emuDetachFast','emuFillTx','emuPrimeTx','emuFinishRead','emuIsr','emuRecordStage',
    'emuAttachPreflight','emuConfigureDetached','emuReleaseHardware'])
oldcmd,newcmd=same('emulator_commands.h','emulator_commands.h',[
    'emuStoredValid','emuSave','emuLoadStored','emuLoadImage','emuRecover','emuSnapshot',
    'emuLoadFailure','emuStart','emuStartPhase','emuAdoptDurable','emuStop'])
same('detached_diag.h','detached_diag.h',['emuDiagIsr'])
same('v10_commands.h','v10_commands.h',['emuV10State','emuCurrentImage','emuRecoverRx','emuPullup','emuStartPullup'])
same('v11_commands.h','v11_commands.h',['emuRecoverRead'])
same('pullup_control.h','pullup_control.h',['pullupPullsOff','pullupOffVerified','pullupOff','pullupOnVerified','pullupOn','pullupState'])
headers=['control_rules.h','poll_rules.h','bench_rules.h','bench_commands.h','eeprom_model.h',
         'recovery_rules.h','irq_rules.h','adoption_rules.h','detached_diag.h','pullup_control.h',
         'v10_rules.h','v10_commands.h','read_accounting.h','read_recovery_rules.h','v11_commands.h']
for name in headers:
    assert (OLD/name).read_bytes()==(CURRENT/name).read_bytes(),name
assert re.search(r'struct EmuStored \{.*?\n\};',oldcmd,re.S)[0]==re.search(r'struct EmuStored \{.*?\n\};',newcmd,re.S)[0]
service=body(newhw,'emuService').decode()
assert 'emuRxEvidence.observe(rxCount,reading,lastState,hwStatus)' in service
assert 'emuRxByteIrqs!=emuDevice.rxBytes' not in service
assert 'write_rules::eligible(' in service
assert service.index('emuCaptureWrite(record,0,flags,hwStatus)')<service.index('emuDevice.stop(true)')
assert 'record.generationAfter=emuDevice.generation; record.committed=1; record.valid=1' in service
assert 'int_clr.val=flags & ~I2C_RXFIFO_FULL_INT_ENA_M' in service
assert service.index('while (rxCount--)')<service.index('int_clr.val=I2C_RXFIFO_FULL_INT_ENA_M')
assert 'emuFault(15)' not in service and '++emuMetrics.emptyRxIrqs' in service
fault=body(newhw,'emuFault').decode()
assert fault.index('emuCaptureRejected(code)')<fault.index('emuDevice.start()')<fault.index('emuDetachFast()')
rules=(CURRENT/'write_rules.h').read_text()
assert 'lastState==5' in rules and 'stop && !busy && !reading && rxRemaining==0' in rules
assert 's.delivered==rxBytes && s.ackDelivered==rxBytes && s.badSamples==0' in rules
recover=body((CURRENT/'v12_commands.h').read_text(),'emuRecoverWrite').decode()
for forbidden in ['emuDevice.load(','emuDevice.receive(','emuDevice.stop(','completedRead(']:
    assert forbidden not in recover,forbidden
assert 'V11_WRITE_UNCHANGED' in recover and 'discarded_payload_unknown\\\":true' in recover
assert 'checkpoint_failed_outcome_uncertain' in recover
preservation={'template_source':'Frozen reviewed V11 plus actual9FIFO write/idleSTOP and complete fault7 STOP proof',
    'preserved_function_body_sha256':preserved,'headers_unchanged':headers,
    'nvs_schema_load_save_and_model_unchanged':True,'D19_updater_read_accounting_and_capture_unchanged':True,
    'rx_clear_after_drain_and_empty_stale_budget_retained':True,
    'FIFO_bounds_generation_page_semantics_and_real_STOP_preserved':True,
    'new_recovery_no_image_count_or_generation_mutation':True,'hardware_actions':False,
    'intentional_changes':['Observed RXFIFO SEND_ACK coverage plus separate idleSTOP replaces unreliable RXbit4/terminalstate5 equality',
        'Pre-discard rejected pagepayload evidence','Bounded4-record committed pagewrite evidence',
        'Exact V11 rejected-write fault7 metadata recovery retaining unknownpayload/physicaltape debt']}
preservation_path=B/'toolchain/v12-source-preservation.json'
preservation_path.write_text(json.dumps(preservation,indent=2)+'\n')
nm=subprocess.check_output([str(TOOLS/'xtensa-esp32-elf-nm'),'-S','-C',str(ELF)],text=True)
symbols=[line for line in nm.splitlines() if re.search(r'emu|eeprom_model|irq_rules|pollWindow|captureMux| edges$|pullup',line)]
iram_names=['emuDetachFast()','emuFault(unsigned int)','emuFillTx()','emuPrimeTx()','emuFinishRead(bool)',
    'emuCaptureWrite(EmuWriteRecord volatile&, unsigned int, unsigned int, unsigned int)',
    'emuService()','emuIsr(void*)','emuDiagIsr(void*)','emuRecordStage(unsigned int)',
    'eeprom_model::Device::stop(bool)','pollWindow(SamplingWindow&, unsigned int)']
for name in iram_names:
    found=[line for line in symbols if line.endswith(' '+name)]
    assert len(found)==1 and 0x40080000<=int(found[0].split()[0],16)<0x400a0000,name
obj=subprocess.check_output([str(TOOLS/'xtensa-esp32-elf-objdump'),'-d','-C',str(ELF)],text=True)
selected=[];keep=False
for line in obj.splitlines():
    match=re.match(r'^([0-9a-f]+) <(.+)>:$',line)
    if match: keep=match[2] in iram_names
    if keep: selected.append(line)
calls=[line for line in selected if re.search(r'\bcall',line)]
for line in calls:
    match=re.search(r'\bcall\w*\s+([0-9a-f]+)\s+<',line)
    assert match and 0x40080000<=int(match[1],16)<0x400a0000,line
dis=B/'toolchain/v12-iram-disassembly.txt';dis.write_text('\n'.join(selected)+'\n')
elf_hash=digest(ELF.read_bytes())
assert APP.read_bytes()[176:208].hex()==elf_hash
paths=sorted(CURRENT.iterdir())+sorted((B/'build-v12').glob('*.bin'))+[
    ELF,preservation_path,dis,B/'toolchain/test_v12_write.cpp',Path(__file__).resolve()]
artifacts={str(p.relative_to(B)):{'bytes':p.stat().st_size,'sha256':digest(p.read_bytes())} for p in paths if p.is_file()}
freeze={'status':'AWAITING_INDEPENDENT_REVIEW_FOR_TARGETED_V12_EXPERIMENTAL_EVALUATION',
    'firmware':'i2c-eeprom-emulator-v12','template_source':preservation['template_source'],
    'fqbn':'esp32:esp32:esp32doit-devkit-v1','core':'esp32:esp32@2.0.17',
    'app_storage_report_bytes':808965,'static_ram_report_bytes':59628,'ota_slot_bytes':1310720,
    'hardware_actions':False,'transport_qualified':False,'artifacts':artifacts,
    'embedded_app_elf_sha256':elf_hash,'isr_and_poll_symbols':symbols,'isr_and_poll_calls':calls,
    'tests':['565 V12 accounting/write qualification/evidence/recovery assertions including226 V11 cases C++17/O2 strict warnings ASan+UBSan PASS',
        '48 V10guards,105 prior IRQ-bound/time/wrap and87 EEPROM-model assertions apply to identical headers',
        f'{len(preserved)} predecessor bodies/{len(headers)} headers exact, model/NVS/14 management bodies preserved',
        '12 ISR/poll functions and all calls IRAM; app embedded ELF exact'],
    'physical_wiring_reported':'User confirmed4.7kGPIO19-to-GPIO21/SDA replacing priorBrotherVCCendpoint; printer remains powered; no supplyrailjoin',
    'power_limit':'USB-powered GPIO19 must be OFF before Brother poweroff; no automatic power sensing/backfeed protection',
    'recovery':{'policy':'V11_WRITE_UNCHANGED','sha256':'d7c0db4edb0db564e8980ba26b8ebf6f95082685aa4e0b40c8c8630b4715e091',
        'generation':1,'remaining':6256,'only_metadata':True,'discarded_payload_unknown':True,'physical_label_debt_mm':33.782,'host_actual_writefault7_and_full_STOP_provenance_required':True},
    'hardware_gates':['V11 startup256 and read16 succeeded; normal8+8write/readback qualification next',
        'Generic shortread/currentaddress/shifter corners remain unqualified', 'Generic malformed mid-byteSTOP remains unqualified; no bitphase proof',
        'No generic drop-in/full-cartridge-endurance qualification claimed']}
out=B/'toolchain/v12-build-freeze.json';out.write_text(json.dumps(freeze,indent=2)+'\n')
print(json.dumps({'status':freeze['status'],'app':artifacts[str(APP.relative_to(B))],
    'elf_sha256':elf_hash,'preserved_bodies':len(preserved),'iram_calls':len(calls)},indent=2))
