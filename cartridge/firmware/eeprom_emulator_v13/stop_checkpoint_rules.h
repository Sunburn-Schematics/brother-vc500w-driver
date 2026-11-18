#pragma once
#include "virtual_paper_policy.h"
namespace stop_checkpoint_rules {
// Only this reviewed closed fault9 event, never a general fault clear. Boot's
// unclean flag may reflect the saved fault9; a marker/session/dirty state cannot.
inline bool eligible(virtual_paper::PassiveState state,uint32_t fault,bool policyEnabled,
    bool allCrcs,uint32_t generation) {
  state.unclean=false;
  return fault==9 && !policyEnabled && allCrcs && generation==43 &&
    virtual_paper::passive(state,generation);
}
}
