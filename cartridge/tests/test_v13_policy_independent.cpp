#include "../firmware/eeprom_emulator_v13/virtual_paper_policy.h"
#include "../firmware/eeprom_emulator_v13/stop_checkpoint_rules.h"

#include <array>
#include <cstdlib>
#include <iostream>

// Independent behavioral qualification: synthetic bytes only, no hardware,
// serial, vendor execution, production image, or application integration.
namespace {
unsigned checks = 0;
void require(bool ok, const char *message) {
  ++checks;
  if (!ok) {
    std::cerr << "FAIL: " << message << '\n';
    std::exit(1);
  }
}
using Image = std::array<uint8_t, 256>;
uint16_t word(const Image &image, unsigned at) {
  return uint16_t(image[at]) | uint16_t(image[at + 1]) << 8;
}
void word(Image &image, unsigned at, uint16_t value) {
  image[at] = uint8_t(value);
  image[at + 1] = uint8_t(value >> 8);
}
uint16_t referenceCrc(const uint8_t *bytes, unsigned count) {
  uint16_t reg = 0xffff;
  for (unsigned i = 0; i < count; ++i) {
    for (unsigned bit = 0; bit < 8; ++bit) {
      const bool feedback = (reg & 1) != ((bytes[i] >> bit) & 1);
      reg >>= 1;
      if (feedback) reg ^= 0x8408;
    }
  }
  return uint16_t(~reg);
}
void fixCrcs(Image &image) {
  word(image, 0xae, referenceCrc(image.data(), 174));
  word(image, 0xe6, referenceCrc(image.data() + 0xd0, 22));
  word(image, 0xf6, referenceCrc(image.data() + 0xe8, 14));
}
bool valid(const Image &image) {
  return word(image, 0xae) == referenceCrc(image.data(), 174) &&
         word(image, 0xe6) == referenceCrc(image.data() + 0xd0, 22) &&
         word(image, 0xf6) == referenceCrc(image.data() + 0xe8, 14);
}
Image fixture() {
  Image image{};
  for (unsigned i = 0; i < image.size(); ++i)
    image[i] = uint8_t((i * 29U + 17U) & 255U);
  image[0] = 9;
  image[1] = 2;
  image[0x10] = 'B';
  image[0xea] = 0;
  word(image, 0x60, 6256);
  word(image, 0xe8, 4);
  fixCrcs(image);
  return image;
}
Image projected(Image image, uint16_t capacity = 11232) {
  word(image, 0x60, capacity);
  word(image, 0xe8, capacity);
  fixCrcs(image);
  return image;
}
virtual_paper::PassiveState passive(uint32_t generation) {
  virtual_paper::PassiveState state;
  state.loaded = true;
  state.live = state.durable = generation;
  state.exactPreimagePin = true;
  return state;
}
Image view(const virtual_paper::Policy &policy) {
  Image image{};
  uint32_t generation = 0;
  require(policy.snapshot(image.data(), generation), "coherent policy snapshot");
  require(generation == policy.viewGeneration, "snapshot generation is current");
  return image;
}
Image backing(const eeprom_model::Device &device) {
  Image image{};
  uint32_t generation = 0;
  require(device.snapshot(image.data(), generation), "coherent backing snapshot");
  require(generation == device.generation, "backing generation is current");
  return image;
}
bool write(eeprom_model::Device &device, virtual_paper::Policy &policy,
           uint8_t address, const uint8_t *data, unsigned count) {
  device.start();
  device.receive(address);
  for (unsigned i = 0; i < count; ++i) device.receive(data[i]);
  const uint8_t base = device.pageBase;
  const uint16_t mask = device.pageMask;
  require(device.stop(true), "qualified physical-model page commit");
  return policy.acceptedWrite(device.image, device.generation, base, mask);
}
void completeRead(virtual_paper::Policy &policy, uint8_t pointer,
                  uint32_t generation, unsigned count) {
  require(policy.beginRead(pointer, generation), "begin qualified read");
  policy.readNack(count);
  policy.readStop();
}
void projectionAndNormalPrint() {
  const Image original = fixture();
  eeprom_model::Device device;
  device.load(original.data(), 43);
  virtual_paper::Policy policy;
  require(policy.enable(passive(43), 43, original.data(), 11232), "passive enable");
  const Image advertised = view(policy);
  require(advertised == projected(original), "exact independent full-image projection");
  require(valid(advertised), "all projected CRC regions valid");
  require(word(advertised, 0x60) == 11232 && word(advertised, 0xe8) == 11232,
          "near-empty logical count has full virtual initial and remaining");
  require(backing(device) == original && device.generation == 43,
          "enable never modifies backing bytes or generation");
  for (unsigned i = 0; i < original.size(); ++i) {
    const bool allowed = i == 0x60 || i == 0x61 || i == 0xae || i == 0xaf ||
                         i == 0xe8 || i == 0xe9 || i == 0xf6 || i == 0xf7;
    require(allowed || advertised[i] == original[i], "projection eight-offset allowlist");
    if (i < 32) require(advertised[i] == original[i], "initial FIFO prefix unchanged");
  }
  completeRead(policy, 0, device.generation, 256);
  require(policy.initializationReads == 1 && !policy.cacheRefreshRequired,
          "completed full-read telemetry without claiming physical insertion");
  const uint32_t refreshBeforePrint = policy.refreshEvents;
  completeRead(policy, 0xe8, device.generation, 16);
  require(policy.refreshEvents == refreshBeforePrint, "pre-write RW2 read never refills");

  Image printerWritten = advertised;
  word(printerWritten, 0xe8, 11189);
  word(printerWritten, 0xf6, referenceCrc(printerWritten.data() + 0xe8, 14));
  require(write(device, policy, 0xe8, printerWritten.data() + 0xe8, 8),
          "first observed eight-byte printer write mirrored");
  require(policy.verificationPending, "partial write holds verification pending");
  Image interim = view(policy);
  for (unsigned i = 0; i < 8; ++i)
    require(interim[0xe8 + i] == printerWritten[0xe8 + i], "first write exact payload");
  require(interim[0xf6] == advertised[0xf6] && interim[0xf7] == advertised[0xf7],
          "intermediate CRC remains exact old bytes without repair");
  require(!valid(interim), "intermediate CRC-invalid write is represented honestly");
  completeRead(policy, 0, device.generation, 256);
  require(policy.refreshEvents == refreshBeforePrint && policy.verificationPending,
          "mid-write pointer-zero read cannot replenish or discharge pending write");

  require(write(device, policy, 0xf0, printerWritten.data() + 0xf0, 8),
          "second observed eight-byte printer write mirrored");
  const Image committed = view(policy);
  require(committed == printerWritten && valid(committed), "view preserves exact printer write+CRC");
  require(word(backing(device), 0x60) == 6256 && word(backing(device), 0xe8) == 11189,
          "backing preserves original initial and latest supplied virtual-counter write");
  completeRead(policy, 0xe8, device.generation, 16);
  require(!policy.verificationPending && policy.verificationReads == 1,
          "complete exact RW2 verification clears pending");
  require(view(policy) == committed && policy.refreshEvents == refreshBeforePrint,
          "post-verification view remains consumed, never automatically replenished");
  completeRead(policy, 0xf8, device.generation, 256);
  require(view(policy) == committed && policy.refreshEvents == refreshBeforePrint,
          "read wrapping through address zero is not an initialization trigger");
  require(policy.beginRead(0, device.generation, false), "current-address read at zero accepted");
  policy.readNack(256);
  policy.readStop();
  require(view(policy) == committed && policy.refreshEvents == refreshBeforePrint,
          "current-address read at wrapped zero does not refill without received pointer");
  completeRead(policy, 0, device.generation, 256);
  require(word(view(policy), 0xe8) == 11232 && valid(view(policy)), "next pointer-zero initialization refills");
  require(policy.refreshEvents == refreshBeforePrint + 1 && policy.initializationReads == 2,
          "one later full-read refresh counted once");
  const Image latest = backing(device);
  require(policy.disable(passive(device.generation), device.generation), "clean disable at latest generation");
  for (unsigned i = 0; i < latest.size(); ++i)
    require(policy.byte(uint8_t(i), latest[i]) == latest[i], "disable serves newest backing, never old seed");
  require(backing(device) == latest, "disable does not change latest backing");
}
void guardsAndForeignWrites() {
  const Image original = fixture();
  auto state = passive(10);
  state.active = true;
  virtual_paper::Policy rejected;
  require(!rejected.enable(state, 10, original.data(), 11232), "active enable rejected");
  require(!rejected.enable(passive(10), 10, original.data(), 11233), "above-vendor-limit capacity rejected");
  Image corrupt = original;
  corrupt[0x70] ^= 1;
  require(!rejected.enable(passive(10), 10, corrupt.data(), 11232), "bad source CRC rejected");
  Image wristband = original;
  wristband[1] = 3;
  fixCrcs(wristband);
  require(!rejected.enable(passive(10), 10, wristband.data(), 11232), "wristband revision rejected");
  eeprom_model::Device device;
  device.load(original.data(), 10);
  virtual_paper::Policy policy;
  require(policy.enable(passive(10), 10, original.data(), 11232), "foreign test enable");
  const uint8_t data = uint8_t(original[0x20] ^ 0x5a);
  const uint32_t refreshes = policy.refreshEvents;
  require(!write(device, policy, 0x20, &data, 1) && policy.suspended,
          "foreign accepted write suspends future virtual refresh");
  require(backing(device)[0x20] == data && view(policy)[0x20] == data,
          "foreign supplied data remains mirrored rather than rewritten");
  completeRead(policy, 0, device.generation, 256);
  require(policy.refreshEvents == refreshes, "foreign-write suspension blocks initialization refill");
  require(policy.disable(passive(device.generation), device.generation), "foreign state can disable cleanly");
  const Image latest = backing(device);
  require(policy.byte(0x20, latest[0x20]) == data, "disable keeps foreign latest byte");

  virtual_paper::Policy mismatch;
  require(mismatch.enable(passive(10), 10, original.data(), 11232), "mismatch test enable");
  const Image before = view(mismatch);
  require(!mismatch.beginRead(0, 11) && mismatch.suspended,
          "unexplained backing generation fails closed");
  require(view(mismatch) == before, "generation mismatch cannot refill");
}
void actualIdleReadDirectionRegression() {
  // Actual retained V12 IRQ status words: TXEMPTY after a read STOP retained
  // slave_rw=1, while the live bus_busy bit was0. A later read has both bits1.
  const uint32_t idleAfterRead = 109051906;
  const uint32_t busyRead = 108789779;
  const uint32_t pointerWrite = 92274992;
  auto trigger = [](uint32_t status, bool enabled, bool alreadyActive) {
    return virtual_paper::shouldBeginRead(enabled, (status & 2) != 0,
                                         (status & 16) != 0, alreadyActive);
  };
  require((idleAfterRead & 2) != 0 && (idleAfterRead & 16) == 0,
          "actual post-STOP status retains direction but is idle");
  require(!trigger(idleAfterRead, true, false), "idle TXEMPTY cannot start a virtual read");
  require(trigger(busyRead, true, false), "active first read can initialize view");
  require(!trigger(busyRead, true, true), "later IRQ in same read cannot refresh again");
  require(!trigger(busyRead, false, false), "disabled policy retains original adapter path");
  require(!trigger(pointerWrite, true, false), "received pointer write is not a read trigger");
}
void narrowStopCheckpointGuard() {
  auto state = passive(43);
  state.unclean = true;  // The persisted fault9 may explain this flag.
  require(stop_checkpoint_rules::eligible(state, 9, false, true, 43),
          "only reviewed clean closed fault9 checkpoint can recover");
  require(!stop_checkpoint_rules::eligible(state, 0, false, true, 43), "wrong fault cannot recover");
  require(!stop_checkpoint_rules::eligible(state, 9, true, true, 43), "enabled policy cannot recover");
  require(!stop_checkpoint_rules::eligible(state, 9, false, false, 43), "bad CRC cannot recover");
  require(!stop_checkpoint_rules::eligible(state, 9, false, true, 44), "other generation cannot recover");
  state.marker = true;
  require(!stop_checkpoint_rules::eligible(state, 9, false, true, 43), "active marker cannot be excused");
  state.marker = false;
  state.dirty = true;
  require(!stop_checkpoint_rules::eligible(state, 9, false, true, 43), "dirty RAM cannot be excused");
  state.dirty = false;
  state.exactPreimagePin = false;
  require(!stop_checkpoint_rules::eligible(state, 9, false, true, 43), "unknown preimage cannot recover");
}
} // namespace
int main() {
  const uint8_t vector[] = {'1','2','3','4','5','6','7','8','9'};
  require(referenceCrc(vector, 9) == 0x906e, "independent X25 check vector");
  require(valid(fixture()), "synthetic fixture has three valid CRCs");
  projectionAndNormalPrint();
  guardsAndForeignWrites();
  actualIdleReadDirectionRegression();
  narrowStopCheckpointGuard();
  std::cout << "PASS: " << checks << " independent V13 policy assertions\n";
}
