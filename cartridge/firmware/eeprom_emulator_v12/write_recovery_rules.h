#pragma once
#include "v10_rules.h"
namespace write_recovery_rules {
inline bool eligible(const v10_rules::State &s,uint32_t pinned) {
  return v10_rules::passive(s) && s.fault==7 && s.loaded && !s.dirty && s.pullupOff &&
    pinned==1 && s.live==1 && s.durable==1 && s.exactValidSeed;
}
}
