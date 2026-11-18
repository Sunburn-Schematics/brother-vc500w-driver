#pragma once
#include <stddef.h>
#include <stdint.h>
#include <string.h>

// Static layout is qualified separately against the physical preimage. This
// module offers one patch only; it is not an arbitrary EEPROM writer.
namespace bench_rules {
constexpr size_t IMAGE_BYTES = 256;
constexpr uint8_t ADDRESS = 0x50;
constexpr uint32_t HALF_PERIOD_US = 100; // At most 5 kHz, plus software overhead.
constexpr uint32_t RISE_TIMEOUT_US = 1000;
constexpr uint32_t ACK_TIMEOUT_US = 10000;
constexpr uint32_t TOTAL_TIMEOUT_US = 8000000;

inline uint16_t u16le(const uint8_t *p) { return uint16_t(p[0]) | uint16_t(p[1]) << 8; }
inline uint16_t crc16(const uint8_t *p, size_t length) {
  uint16_t crc = 0xffff;
  for (size_t i = 0; i < length; ++i) {
    crc ^= p[i];
    for (unsigned bit = 0; bit < 8; ++bit)
      crc = uint16_t((crc >> 1) ^ ((crc & 1) ? 0x8408 : 0));
  }
  return uint16_t(crc ^ 0xffff);
}
struct Crcs { bool r0; bool rw1; bool rw2; bool valid() const { return r0 && rw1 && rw2; } };
inline Crcs check(const uint8_t *image) {
  return {crc16(image, 174) == u16le(image + 174),
          crc16(image + 208, 22) == u16le(image + 230),
          crc16(image + 232, 14) == u16le(image + 246)};
}
inline bool pageBounded(uint16_t address, size_t length) {
  return length > 0 && length <= 16 && address < IMAGE_BYTES &&
         length <= IMAGE_BYTES - address && (address & 15) + length <= 16;
}
inline const char *restoreImage(const uint8_t *before, uint8_t *after) {
  if (!check(before).valid()) return "preimage_crc_invalid";
  const uint16_t initial = u16le(before + 0x60);
  const uint16_t remaining = u16le(before + 0xe8);
  if (before[1] == 3) return "wristband_revision_not_supported";
  if (initial == 0 || initial == 0xffff) return "initial_length_invalid";
  if (remaining > initial) return "remaining_exceeds_initial";
  memcpy(after, before, IMAGE_BYTES);
  after[0xe8] = before[0x60]; after[0xe9] = before[0x61];
  const uint16_t crc = crc16(after + 0xe8, 14);
  after[0xf6] = uint8_t(crc); after[0xf7] = uint8_t(crc >> 8);
  for (size_t i = 0; i < IMAGE_BYTES; ++i)
    if (before[i] != after[i] && i != 0xe8 && i != 0xe9 && i != 0xf6 && i != 0xf7)
      return "patch_allowlist_failed";
  return check(after).valid() ? nullptr : "expected_crc_invalid";
}

// IO is deliberately restricted to driving low, releasing, sampling and time.
// No drive-high or bus-clear operation exists. A failed operation does not
// perform extra clocks or synthesize a STOP; the owner releases both pins.
template<class IO> class Master {
 public:
  explicit Master(IO &io) : io_(io), startUs_(io.nowUs()) {}
  const char *error() const { return error_; }
  bool expired() {
    if (failIf(io_.nowUs() - startUs_ >= TOTAL_TIMEOUT_US, "bench_total_timeout")) return true;
    return failIf(io_.nowUs() >= phaseDeadline_, "write_ack_timeout");
  }

  bool start(bool repeated = false) {
    if (error_ || expired()) return false;
    io_.releaseSda();
    if (!high(false)) return false;
    io_.releaseScl();
    if (!high(true) || !wait(HALF_PERIOD_US)) return false;
    // Both pins must be high at the start of an isolated transaction.
    if (!io_.sda() || !io_.scl()) return setError("bus_not_idle");
    if (!repeated && !wait(HALF_PERIOD_US)) return false;
    io_.lowSda();
    if (!wait(HALF_PERIOD_US)) return false;
    io_.lowScl();
    return true;
  }
  bool stop() {
    if (error_ || expired()) return false;
    io_.lowScl(); io_.lowSda();
    if (!wait(HALF_PERIOD_US)) return false;
    io_.releaseScl();
    if (!high(true) || !wait(HALF_PERIOD_US)) return false;
    io_.releaseSda();
    if (!high(false) || !wait(HALF_PERIOD_US)) return false;
    return true;
  }
  bool send(uint8_t value, bool &ack) {
    if (error_ || expired()) return false;
    for (unsigned bit = 0; bit < 8; ++bit) {
      const bool one = value & 0x80;
      if (one) { io_.releaseSda(); if (!high(false)) return false; }
      else io_.lowSda();
      if (!wait(HALF_PERIOD_US)) return false;
      io_.releaseScl();
      if (!high(true) || !wait(HALF_PERIOD_US)) return false;
      if (one && !io_.sda()) return setError("unexpected_sda_low");
      io_.lowScl();
      value <<= 1;
    }
    io_.releaseSda();
    if (!wait(HALF_PERIOD_US)) return false;
    io_.releaseScl();
    if (!high(true) || !wait(HALF_PERIOD_US)) return false;
    ack = !io_.sda();
    io_.lowScl();
    // Allow the slave to release its ninth-bit ACK before changing data.
    return wait(5);
  }
  bool receive(uint8_t &value, bool ack) {
    if (error_ || expired()) return false;
    value = 0;
    io_.releaseSda();
    for (unsigned bit = 0; bit < 8; ++bit) {
      if (!wait(HALF_PERIOD_US)) return false;
      io_.releaseScl();
      if (!high(true) || !wait(HALF_PERIOD_US)) return false;
      value = uint8_t((value << 1) | (io_.sda() ? 1 : 0));
      io_.lowScl();
    }
    if (ack) io_.lowSda(); else io_.releaseSda();
    if (!wait(HALF_PERIOD_US)) return false;
    io_.releaseScl();
    if (!high(true) || !wait(HALF_PERIOD_US)) return false;
    io_.lowScl(); io_.releaseSda();
    return wait(5);
  }
  bool read(uint8_t address, uint8_t *data, size_t length) {
    if (!length || length > IMAGE_BYTES || length > IMAGE_BYTES - size_t(address))
      return setError("read_bounds_invalid");
    if (!start() || !sendAck(ADDRESS << 1) || !sendAck(address) || !start(true) ||
        !sendAck((ADDRESS << 1) | 1)) return false;
    for (size_t i = 0; i < length; ++i) {
      if (!receive(data[i], i + 1 < length)) return false;
      if ((i & 15) == 15) { io_.yield(); if (expired()) return false; }
    }
    return stop();
  }
  bool writePage(uint8_t address, const uint8_t *data, size_t length) {
    if (!pageBounded(address, length)) return setError("page_bounds_invalid");
    if (!start() || !sendAck(ADDRESS << 1) || !sendAck(address)) return false;
    for (size_t i = 0; i < length; ++i) if (!sendAck(data[i])) return false;
    return stop();
  }
  bool ackPoll() {
    phaseDeadline_ = io_.nowUs() + ACK_TIMEOUT_US;
    while (!expired()) {
      bool ack = false;
      if (!start() || !send(ADDRESS << 1, ack) || !stop()) return false;
      if (ack) { phaseDeadline_ = UINT64_MAX; return true; }
      if (!wait(100)) return false;
    }
    return setError("write_ack_timeout");
  }
 private:
  IO &io_;
  uint64_t startUs_;
  uint64_t phaseDeadline_ = UINT64_MAX;
  const char *error_ = nullptr;
  bool setError(const char *message) { if (!error_) error_ = message; return false; }
  bool failIf(bool failed, const char *message) { if (failed) setError(message); return failed; }
  bool wait(uint32_t us) {
    if (error_ || expired()) return false;
    io_.waitUs(us);
    return !expired();
  }
  bool high(bool clock) {
    const uint64_t riseStart = io_.nowUs();
    while (!(clock ? io_.scl() : io_.sda())) {
      if (error_ || expired()) return false;
      if (io_.nowUs() - riseStart >= RISE_TIMEOUT_US)
        return setError(clock ? "scl_rise_timeout" : "sda_rise_timeout");
      io_.waitUs(5);
    }
    return !expired();
  }
  bool sendAck(uint8_t value) {
    bool ack = false;
    if (!send(value, ack)) return false;
    return ack ? true : setError("device_nack");
  }
};
} // namespace bench_rules
