#pragma once
#include "emulator_hw.h"

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
    case 11:return "checkpoint_failed"; default:return "unknown_fault";
  }
}
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
  if (!pauseNetworking() || !emuSave(true)) { emuFaultCode=11; emuResult("start",false,"marker_or_network_failed"); return; }
  memset((void *)&emuMetrics,0,sizeof(emuMetrics));
  emulationSessionOpen=true;
  if (!emuAttachHardware()) {
    emuFaultCode=10; emuReleaseHardware(); emuResult("start",false,"hardware_attach_failed"); return;
  }
  emuResult("start",true,"experimental_responder_enabled");
}
static void emuStop() {
  if (!emulationSessionOpen) { emuResult("stop",true,"already_passive_marker_preserved"); return; }
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
  if (!benchPinsInputOnly() || !benchPinsPullsOff()) {
    emuFaultCode=10; updateBusy=true; emuResult("stop",false,"gpio_release_failed"); return;
  }
  if (!emuSave(false)) { emuFaultCode=11; emuResult("stop",false,"checkpoint_failed"); return; }
  emulationSessionOpen=false;
  emuResult("stop",emuFaultCode==0,emuFaultCode?"detached_saved_with_fault":"detached_and_durable");
  resumeNetworking();
}
static bool processEmuCommand(char *line) {
  if (strcmp(line,"EMU_STATUS")==0) { emuStatus(); return true; }
  if (strcmp(line,"EMU_SNAPSHOT")==0) { emuSnapshot(); return true; }
  if (recoveryRequired && strncmp(line,"EMU_",4)==0) { commandError("usb_recovery_required"); return true; }
  if (strcmp(line,"EMU_STOP")==0) { emuStop(); return true; }
  if (strncmp(line,"EMU_START ",10)==0) { emuStart(line+10); return true; }
  if (strncmp(line,"EMU_LOAD ",9)==0) { emuLoadImage(line+9); return true; }
  if (strncmp(line,"EMU_",4)==0) { commandError("unknown_emulator_command"); return true; }
  if (emulationSessionOpen && strcmp(line,"INFO")!=0 && strcmp(line,"NETINFO")!=0) {
    commandError("emulator_session_requires_EMU_STOP"); return true;
  }
  return false;
}
