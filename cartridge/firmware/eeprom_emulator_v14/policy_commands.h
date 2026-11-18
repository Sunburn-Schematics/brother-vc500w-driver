#pragma once
#include "stop_checkpoint_rules.h"
static virtual_paper::PassiveState emuPolicyPassive(bool exact) {
  virtual_paper::PassiveState state;
  state.live=emuDevice.generation;state.durable=emuDurableGeneration;
  state.loaded=emuLoaded;state.active=emulationActive;state.session=emulationSessionOpen;
  state.marker=emuSessionMarker;state.dirty=emuDevice.generation!=emuDurableGeneration;
  state.unclean=emuUncleanBoot;state.armed=armed;state.update=updateBusy;state.recovery=recoveryRequired;
  state.inputs=benchPinsInputOnly();state.pullsOff=benchPinsPullsOff();state.pullupOff=pullupOffVerified();
  state.matricesDetached=emuDiagOutputsDetached();state.exactPreimagePin=exact;
  return state;
}
static void emuPolicyState() {
  const uint32_t gen=emuPolicy.enabled?emuPolicy.viewGeneration:emuDevice.generation;
  const uint16_t initial=uint16_t(emuDevice.image[0x60])|uint16_t(emuDevice.image[0x61])<<8;
  const uint16_t remaining=uint16_t(emuDevice.image[0xe8])|uint16_t(emuDevice.image[0xe9])<<8;
  Serial.printf(",\"policy\":{\"mode\":\"initialization_budget_v1\",\"enabled\":%s,\"persisted\":false,"
    "\"capacity_counts\":%u,\"view_generation\":%lu,\"refresh_events\":%lu,"
    "\"backing_initial_raw\":%u,\"backing_remaining_raw\":%u,"
    "\"advertised_initial_raw\":%u,\"advertised_remaining_raw\":%u,"
    "\"write_verification_pending\":%s,\"cache_refresh_required\":%s,\"suspended\":%s,"
    "\"initialization_reads\":%lu,\"verification_reads\":%lu,\"blocked_refreshes\":%lu,\"last_init_read_bytes\":%lu}",
    emuPolicy.enabled?"true":"false",emuPolicy.capacity,(unsigned long)gen,(unsigned long)emuPolicy.refreshEvents,
    initial,remaining,emuPolicy.enabled?emuPolicy.word(0x60):initial,emuPolicy.enabled?emuPolicy.word(0xe8):remaining,
    emuPolicy.verificationPending?"true":"false",emuPolicy.cacheRefreshRequired?"true":"false",
    emuPolicy.suspended?"true":"false",(unsigned long)emuPolicy.initializationReads,
    (unsigned long)emuPolicy.verificationReads,(unsigned long)emuPolicy.blockedRefreshes,
    (unsigned long)emuPolicy.lastInitReadBytes);
}
static void emuViewSnapshot() {
  uint8_t image[256]={};uint32_t gen=0;char hash[65]={};
  const bool valid=emuLoaded && (emuPolicy.enabled?emuPolicy.snapshot(image,gen):emuDevice.snapshot(image,gen));
  if (!valid || !benchHash(image,hash)) {emuResult("view_snapshot",false,"snapshot_busy_or_unloaded");return;}
  const bench_rules::Crcs checks=bench_rules::check(image);
  Serial.print("{\"type\":\"emu_view_snapshot\",\"image_kind\":\"advertised\",\"ok\":true,\"bytes\":256,\"hex\":\"");
  for (unsigned i=0;i<256;++i) Serial.printf("%02x",image[i]);
  const uint32_t live=emuPolicy.enabled?emuPolicy.viewGeneration:emuDevice.generation;
  Serial.printf("\",\"sha256\":\"%s\",\"image_generation\":%lu,\"snapshot_is_current\":%s,"
    "\"crc_r0\":%s,\"crc_rw1\":%s,\"crc_rw2\":%s,",hash,(unsigned long)gen,gen==live?"true":"false",
    checks.r0?"true":"false",checks.rw1?"true":"false",checks.rw2?"true":"false");
  emuState(emuDevice.generation);Serial.println("}");
}
static void emuPolicyCommand(char *arguments) {
  char *saved=nullptr,*op=strtok_r(arguments," ",&saved),*hash=strtok_r(nullptr," ",&saved),
    *generation=strtok_r(nullptr," ",&saved),*capacity=strtok_r(nullptr," ",&saved);
  uint32_t pinned=0,budget=0;
  const bool enable=op && strcmp(op,"ENABLE")==0,disable=op && strcmp(op,"DISABLE")==0;
  if ((!enable && !disable) || !hash || !control_rules::validHex(hash,64) || !generation ||
      !control_rules::decimalBounded(generation,0,UINT32_MAX-1,pinned) ||
      (enable && (!capacity || !control_rules::decimalBounded(capacity,1,virtual_paper::MAX_CAPACITY,budget))) ||
      (disable && capacity) || strtok_r(nullptr," ",&saved)) {
    emuResult("policy",false,"requires_ENABLE_sha_generation_capacity_or_DISABLE_sha_generation");return;
  }
  control_rules::lowerHex(hash);
  uint8_t image[256]={};uint32_t gen=0;char actual[65]={};
  const bool exact=emuLoaded && emuDevice.snapshot(image,gen) && gen==pinned && benchHash(image,actual) &&
    strcmp(actual,hash)==0 && strcmp(actual,emuIdleHash)==0;
  if (!exact || !virtual_paper::passive(emuPolicyPassive(exact),pinned) || emuFaultCode) {
    emuResult(enable?"policy_enable":"policy_disable",false,"policy_passive_preimage_guard_failed");return;
  }
  const bool ok=enable?emuPolicy.enable(emuPolicyPassive(exact),pinned,image,uint16_t(budget)):
    emuPolicy.disable(emuPolicyPassive(exact),pinned);
  emuResult(enable?"policy_enable":"policy_disable",ok,
    ok?(enable?"virtual_initialization_budget_enabled_backing_unchanged":"policy_disabled_latest_backing_preserved"):
      "policy_state_or_capacity_failed");
}
static void emuRecoverStopCheckpoint(char *arguments) {
  char *saved=nullptr,*hash=strtok_r(arguments," ",&saved),*generation=strtok_r(nullptr," ",&saved);
  uint32_t pinned=0;
  if (!hash || !control_rules::validHex(hash,64) || !generation ||
      !control_rules::decimalBounded(generation,0,UINT32_MAX-1,pinned) || strtok_r(nullptr," ",&saved)) {
    emuResult("recover_stop_checkpoint",false,"requires_checkpoint_sha256_generation");return;
  }
  control_rules::lowerHex(hash);
  uint8_t image[256]={};uint32_t gen=0;char actual[65]={};
  const bool exact=pinned==63 && strcmp(hash,"50ce837a5bd0d10e347cc665af5c6df5340fbafbe7579e70a0dff437bcde48ed")==0 &&
    emuLoaded && emuDevice.snapshot(image,gen) && gen==pinned && benchHash(image,actual) &&
    strcmp(actual,hash)==0 && strcmp(actual,emuIdleHash)==0 && bench_rules::check(image).valid();
  const auto state=emuPolicyPassive(exact);
  if (!exact || !stop_checkpoint_rules::eligible(state,emuFaultCode,emuPolicy.enabled,
      bench_rules::check(image).valid(),pinned)) {
    emuResult("recover_stop_checkpoint",false,"exact_closed_fault9_checkpoint_required");return;
  }
  if (!pauseNetworking()) {emuResult("recover_stop_checkpoint",false,"network_suspend_failed");return;}
  const bool priorUnclean=emuUncleanBoot;emuFaultCode=0;emuUncleanBoot=false;
  if (!emuSave(false)) {
    emuFaultCode=9;emuUncleanBoot=priorUnclean;
    emuResult("recover_stop_checkpoint",false,"checkpoint_failed_outcome_uncertain");
  } else emuResult("recover_stop_checkpoint",true,"verified_checkpoint_fault_metadata_cleared");
  resumeNetworking();
}
