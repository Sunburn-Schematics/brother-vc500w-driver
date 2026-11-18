#pragma once
#include <stdint.h>

namespace poll_rules {
// Caller bounds elapsed/tail/budget below 2^31; this is used in the hot loop.
inline __attribute__((always_inline)) uint32_t tailLimit(uint32_t elapsedCycles,
                                                       uint32_t tailCycles,
                                                       uint32_t budgetCycles) {
  const uint32_t extended = elapsedCycles + tailCycles;
  return extended < budgetCycles ? extended : budgetCycles;
}
// Every retained window is <=102 ms. Both preceding/following anchor records
// are therefore within signed32 cycles at 240 MHz, including CCOUNT wrap.
inline int64_t timestampUs(int64_t anchorUs, uint32_t anchorCycles,
                           uint32_t eventCycles, uint32_t cyclesPerUs) {
  const uint32_t difference = eventCycles - anchorCycles;
  const int64_t signedDifference = difference <= 0x7fffffffU ? int64_t(difference) :
                                  int64_t(difference) - 0x100000000LL;
  return anchorUs + signedDifference / cyclesPerUs;
}
} // namespace poll_rules
