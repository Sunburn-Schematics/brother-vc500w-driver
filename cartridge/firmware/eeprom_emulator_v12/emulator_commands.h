#pragma once
#include "emulator_hw.h"
#include "recovery_rules.h"
#include "adoption_rules.h"

struct EmuStored {
  uint32_t magic, version, sequence, generation, activeSession, fault;
  uint8_t image[256];
  char seedHash[65];
  uint32_t crc;
};
static bool emuStoredValid(const EmuStored &value) {
  return value.magic==0x53424536 && value.version==1 && value.activeSession<=1 &&
    value.seedHash[64]==0 &&
    control_rules::validHex(value.seedHash,64) &&
    value.crc==crc32(reinterpret_cast<const uint8_t *>(&value),offsetof(EmuStored,crc));
}
static bool emuSave(bool activeSession) {
  if (emulationActive || !benchPinsInputOnly() || !benchPinsPullsOff() || emuSnapshotSequence==UINT32_MAX) return false;
  EmuStored value={}; uint32_t gen=0;
  if (!emuDevice.snapshot(value.image,gen)) return false;
  value.magic=0x53424536; value.version=1; value.sequence=emuSnapshotSequence+1;
  value.generation=gen; value.activeSession=activeSession; value.fault=emuFaultCode;
  memcpy(value.seedHash,emuSeedHash,65);
  value.crc=crc32(reinterpret_cast<const uint8_t *>(&value),offsetof(EmuStored,crc));
  Preferences store;
  if (!store.begin("sb-eeemu-v6",false)) return false;
  const char *key=(value.sequence&1)?"a":"b";
  const size_t written=store.putBytes(key,&value,sizeof(value));
  EmuStored verified={}; const size_t received=store.getBytes(key,&verified,sizeof(verified));
  store.end();
  if (written!=sizeof(value) || received!=sizeof(value) || !emuStoredValid(verified) ||
      memcmp(&value,&verified,sizeof(value))!=0) return false;
  emuSnapshotSequence=value.sequence; emuDurableGeneration=gen; emuSessionMarker=activeSession;
  return benchHash(value.image,emuIdleHash);
}
static void emuLoadStored() {
  Preferences store;
  if (!store.begin("sb-eeemu-v6",true)) return;
  EmuStored a={},b={};
  const bool validA=store.getBytesLength("a")==sizeof(a) && store.getBytes("a",&a,sizeof(a))==sizeof(a) && emuStoredValid(a);
  const bool validB=store.getBytesLength("b")==sizeof(b) && store.getBytes("b",&b,sizeof(b))==sizeof(b) && emuStoredValid(b);
  store.end();
  if (!validA && !validB) return;
  const EmuStored &value=validB && (!validA || b.sequence>a.sequence)?b:a;
  emuDevice.load(value.image,value.generation); emuLoaded=true;
  emuSnapshotSequence=value.sequence; emuDurableGeneration=value.generation;
  emuSessionMarker=value.activeSession; emuFaultCode=value.fault;
  emuUncleanBoot=value.activeSession || value.fault;
  memcpy(emuSeedHash,value.seedHash,65); benchHash(value.image,emuIdleHash);
  // Never attach the peripheral or acknowledge an address while booting.
}
static const char *emuFaultName() {
  switch (emuFaultCode) {
    case 0:return "none"; case 1:return "fifo_bounds"; case 2:return "tx_byte_count_unqualified";
    case 3:return "rx_overflow"; case 4:return "ambiguous_start_stop"; case 5:return "nack_state_unqualified";
    case 6:return "rx_direction_ambiguous"; case 7:return "write_stop_unqualified";
    case 8:return "tx_underflow"; case 9:return "stop_timeout"; case 10:return "gpio_release_failed";
    case 11:return "checkpoint_failed"; case 12:return "hardware_attach_failed";
    case 13:return "gpio_release_failed";
    case 14:return "irq_service_budget_exceeded"; case 15:return "empty_rx_watermark";
    default:return "unknown_fault";
  }
}
#include "write_evidence_output.h"
static void emuState(uint32_t live) {
  const bool sclInput=(REG_READ(GPIO_ENABLE_REG)&(1U<<22))==0;
  const bool sdaOd=(REG_READ(GPIO_PIN21_REG)&GPIO_PIN21_PAD_DRIVER)!=0;
  Serial.printf("\"firmware\":\"%s\",\"capability\":\"eeprom_256_experimental_v1\","
    "\"emulation_active\":%s,\"session_open\":%s,\"loaded\":%s,\"address\":80,"
    "\"image_sha256\":\"%s\",\"seed_sha256\":\"%s\",\"live_generation\":%lu,"
    "\"durable_generation\":%lu,\"dirty\":%s,\"unclean_boot\":%s,\"session_active_marker\":%s,"
    "\"pins_input_only\":%s,\"scl_input_only\":%s,\"sda_open_drain\":%s,\"internal_pulls\":%s,"
    "\"last_fault\":\"%s\",\"transport_qualified\":false,\"metrics\":{"
    "\"rx_bytes\":%lu,\"tx_completed_bytes\":%lu,\"tx_prefetched_bytes\":%lu,\"byte_irqs\":%lu,"
    "\"starts\":%lu,\"stops\":%lu,\"nacks\":%lu,\"rx_overflows\":%lu,\"tx_underflows\":%lu,"
    "\"fifo_accounting_errors\":%lu,\"max_isr_cycles\":%lu,\"last_hw_status\":%lu,\"invalid_stops\":%lu}",
    REVISION,emulationActive?"true":"false",emulationSessionOpen?"true":"false",emuLoaded?"true":"false",
    (!emulationActive && !emulationSessionOpen && live==emuDurableGeneration)?emuIdleHash:"",
    emuSeedHash,(unsigned long)live,(unsigned long)emuDurableGeneration,
    live!=emuDurableGeneration?"true":"false",emuUncleanBoot?"true":"false",emuSessionMarker?"true":"false",
    benchPinsInputOnly()?"true":"false",sclInput?"true":"false",sdaOd?"true":"false",
    benchPinsPullsOff()?"false":"true",emuFaultName(),(unsigned long)emuMetrics.rxBytes,
    (unsigned long)emuMetrics.txCompleted,(unsigned long)emuMetrics.txPrefetched,(unsigned long)emuMetrics.byteIrqs,
    (unsigned long)emuMetrics.starts,(unsigned long)emuMetrics.stops,(unsigned long)emuMetrics.nacks,
    (unsigned long)emuMetrics.rxOverflows,(unsigned long)emuMetrics.txUnderflows,(unsigned long)emuMetrics.fifoErrors,
    (unsigned long)emuMetrics.maxIsrCycles,(unsigned long)emuMetrics.lastHwStatus,(unsigned long)emuMetrics.invalidStops);
  Serial.printf(",\"last_fault_code\":%lu,\"attach_diagnostics\":{\"stage\":\"%s\","
    "\"core\":%lu,\"levels\":%lu,\"output_enable\":%lu,\"sda_mux\":%lu,\"scl_mux\":%lu,"
    "\"pins_input_only\":%s,\"internal_pulls\":%s,"
    "\"irq_error\":%ld,\"gpio_error\":%ld}",(unsigned long)emuFaultCode,
    emuAttachDiagnostics.stage,(unsigned long)emuAttachDiagnostics.core,(unsigned long)emuAttachDiagnostics.levels,
    (unsigned long)emuAttachDiagnostics.outputEnable,(unsigned long)emuAttachDiagnostics.sdaMux,(unsigned long)emuAttachDiagnostics.sclMux,
    emuAttachDiagnostics.inputOnly?"true":"false",emuAttachDiagnostics.pullsOff?"false":"true",
    (long)emuAttachDiagnostics.irqError,(long)emuAttachDiagnostics.gpioError);
  Serial.printf(",\"reset_reason\":%lu,\"boot_breadcrumb\":{\"valid\":%s,\"stage\":%lu,\"stage_inverse\":%lu},"
    "\"isr_diagnostics\":{\"isr_calls\":%lu,\"last_irq_flags\":%lu,\"last_rx_count\":%lu,"
    "\"last_tx_count\":%lu,\"irq_storms\":%lu,\"empty_rx_irqs\":%lu}",
    (unsigned long)emuResetReason,emuBootBreadcrumbValid?"true":"false",(unsigned long)emuBootStage,
    (unsigned long)emuBootInverse,(unsigned long)emuMetrics.isrCalls,(unsigned long)emuMetrics.lastIrqFlags,
    (unsigned long)emuMetrics.lastRxCount,(unsigned long)emuMetrics.lastTxCount,
    (unsigned long)emuMetrics.irqStorms,(unsigned long)emuMetrics.emptyRxIrqs);
  const bool historyAvailable=!emulationActive;
  Serial.printf(",\"irq_history_available\":%s,\"irq_history\":[",historyAvailable?"true":"false");
  if (historyAvailable) {
    const uint32_t count=emuHistoryCount, first=(emuHistoryNext+EMU_HISTORY_CAPACITY-count)&(EMU_HISTORY_CAPACITY-1);
    for (uint32_t i=0;i<count;++i) {
      const EmuIrqSample &sample=emuIrqHistory[(first+i)&(EMU_HISTORY_CAPACITY-1)];
      Serial.printf("%s{\"flags\":%lu,\"hw_status\":%lu,\"rx_fifo\":%lu,\"tx_fifo\":%lu}",i?",":"",
        (unsigned long)sample.flags,(unsigned long)sample.status,(unsigned long)sample.rx,(unsigned long)sample.tx);
    }
  }
  Serial.print("]");
  pullupState();
  Serial.printf(",\"read_accounting\":{\"method\":\"fifo_dequeued_at_terminal_nack\",\"loaded\":%lu,\"fifo_remaining\":%lu,\"consumed\":%lu,"
    "\"terminal_nack\":%s,\"rx_irq_diagnostic\":%lu,\"tx_irq_diagnostic\":%lu}",
    (unsigned long)emuMetrics.readLoaded,(unsigned long)emuMetrics.readRemaining,(unsigned long)emuMetrics.readConsumed,
    emuMetrics.readTerminalNack?"true":"false",(unsigned long)emuMetrics.readRxIrqs,(unsigned long)emuMetrics.readTxIrqs);
  emuWriteEvidenceState();
}
static void emuResult(const char *op,bool ok,const char *message) {
  Serial.printf("{\"type\":\"emu_result\",\"op\":\"%s\",\"ok\":%s,\"message\":\"%s\",",op,ok?"true":"false",message);
  emuState(emuDevice.generation); Serial.println("}");
}
static void emuStatus() {
  Serial.print("{\"type\":\"emu_status\",\"ok\":true,"); emuState(emuDevice.generation); Serial.println("}");
}
static void emuSnapshot() {
  uint8_t copy[256]={}; uint32_t gen=0; char hash[65]={};
  if (!emuLoaded || !emuDevice.snapshot(copy,gen) || !benchHash(copy,hash)) {
    Serial.print("{\"type\":\"emu_snapshot\",\"ok\":false,\"message\":\"snapshot_busy_or_unloaded\",");
    emuState(emuDevice.generation); Serial.println("}"); return;
  }
  const bench_rules::Crcs crc=bench_rules::check(copy);
  Serial.print("{\"type\":\"emu_snapshot\",\"ok\":true,\"bytes\":256,\"hex\":\"");
  for (unsigned i=0;i<256;++i) Serial.printf("%02x",copy[i]);
  const uint32_t live=emuDevice.generation;
  Serial.printf("\",\"sha256\":\"%s\",\"crc_r0\":%s,\"crc_rw1\":%s,\"crc_rw2\":%s,"
    "\"image_generation\":%lu,\"snapshot_is_current\":%s,",hash,crc.r0?"true":"false",
    crc.rw1?"true":"false",crc.rw2?"true":"false",(unsigned long)gen,gen==live?"true":"false");
  emuState(live); Serial.println("}");
}
#include "detached_diag.h"

static void emuStartPhase(const char *phase) {
  Serial.printf("{\"type\":\"emu_start_phase\",\"op\":\"start\",\"phase\":\"%s\",",phase);
  if (strcmp(phase,"fifo_loaded")==0) { emuPrintDiagRegisters(emuDiagRegisters()); Serial.print(","); }
  emuState(emuDevice.generation); Serial.println("}"); Serial.flush();
}

static void emuLoadFailure(const char *message) {
  // Any partial binary transfer locks mutations until the existing nonce reset.
  snprintf(recoveryNonce,sizeof(recoveryNonce),"%08lx%08lx",(unsigned long)esp_random(),(unsigned long)esp_random());
  recoveryRequired=true;
  Serial.printf("{\"type\":\"emu_result\",\"op\":\"load\",\"ok\":false,\"message\":\"%s\","
    "\"recovery_nonce\":\"%s\",",message,recoveryNonce); emuState(emuDevice.generation); Serial.println("}");
}
static void emuLoadImage(char *arguments) {
  char *saved=nullptr,*size=strtok_r(arguments," ",&saved),*hash=strtok_r(nullptr," ",&saved);
  if (!size || strcmp(size,"256")!=0 || !hash || !control_rules::validHex(hash,64) || strtok_r(nullptr," ",&saved)) {
    emuResult("load",false,"requires_256_and_sha256"); return;
  }
  if (emulationSessionOpen || emulationActive || emuUncleanBoot || emuSessionMarker ||
      emuDevice.generation!=emuDurableGeneration || armed || updateBusy || recoveryRequired ||
      !benchPinsInputOnly() || !benchPinsPullsOff()) {
    emuResult("load",false,"requires_passive_idle"); return;
  }
  if (emuDevice.generation==UINT32_MAX || !pauseNetworking()) { emuResult("load",false,"generation_or_network_failed"); return; }
  control_rules::lowerHex(hash);
  Serial.printf("{\"type\":\"emu_load_ready\",\"bytes\":256,\"sha256\":\"%s\",\"timeout_ms\":5000}\n",hash);
  uint8_t image[256]={}; size_t received=0; const uint32_t begin=millis();
  while (received<256 && uint32_t(millis()-begin)<5000) {
    if (Serial.available()) image[received++]=uint8_t(Serial.read()); else delay(1);
  }
  char actual[65]={};
  if (received!=256 || !benchHash(image,actual) || strcmp(actual,hash)!=0 || !bench_rules::check(image).valid()) {
    emuLoadFailure("image_length_hash_or_crc_failed"); resumeNetworking(); return;
  }
  const uint32_t nextGen=emuDevice.generation+1;
  emuDevice.load(image,nextGen); memcpy(emuSeedHash,actual,65); emuLoaded=true;
  emuFaultCode=0; emuUncleanBoot=false;
  if (!emuSave(false)) { emuFaultCode=11; emuResult("load",false,"checkpoint_failed"); }
  else emuResult("load",true,"loaded_and_durable");
  resumeNetworking();
}
static void emuStart(char *arguments) {
  char *saved=nullptr,*ownership=strtok_r(arguments," ",&saved),*hash=strtok_r(nullptr," ",&saved),
    *generation=strtok_r(nullptr," ",&saved); uint32_t gen=0;
  if (!ownership || strcmp(ownership,"SOLE_0X50")!=0 || !hash || !control_rules::validHex(hash,64) ||
      !generation || !control_rules::decimalBounded(generation,0,UINT32_MAX-1,gen) || strtok_r(nullptr," ",&saved)) {
    emuResult("start",false,"requires_ownership_sha256_generation"); return;
  }
  control_rules::lowerHex(hash);
  uint8_t image[256]={}; uint32_t live=0;
  if (emulationSessionOpen || emulationActive || !emuLoaded || emuUncleanBoot || emuFaultCode ||
      armed || updateBusy || recoveryRequired || gen!=emuDurableGeneration || gen!=emuDevice.generation ||
      strcmp(hash,emuIdleHash)!=0 || !emuDevice.snapshot(image,live) || !bench_rules::check(image).valid()) {
    emuResult("start",false,"state_or_preimage_guard_failed"); return;
  }
  // An ordinary non-idle bus is not an active session. Preserve the guard and
  // return precise diagnostics before writing an active marker or enabling SDA.
  emuRecordStage(16);
  if (!emuAttachPreflight()) { emuResult("start",false,"attach_preflight_failed"); return; }
  REG_WRITE(GPIO_FUNC21_OUT_SEL_CFG_REG,SIG_GPIO_OUT_IDX|GPIO_FUNC21_OEN_SEL);
  REG_WRITE(GPIO_FUNC22_OUT_SEL_CFG_REG,SIG_GPIO_OUT_IDX|GPIO_FUNC22_OEN_SEL);
  REG_WRITE(GPIO_ENABLE_W1TC_REG,BUS_MASK);
  REG_WRITE(GPIO_OUT_W1TS_REG,BUS_MASK);
  if (!pauseNetworking() || !emuSave(true)) { emuFaultCode=11; emuResult("start",false,"marker_or_network_failed"); return; }
  emuRecordStage(17);
  memset((void *)&emuMetrics,0,sizeof(emuMetrics));
  emuHistoryNext=emuHistoryCount=0;
  emulationSessionOpen=true;
  emuStartPhase("marker_saved");
  if (!emuAttachHardware()) {
    emuFaultCode=12; emuReleaseHardware();
    if (!benchPinsInputOnly() || !benchPinsPullsOff()) emuFaultCode=13;
    emuResult("start",false,"hardware_attach_failed"); return;
  }
  emuResult("start",true,"experimental_responder_enabled");
}
static void emuStop() {
  if (!emulationSessionOpen) {
    const bool released=pullupOff();
    if (!released) updateBusy=true;
    emuResult("stop",released,released?"already_passive_marker_preserved":"gpio19_release_failed");
    if (released) resumeNetworking(); return;
  }
  const uint32_t begin=millis();
  while (emulationActive && emuHw->status_reg.bus_busy && uint32_t(millis()-begin)<200) delay(1);
  if (emulationActive && emuHw->status_reg.bus_busy) emuFault(9);
  // Same-core pending IRQs normally run before this task. Service a pending
  // boundary once while masked, then detach. No wait/flash/serial in the mask.
  portENTER_CRITICAL(&captureMux);
  if (emulationActive && emuHw->int_status.val) emuIsr(nullptr);
  emuDetachFast();
  portEXIT_CRITICAL(&captureMux);
  emuReleaseHardware();
  emuRecordStage(32);
  if (!pullupOff() || !benchPinsInputOnly() || !benchPinsPullsOff()) {
    emuFaultCode=13; updateBusy=true; emuResult("stop",false,"gpio_release_failed"); return;
  }
  if (!emuSave(false)) { emuFaultCode=11; emuResult("stop",false,"checkpoint_failed"); return; }
  emuRecordStage(33);
  emulationSessionOpen=false;
  emuResult("stop",emuFaultCode==0,emuFaultCode?"detached_saved_with_fault":"detached_and_durable");
  resumeNetworking();
}
static void emuRecover(char *arguments) {
  char *saved=nullptr,*policy=strtok_r(arguments," ",&saved),*hash=strtok_r(nullptr," ",&saved),
    *generation=strtok_r(nullptr," ",&saved); uint32_t pinned=0;
  if (!policy || strcmp(policy,"ATTACH_V6_UNCHANGED")!=0 || !hash || !control_rules::validHex(hash,64) ||
      !generation || !control_rules::decimalBounded(generation,0,UINT32_MAX,pinned) || strtok_r(nullptr," ",&saved)) {
    emuResult("recover",false,"requires_v6_attach_sha256_generation"); return;
  }
  control_rules::lowerHex(hash);
  uint8_t image[256]={}; uint32_t gen=0; char actual[65]={};
  const bool exact=emuLoaded && emuDevice.snapshot(image,gen) && benchHash(image,actual) &&
    bench_rules::check(image).valid() && gen==pinned && strcmp(actual,hash)==0 &&
    strcmp(actual,emuSeedHash)==0 && strcmp(actual,emuIdleHash)==0;
  const emu_recovery::State state={emuFaultCode,emuDevice.generation,emuDurableGeneration,
    emulationActive,emulationSessionOpen,emuSessionMarker,emuLoaded,
    emuDevice.generation!=emuDurableGeneration,armed,updateBusy,recoveryRequired,
    benchPinsInputOnly(),benchPinsPullsOff(),exact};
  if (!emu_recovery::eligible(state,pinned)) { emuResult("recover",false,"recovery_preimage_or_state_guard_failed"); return; }
  if (!pauseNetworking()) { emuResult("recover",false,"network_suspend_failed"); return; }
  const uint32_t previousFault=emuFaultCode; const bool previousUnclean=emuUncleanBoot;
  emuFaultCode=0; emuUncleanBoot=false;
  if (!emuSave(false)) {
    // A failed readback is an uncertain persistence outcome, not a rollback.
    // Preserve prior RAM evidence and require the host to inspect, never retry.
    emuFaultCode=previousFault; emuUncleanBoot=previousUnclean;
    emuResult("recover",false,"checkpoint_failed_outcome_uncertain");
  } else emuResult("recover",true,"unchanged_seed_fault_metadata_cleared");
  resumeNetworking();
}
static void emuAdoptDurable(char *arguments) {
  char *saved=nullptr,*policy=strtok_r(arguments," ",&saved),*hash=strtok_r(nullptr," ",&saved),
    *generation=strtok_r(nullptr," ",&saved),*ack=strtok_r(nullptr," ",&saved); uint32_t pinned=0;
  if (!policy || strcmp(policy,"V7_START_RESET")!=0 || !hash || !control_rules::validHex(hash,64) ||
      !generation || !control_rules::decimalBounded(generation,0,UINT32_MAX,pinned) ||
      !ack || strcmp(ack,"ACK_VOLATILE_UNKNOWN")!=0 || strtok_r(nullptr," ",&saved)) {
    emuResult("adopt_durable",false,"requires_policy_sha256_generation_and_uncertainty_ack"); return;
  }
  control_rules::lowerHex(hash);
  uint8_t image[256]={}; uint32_t gen=0; char actual[65]={};
  const bool exact=emuLoaded && emuDevice.snapshot(image,gen) && benchHash(image,actual) &&
    bench_rules::check(image).valid() && gen==pinned && strcmp(actual,hash)==0 &&
    strcmp(actual,emuSeedHash)==0 && strcmp(actual,emuIdleHash)==0;
  const adoption_rules::State state={emuFaultCode,emuDevice.generation,emuDurableGeneration,
    emulationActive,emulationSessionOpen,emuSessionMarker,emuUncleanBoot,emuLoaded,
    emuDevice.generation!=emuDurableGeneration,armed,updateBusy,recoveryRequired,
    benchPinsInputOnly(),benchPinsPullsOff(),emuDiagOutputsDetached(),exact};
  if (!adoption_rules::eligible(state,pinned)) { emuResult("adopt_durable",false,"adoption_preimage_or_state_guard_failed"); return; }
  if (!pauseNetworking()) { emuResult("adopt_durable",false,"network_suspend_failed"); return; }
  const bool previousMarker=emuSessionMarker, previousUnclean=emuUncleanBoot;
  emuUncleanBoot=false;
  const bool checkpoint=emuSave(false);
  if (!checkpoint) { emuSessionMarker=previousMarker; emuUncleanBoot=previousUnclean; }
  // This acknowledges a last-durable image, NOT preservation of unobserved RAM.
  Serial.printf("{\"type\":\"emu_result\",\"op\":\"adopt_durable\",\"ok\":%s,\"message\":\"%s\","
    "\"adoption_policy\":\"V7_START_RESET\",\"adopted_durable\":%s,\"pre_reset_volatile_history_unknown\":true,"
    "\"prior_marker\":%s,\"prior_unclean\":%s,\"prior_reset_reason\":%lu,"
    "\"prior_boot_breadcrumb\":{\"valid\":%s,\"stage\":%lu,\"stage_inverse\":%lu},",
    checkpoint?"true":"false",checkpoint?"durable_checkpoint_adopted_volatile_history_unknown":"checkpoint_failed_outcome_uncertain",
    checkpoint?"true":"false",previousMarker?"true":"false",previousUnclean?"true":"false",
    (unsigned long)emuResetReason,emuBootBreadcrumbValid?"true":"false",(unsigned long)emuBootStage,(unsigned long)emuBootInverse);
  emuState(emuDevice.generation); Serial.println("}");
  resumeNetworking();
}
#include "v10_commands.h"
#include "v11_commands.h"
#include "v12_commands.h"
static bool processEmuCommand(char *line) {
  if (strcmp(line,"EMU_STATUS")==0) { emuStatus(); return true; }
  if (strcmp(line,"EMU_SNAPSHOT")==0) { emuSnapshot(); return true; }
  if (strcmp(line,"EMU_DIAG DETACHED")==0) {
    if (pullupConfiguredOn) commandError("pullup_requires_OFF_or_START_or_STOP");
    else emuDetachedDiagnostic();
    return true;
  }
  if (recoveryRequired && strncmp(line,"EMU_",4)==0) { commandError("usb_recovery_required"); return true; }
  if (strcmp(line,"EMU_STOP")==0) { emuStop(); return true; }
  if (strncmp(line,"EMU_PULLUP ",11)==0) { emuPullup(line+11); return true; }
  if (strncmp(line,"EMU_START_PULLUP ",17)==0) { emuStartPullup(line+17); return true; }
  if (pullupConfiguredOn && strcmp(line,"INFO")!=0 && strcmp(line,"NETINFO")!=0 &&
      strncmp(line,"EMU_START ",10)!=0) { commandError("pullup_requires_OFF_or_START_or_STOP"); return true; }
  if (strncmp(line,"EMU_RECOVER_WRITE ",18)==0) { emuRecoverWrite(line+18); return true; }
  if (strncmp(line,"EMU_RECOVER_READ ",17)==0) { emuRecoverRead(line+17); return true; }
  if (strncmp(line,"EMU_RECOVER_RX ",15)==0) { emuRecoverRx(line+15); return true; }
  if (strncmp(line,"EMU_ADOPT_DURABLE ",18)==0) { emuAdoptDurable(line+18); return true; }
  if (strncmp(line,"EMU_RECOVER ",12)==0) { emuRecover(line+12); return true; }
  if (strncmp(line,"EMU_START ",10)==0) { emuStart(line+10); return true; }
  if (strncmp(line,"EMU_LOAD ",9)==0) { emuLoadImage(line+9); return true; }
  if (strncmp(line,"EMU_",4)==0) { commandError("unknown_emulator_command"); return true; }
  if (emulationSessionOpen && strcmp(line,"INFO")!=0 && strcmp(line,"NETINFO")!=0) {
    commandError("emulator_session_requires_EMU_STOP"); return true;
  }
  return false;
}
