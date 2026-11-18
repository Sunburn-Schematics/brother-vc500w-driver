#pragma once
#include "write_recovery_rules.h"
static void emuRecoverWrite(char *arguments) {
  char *saved=nullptr,*policy=strtok_r(arguments," ",&saved),*hash=strtok_r(nullptr," ",&saved),
    *generation=strtok_r(nullptr," ",&saved); uint32_t pinned=0;
  if (!policy || strcmp(policy,"V11_WRITE_UNCHANGED")!=0 || !hash || !control_rules::validHex(hash,64) ||
      !generation || !control_rules::decimalBounded(generation,1,1,pinned) || strtok_r(nullptr," ",&saved)) {
    emuResult("recover_write",false,"requires_v11_write_sha256_generation_1"); return;
  }
  control_rules::lowerHex(hash);
  const bool exact=strcmp(hash,"d7c0db4edb0db564e8980ba26b8ebf6f95082685aa4e0b40c8c8630b4715e091")==0 &&
    emuCurrentImage(hash,pinned,true);
  if (!write_recovery_rules::eligible(emuV10State(exact),pinned)) {
    emuResult("recover_write",false,"recovery_preimage_or_state_guard_failed"); return;
  }
  if (!pauseNetworking()) { emuResult("recover_write",false,"network_suspend_failed"); return; }
  const bool previousUnclean=emuUncleanBoot;
  emuFaultCode=0; emuUncleanBoot=false;
  const bool checkpoint=emuSave(false);
  if (!checkpoint) { emuFaultCode=7; emuUncleanBoot=previousUnclean; }
  Serial.printf("{\"type\":\"emu_result\",\"op\":\"recover_write\",\"ok\":%s,\"message\":\"%s\","
    "\"recovery_policy\":\"V11_WRITE_UNCHANGED\",\"prior_fault_code\":7,\"discarded_payload_unknown\":true,",
    checkpoint?"true":"false",checkpoint?"unchanged_write_fault_metadata_cleared":"checkpoint_failed_outcome_uncertain");
  emuState(emuDevice.generation); Serial.println("}"); resumeNetworking();
}
