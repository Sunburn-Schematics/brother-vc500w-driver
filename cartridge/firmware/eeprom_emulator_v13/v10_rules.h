#pragma once
#include <stdint.h>
namespace v10_rules {
struct State {
  uint32_t fault, live, durable;
  bool active, session, marker, loaded, dirty, armed, update, usbRecovery;
  bool inputOnly, pullsOff, matricesDetached, pullupOff, exactValidSeed;
};
inline bool passive(const State &s) {
  return !s.active && !s.session && !s.marker && !s.armed && !s.update &&
    !s.usbRecovery && s.inputOnly && s.pullsOff && s.matricesDetached;
}
inline bool recoverPointerFault(const State &s, uint32_t pinned) {
  // Only the separately host-provenanced V9 pointer/stale-RX event, never data reset.
  return passive(s) && s.fault==15 && s.loaded && !s.dirty && s.pullupOff &&
    pinned==1 && s.live==1 && s.durable==1 && s.exactValidSeed;
}
inline bool enablePullup(const State &s, bool unclean) {
  return passive(s) && s.fault==0 && s.loaded && !s.dirty && !unclean &&
    s.live==s.durable && s.pullupOff && s.exactValidSeed;
}
}
