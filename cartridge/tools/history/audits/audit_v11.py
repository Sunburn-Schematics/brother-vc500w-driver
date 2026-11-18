"""Offline V11 source/binary freeze; frozen V10 is the approved template."""
from pathlib import Path
import ast
import hashlib
import json
import re
import subprocess

B=Path(__file__).resolve().parent.parent
OLD=B/'firmware/eeprom_emulator_v10'
CURRENT=B/'firmware/eeprom_emulator_v11'
TOOLS=B/'toolchain/data/packages/esp32/tools/xtensa-esp32-elf-gcc/esp-2021r2-patch5-8.4.0/bin'
ELF=B/'build-v11/eeprom_emulator_v11.ino.elf'
APP=B/'build-v11/eeprom_emulator_v11.ino.bin'
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
same('eeprom_emulator_v10.ino','eeprom_emulator_v11.ino',[
    'crc32','commandError','appCapacity','networkInfo','pauseNetworking','resumeNetworking',
    'serviceNetworking','configureOTA','loadNetwork','provisionNetwork','clearNetwork',
    'failUsbUpdate','usbUpdate','processCommand','pollWindow'])
oldhw,newhw=same('emulator_hw.h','emulator_hw.h',[
    'emuDetachFast','emuFillTx','emuPrimeTx','emuFault','emuIsr',
    'emuRecordStage','emuAttachPreflight','emuConfigureDetached','emuAttachHardware','emuReleaseHardware'])
oldcmd,newcmd=same('emulator_commands.h','emulator_commands.h',[
    'emuStoredValid','emuSave','emuLoadStored','emuLoadImage','emuRecover','emuSnapshot',
    'emuLoadFailure','emuStart','emuStartPhase','emuAdoptDurable','emuStop'])
same('detached_diag.h','detached_diag.h',['emuDiagIsr'])
same('v10_commands.h','v10_commands.h',['emuV10State','emuCurrentImage','emuRecoverRx','emuPullup','emuStartPullup'])
same('pullup_control.h','pullup_control.h',['pullupPullsOff','pullupOffVerified','pullupOff','pullupOnVerified','pullupOn','pullupState'])
headers=['control_rules.h','poll_rules.h','bench_rules.h','bench_commands.h','eeprom_model.h',
         'recovery_rules.h','irq_rules.h','adoption_rules.h','detached_diag.h',
         'pullup_control.h','v10_rules.h','v10_commands.h']
for name in headers:
    assert (OLD/name).read_bytes()==(CURRENT/name).read_bytes(),name
assert re.search(r'struct EmuStored \{.*?\n\};',oldcmd,re.S)[0]==re.search(r'struct EmuStored \{.*?\n\};',newcmd,re.S)[0]
service=body(newhw,'emuService').decode()
assert 'int_clr.val=flags & ~I2C_RXFIFO_FULL_INT_ENA_M' in service
assert service.index('while (rxCount--)')<service.index('int_clr.val=I2C_RXFIFO_FULL_INT_ENA_M')
assert 'emuFault(15)' not in service
assert '++emuMetrics.emptyRxIrqs' in service
pull=(CURRENT/'pullup_control.h').read_text()
assert 'W1TC_REG,PULLUP_MASK' not in pull.replace('GPIO_ENABLE_W1TC_REG,PULLUP_MASK','')
assert 'gpio_set_level' not in pull and 'digitalWrite' not in pull
assert pull.index('GPIO_OUT_W1TS_REG,PULLUP_MASK); // HIGH before output-enable.')<pull.index('pins.mode=GPIO_MODE_INPUT_OUTPUT;')
recover=body((CURRENT/'v11_commands.h').read_text(),'emuRecoverRead').decode()
for forbidden in ['emuDevice.load(','emuDevice.receive(','emuDevice.stop(','completedRead(']:
    assert forbidden not in recover,forbidden
assert 'V10_READ_UNCHANGED' in recover and 'checkpoint_failed_outcome_uncertain' in recover
assert 'd7c0db4edb0db564e8980ba26b8ebf6f95082685aa4e0b40c8c8630b4715e091' in recover
expected_service=body(oldhw,'emuService').decode().replace('emuHistoryNext=(history+1)&(EMU_HISTORY_CAPACITY-1);','emuHistoryNext=(history+1)&(EMU_HISTORY_CAPACITY-1);')
expected_service=expected_service.replace('emuHistoryNext=(history+1)&15;','emuHistoryNext=(history+1)&(EMU_HISTORY_CAPACITY-1);').replace('emuHistoryCount<16','emuHistoryCount<EMU_HISTORY_CAPACITY')
expected_service=expected_service.replace('!emuFinishRead())','!emuFinishRead(true))').replace('!emuReadFinished && !emuFinishRead(true)','!emuReadFinished && !emuFinishRead(false)')
assert service==expected_service
assert 'emuRxByteIrqs!=emuDevice.rxBytes' in service and 'lastState!=5' in service
assert 'eeprom_model::txConsumed(' not in newhw
preservation={'template_source':'Frozen reviewed V10 plus actual full startup read/NACK and complete STOP evidence',
    'preserved_function_body_sha256':preserved,'headers_unchanged':headers,
    'nvs_schema_load_save_and_model_unchanged':True,'legacy_recovery_and_adoption_unchanged':True,
    'rx_clear_after_drain_and_empty_stale_budget_retained':True,
    'gpio19_only_high_latch_writes':True,'new_recovery_no_image_count_or_generation_mutation':True,'model_page_STOP_and_FIFO_bounds_preserved':True,
    'hardware_actions':False,'intentional_changes':['TerminalNACK FIFO subtraction replaces unreliable bit4 TX equality',
        'STOP without terminalNACK fails closed','64entry internal DRAM IRQhistory',
        'Terminal read accounting diagnostics','Exact V10 readfault2 metadata recovery']}
preservation_path=B/'toolchain/v11-source-preservation.json'
preservation_path.write_text(json.dumps(preservation,indent=2)+'\n')
nm=subprocess.check_output([str(TOOLS/'xtensa-esp32-elf-nm'),'-S','-C',str(ELF)],text=True)
symbols=[line for line in nm.splitlines() if re.search(r'emu|eeprom_model|irq_rules|pollWindow|captureMux| edges$|pullup',line)]
iram_names=['emuDetachFast()','emuFault(unsigned int)','emuFillTx()','emuPrimeTx()','emuFinishRead(bool)',
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
dis=B/'toolchain/v11-iram-disassembly.txt';dis.write_text('\n'.join(selected)+'\n')
elf_hash=digest(ELF.read_bytes())
assert APP.read_bytes()[176:208].hex()==elf_hash
paths=sorted(CURRENT.iterdir())+sorted((B/'build-v11').glob('*.bin'))+[
    ELF,preservation_path,dis,B/'toolchain/test_v11_accounting.cpp',Path(__file__).resolve()]
artifacts={str(p.relative_to(B)):{'bytes':p.stat().st_size,'sha256':digest(p.read_bytes())} for p in paths if p.is_file()}
freeze={'status':'AWAITING_INDEPENDENT_REVIEW_FOR_TARGETED_V11_EXPERIMENTAL_EVALUATION',
    'firmware':'i2c-eeprom-emulator-v11','template_source':preservation['template_source'],
    'fqbn':'esp32:esp32:esp32doit-devkit-v1','core':'esp32:esp32@2.0.17',
    'app_storage_report_bytes':806469,'static_ram_report_bytes':59224,'ota_slot_bytes':1310720,
    'hardware_actions':False,'transport_qualified':False,'artifacts':artifacts,
    'embedded_app_elf_sha256':elf_hash,'isr_and_poll_symbols':symbols,'isr_and_poll_calls':calls,
    'tests':['226 V11 FIFO-accounting/recovery assertions C++17/O2 strict warnings ASan+UBSan PASS',
        '48 V10guards,105 prior IRQ-bound/time/wrap and87 EEPROM-model assertions apply to identical headers',
        f'{len(preserved)} predecessor bodies/{len(headers)} headers exact, model/NVS/14 management bodies preserved',
        '11 ISR/poll functions and all calls IRAM; app embedded ELF exact'],
    'physical_wiring_reported':'User confirmed4.7kGPIO19-to-GPIO21/SDA replacing priorBrotherVCCendpoint; printer remains powered; no supplyrailjoin',
    'power_limit':'USB-powered GPIO19 must be OFF before Brother poweroff; no automatic power sensing/backfeed protection',
    'recovery':{'policy':'V10_READ_UNCHANGED','sha256':'d7c0db4edb0db564e8980ba26b8ebf6f95082685aa4e0b40c8c8630b4715e091',
        'generation':1,'remaining':6256,'only_metadata':True,'host_actual_read_NACK_fault_and_full_STOP_provenance_required':True},
    'hardware_gates':['V10 D19OFF/ON/hot presence and READY observed; V11 next exact full256 terminal accounting',
        'Generic shortread/currentaddress/shifter corners remain unqualified', 'Actual normal8+8write/16readback',
        'No generic drop-in/full-cartridge-endurance qualification claimed']}
out=B/'toolchain/v11-build-freeze.json';out.write_text(json.dumps(freeze,indent=2)+'\n')
print(json.dumps({'status':freeze['status'],'app':artifacts[str(APP.relative_to(B))],
    'elf_sha256':elf_hash,'preserved_bodies':len(preserved),'iram_calls':len(calls)},indent=2))
