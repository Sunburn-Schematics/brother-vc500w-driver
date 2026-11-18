#include "../firmware/polling_i2c/poll_rules.h"
#include <cassert>
#include <cstdio>
#include <cstdint>

int main() {
  using namespace poll_rules;
  constexpr uint32_t cpu = 240;
  constexpr int64_t anchorUs = 0x100000000LL + 1000;
  assert(timestampUs(anchorUs, 1000, 1000, cpu) == anchorUs);
  assert(timestampUs(anchorUs, 1000, 3400, cpu) == anchorUs + 10);
  assert(timestampUs(anchorUs, 3400, 1000, cpu) == anchorUs - 10);
  // A CCOUNT wrap just before the anchor preserves pretrigger/trigger times.
  assert(timestampUs(anchorUs, 16, 0xfffff6b0U, cpu) == anchorUs - 10);
  assert(timestampUs(anchorUs, 0xfffffff0U, 2384, cpu) == anchorUs + 10);
  // Microsecond quantization can round sub-microsecond offsets toward zero.
  assert(timestampUs(anchorUs, 1000, 999, cpu) == anchorUs);
  assert(timestampUs(anchorUs, 1000, 1001, cpu) == anchorUs);
  // A complete <=52 ms window stays monotonic across 32-bit CCOUNT wrap.
  const uint32_t base = 0xfff00000U;
  int64_t previous = timestampUs(anchorUs, base, base, cpu);
  for (uint32_t elapsed = 0; elapsed <= 52000U * cpu; elapsed += 97) {
    const int64_t current = timestampUs(anchorUs, base, base + elapsed, cpu);
    assert(current >= previous && current <= anchorUs + 52000);
    assert(current == anchorUs + elapsed / cpu);
    previous = current;
  }
  // First START can extend by 50 ms, but never beyond the 52 ms/duration budget.
  assert(tailLimit(1999U * cpu, 50000U * cpu, 52000U * cpu) == 51999U * cpu);
  assert(tailLimit(2000U * cpu, 50000U * cpu, 52000U * cpu) == 52000U * cpu);
  assert(tailLimit(500U * cpu, 50000U * cpu, 1000U * cpu) == 1000U * cpu);
  assert(tailLimit(0, 50000U * cpu, 52000U * cpu) == 50000U * cpu);
  std::puts("PASS: bounded trigger deadlines, pre-anchor signed deltas, CCOUNT wrap, full timestamp precision and monotonic 52 ms windows");
}
