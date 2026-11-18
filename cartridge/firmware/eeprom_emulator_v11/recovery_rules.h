#pragma once
#include <stdint.h>

namespace emu_recovery {
struct State {
  uint32_t fault, live, durable;
  bool active, session, marker, loaded, dirty, armed, update, usbRecovery;
  bool inputOnly, pullsOff, exactValidSeed;
};
// This is only an explicit acknowledgement of the reviewed V6 failed attach.
// It cannot reset data/generation or clear a new V7 peripheral transport fault.
inline bool eligible(const State &s, uint32_t pinnedGeneration) {
  return s.fault==10 && s.live==s.durable && s.durable==pinnedGeneration &&
    !s.active && !s.session && !s.marker && s.loaded && !s.dirty &&
    !s.armed && !s.update && !s.usbRecovery && s.inputOnly && s.pullsOff && s.exactValidSeed;
}
}
