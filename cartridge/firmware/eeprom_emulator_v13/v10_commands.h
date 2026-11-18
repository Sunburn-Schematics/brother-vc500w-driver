#pragma once
#include "v10_rules.h"

static v10_rules::State emuV10State(bool exact) {
  return {emuFaultCode,emuDevice.generation,emuDurableGeneration,emulationActive,
    emulationSessionOpen,emuSessionMarker,emuLoaded,emuDevice.generation!=emuDurableGeneration,
    armed,updateBusy,recoveryRequired,benchPinsInputOnly(),benchPinsPullsOff(),
    emuDiagOutputsDetached(),pullupOffVerified(),exact};
}
static bool emuCurrentImage(const char *hash,uint32_t pinned,bool requireSeed) {
  uint8_t image[256]={}; uint32_t gen=0; char actual[65]={};
  return emuLoaded && emuDevice.snapshot(image,gen) && gen==pinned &&
    bench_rules::check(image).valid() && benchHash(image,actual) &&
    strcmp(actual,hash)==0 && strcmp(actual,emuIdleHash)==0 &&
    (!requireSeed || strcmp(actual,emuSeedHash)==0);
}
static void emuRecoverRx(char *arguments) {
  char *saved=nullptr,*policy=strtok_r(arguments," ",&saved),*hash=strtok_r(nullptr," ",&saved),
    *generation=strtok_r(nullptr," ",&saved); uint32_t pinned=0;
  if (!policy || strcmp(policy,"V9_POINTER_UNCHANGED")!=0 || !hash || !control_rules::validHex(hash,64) ||
      !generation || !control_rules::decimalBounded(generation,1,1,pinned) || strtok_r(nullptr," ",&saved)) {
    emuResult("recover_rx",false,"requires_v9_pointer_sha256_generation_1"); return;
  }
  control_rules::lowerHex(hash);
  const bool exact=strcmp(hash,"d7c0db4edb0db564e8980ba26b8ebf6f95082685aa4e0b40c8c8630b4715e091")==0 &&
    emuCurrentImage(hash,pinned,true);
  if (!v10_rules::recoverPointerFault(emuV10State(exact),pinned)) {
    emuResult("recover_rx",false,"recovery_preimage_or_state_guard_failed"); return;
  }
  if (!pauseNetworking()) { emuResult("recover_rx",false,"network_suspend_failed"); return; }
  const bool previousUnclean=emuUncleanBoot;
  emuFaultCode=0; emuUncleanBoot=false;
  const bool checkpoint=emuSave(false);
  if (!checkpoint) { emuFaultCode=15; emuUncleanBoot=previousUnclean; }
  Serial.printf("{\"type\":\"emu_result\",\"op\":\"recover_rx\",\"ok\":%s,\"message\":\"%s\","
    "\"recovery_policy\":\"V9_POINTER_UNCHANGED\",\"prior_fault_code\":15,",
    checkpoint?"true":"false",checkpoint?"unchanged_pointer_fault_metadata_cleared":"checkpoint_failed_outcome_uncertain");
  emuState(emuDevice.generation); Serial.println("}"); resumeNetworking();
}
static void emuPullup(char *arguments) {
  char *saved=nullptr,*mode=strtok_r(arguments," ",&saved),*power=strtok_r(nullptr," ",&saved);
  if (mode && strcmp(mode,"OFF")==0 && !power) {
    if (emulationActive || emulationSessionOpen || armed || updateBusy || recoveryRequired ||
        !benchPinsInputOnly() || !benchPinsPullsOff()) {
      emuResult("pullup_off",false,"requires_closed_passive_state"); return;
    }
    const bool ok=pullupOff();
    if (!ok) updateBusy=true;
    emuResult("pullup_off",ok,ok?"gpio19_high_z":"gpio19_release_failed");
    if (ok) resumeNetworking(); return;
  }
  if (!mode || strcmp(mode,"ON")!=0 || !power || strcmp(power,"PRINTER_POWERED")!=0 ||
      strtok_r(nullptr," ",&saved)) {
    emuResult("pullup_on",false,"requires_ON_PRINTER_POWERED_or_OFF"); return;
  }
  const bool exact=emuCurrentImage(emuIdleHash,emuDurableGeneration,false);
  if (!v10_rules::enablePullup(emuV10State(exact),emuUncleanBoot)) {
    emuResult("pullup_on",false,"requires_clean_closed_passive_state"); return;
  }
  if (!pauseNetworking()) { emuResult("pullup_on",false,"network_suspend_failed"); return; }
  const bool ok=pullupOn();
  emuResult("pullup_on",ok,ok?"gpio19_high_via_resistor":"gpio19_enable_failed");
  if (!ok) resumeNetworking();
}
static void emuStartPullup(char *arguments) {
  char *saved=nullptr,*ownership=strtok_r(arguments," ",&saved),*hash=strtok_r(nullptr," ",&saved),
    *generation=strtok_r(nullptr," ",&saved),*power=strtok_r(nullptr," ",&saved); uint32_t pinned=0;
  if (!ownership || strcmp(ownership,"SOLE_0X50")!=0 || !hash || !control_rules::validHex(hash,64) ||
      !generation || !control_rules::decimalBounded(generation,0,UINT32_MAX-1,pinned) ||
      !power || strcmp(power,"PRINTER_POWERED")!=0 || strtok_r(nullptr," ",&saved)) {
    emuResult("start",false,"requires_ownership_sha256_generation_PRINTER_POWERED"); return;
  }
  control_rules::lowerHex(hash);
  const bool exact=pinned==emuDurableGeneration && emuCurrentImage(hash,pinned,false);
  if (!v10_rules::enablePullup(emuV10State(exact),emuUncleanBoot) ||
      xPortGetCoreID()!=1 || (busLevels()&1)==0) {
    emuResult("start",false,"pullup_start_preimage_or_passive_guard_failed"); return;
  }
  // Prepare all potentially slow work before raising the physical presence input.
  if (!pauseNetworking() || !emuSave(true)) {
    emuFaultCode=11; emuResult("start",false,"marker_or_network_failed"); return;
  }
  emuRecordStage(17); memset((void *)&emuMetrics,0,sizeof(emuMetrics));
  emuHistoryNext=emuHistoryCount=0; emulationSessionOpen=true;
  emuStartPhase("marker_saved"); // GPIO19 is still verified high-Z here.
  const bool enabled=pullupOn();
  const uint32_t begin=micros();
  while (enabled && busLevels()!=3 && uint32_t(micros()-begin)<10000) delayMicroseconds(10);
  if (!enabled || busLevels()!=3 || !emuAttachHardware()) {
    emuFaultCode=12; emuReleaseHardware();
    if (!pullupOff() || !benchPinsInputOnly() || !benchPinsPullsOff()) emuFaultCode=13;
    emuResult("start",false,"hardware_attach_failed"); return;
  }
  emuResult("start",true,"experimental_responder_enabled");
}
