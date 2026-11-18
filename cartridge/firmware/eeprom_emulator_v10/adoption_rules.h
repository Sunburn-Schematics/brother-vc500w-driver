#pragma once
#include <stdint.h>
namespace adoption_rules {
struct State {
  uint32_t fault, live, durable;
  bool active, session, marker, unclean, loaded, dirty, armed, update, usbRecovery;
  bool inputs, pullsOff, matrixDetached, exactValidSeed;
};
// Adopt only an explicitly pinned last-durable checkpoint, never assert that
// pre-reset RAM was unchanged. Image bytes/count/generation are not modified.
inline bool eligible(const State &s,uint32_t pinned) {
  return s.fault==0 && s.live==s.durable && s.durable==pinned &&
    !s.active && !s.session && s.marker && s.unclean && s.loaded && !s.dirty &&
    !s.armed && !s.update && !s.usbRecovery && s.inputs && s.pullsOff &&
    s.matrixDetached && s.exactValidSeed;
}
}
