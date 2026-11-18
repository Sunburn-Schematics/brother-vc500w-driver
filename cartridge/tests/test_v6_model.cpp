// Independent native tests: pure model only, no GPIO, UART, network or flash.
// Template: test_v5_bench.cpp native fixture and bounded-input test style.
#include "../firmware/eeprom_emulator_v6/eeprom_model.h"
#include <algorithm>
#include <array>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>

using Image = std::array<uint8_t, 256>;
using eeprom_model::Device;
static unsigned assertions = 0, groups = 0;
static void require(bool condition, const char *message) {
  ++assertions;
  if (!condition) throw std::runtime_error(message);
}
static Image fileImage(const char *path) {
  std::ifstream input(path, std::ios::binary);
  if (!input) throw std::runtime_error("fixture open failed");
  std::array<char, 257> bytes{};
  input.read(bytes.data(), bytes.size());
  if (input.gcount() != 256) throw std::runtime_error("fixture must contain exactly 256 bytes");
  Image result{};
  std::copy_n(bytes.begin(), 256, result.begin());
  return result;
}
static Image copyImage(const Device &device) {
  Image result{};
  uint32_t generation = 0;
  require(device.snapshot(result.data(), generation), "coherent snapshot unavailable");
  require(generation == device.generation, "snapshot generation mismatch");
  return result;
}
static uint16_t crc(const Image &image, size_t start, size_t length) {
  uint16_t value = 0xffff;
  for (size_t i = start; i < start + length; ++i) {
    value ^= image[i];
    for (unsigned bit = 0; bit < 8; ++bit)
      value = (value & 1) ? uint16_t((value >> 1) ^ 0x8408) : uint16_t(value >> 1);
  }
  return value ^ 0xffff;
}
static uint16_t word(const Image &image, unsigned offset) {
  return uint16_t(image[offset] | uint16_t(image[offset + 1]) << 8);
}
static bool region(const Image &image, size_t start, size_t length, unsigned target) {
  return crc(image, start, length) == word(image, target);
}
static void writeChunk(Device &device, uint8_t address, const Image &source, unsigned length) {
  device.start();
  device.receive(address);
  for (unsigned i = 0; i < length; ++i) device.receive(source[unsigned(address) + i]);
  require(device.stop(true), "qualified complete write did not commit");
}

static void readSemantics(const Image &fixture) {
  ++groups;
  Device device;
  device.load(fixture.data(), 7);
  device.receive(0);
  device.start(); // repeated START into read: keep completed pointer, discard write latch.
  Image observed{};
  for (unsigned i = 0; i < 256; ++i) observed[i] = device.prefetched(i);
  require(observed == fixture, "full 00..FF read differs from synthetic fixture");
  require(device.pointer == 0, "FIFO prefetch advanced EEPROM current address");
  device.completedRead(256); // Includes the final transmitted byte that master NACKs.
  require(device.pointer == 0, "256 completed bytes did not wrap current address");
  require(!device.stop(true), "read STOP fabricated a write");
  require(device.generation == 7, "read altered image generation");
  require(copyImage(device) == fixture, "read mutated EEPROM bytes");

  device.receive(0xfe);
  device.start();
  require(device.prefetched(0) == fixture[0xfe] && device.prefetched(1) == fixture[0xff] &&
          device.prefetched(2) == fixture[0] && device.prefetched(3) == fixture[1],
          "sequential read did not wrap FF to 00");
  device.completedRead(1);
  require(device.pointer == 0xff, "final NACKed byte was not included in completed count");
  device.stop(true);
  require(device.pointer == 0xff, "STOP reset current address");
  device.start();
  require(device.prefetched(0) == fixture[0xff], "current-address read lost prior pointer");
  device.completedRead(2);
  require(device.pointer == 1, "current-address read progression failed");
}

static void stopAndRestart(const Image &fixture) {
  ++groups;
  Device device;
  device.load(fixture.data(), 100);
  device.receive(0xe8);
  require(!device.stop(true), "pointer-only STOP committed a page");
  require(device.pointer == 0xe8 && device.generation == 100, "pointer-only STOP changed state");
  device.receive(0xe8);
  device.receive(fixture[0xe8] ^ 0x80);
  device.receive(fixture[0xe9] ^ 0x40);
  require(!device.stop(false), "malformed midbyte STOP committed earlier completed data");
  require(copyImage(device) == fixture, "ineligible STOP changed bytes");
  require(device.generation == 100, "ineligible STOP changed generation");
  require(!device.stop(true), "later valid STOP resurrected discarded write latch");

  device.receive(0xe8);
  device.receive(fixture[0xe8] ^ 1);
  device.start(); // repeated START is not the write-cycle-committing STOP.
  require(!device.stop(true), "repeated START committed abandoned write data");
  require(copyImage(device) == fixture, "restart mutated abandoned write bytes");
  device.receive(0x32);
  device.start();
  require(device.prefetched(0) == fixture[0x32], "random read pointer lost on repeated START");
}

static void pageWrites(const Image &fixture) {
  ++groups;
  Device device;
  device.load(fixture.data(), 0);
  device.receive(0x2e);
  device.receive(0xa1);
  device.receive(0xb2);
  device.receive(0xc3);
  device.receive(0xd4);
  require(copyImage(device) == fixture, "write became visible before qualified STOP");
  require(device.stop(true), "page wrap write did not commit");
  Image expected = fixture;
  expected[0x2e] = 0xa1; expected[0x2f] = 0xb2;
  expected[0x20] = 0xc3; expected[0x21] = 0xd4;
  require(copyImage(device) == expected, "page write crossed 16-byte page boundary");
  require(device.pointer == 0x22, "selected next-byte pointer contract failed after page wrap");

  // More than one page of data: the last byte latched for each page position wins.
  device.start(); device.receive(0xf9);
  expected = copyImage(device);
  for (unsigned i = 0; i < 40; ++i) {
    device.receive(uint8_t(i + 1));
    expected[0xf0 | ((9 + i) & 15)] = uint8_t(i + 1);
  }
  require(device.stop(true), "long wrapped page write did not commit");
  require(copyImage(device) == expected, "long page write did not preserve last latch per slot");
  require(device.generation == 2, "generation did not advance once per committed page transaction");
}

static void normalPrinterWritePattern(const Image &restored, const Image &consumed) {
  ++groups;
  require(word(restored, 0x60) == 6299 && word(restored, 0xe8) == 6299,
          "restore fixture does not represent synthetic full-roll counter");
  require(word(consumed, 0x60) == 6299 && word(consumed, 0xe8) == 6256,
          "consumed fixture did not preserve synthetic 43-count consumption");
  Device device;
  device.load(restored.data(), 10);
  writeChunk(device, 0xe8, consumed, 8);
  Image intermediate = restored;
  std::copy(consumed.begin() + 0xe8, consumed.begin() + 0xf0, intermediate.begin() + 0xe8);
  require(copyImage(device) == intermediate, "first normal eight-byte write differs");
  require(device.pointer == 0xf0, "first eight-byte next pointer differs from selected contract");
  require(!region(intermediate, 0xe8, 14, 0xf6), "model silently repaired intermediate RW2 CRC");
  require(intermediate[0xf6] == restored[0xf6] && intermediate[0xf7] == restored[0xf7],
          "first write changed unaddressed CRC bytes");
  writeChunk(device, 0xf0, consumed, 8);
  require(copyImage(device) == consumed, "two normal eight-byte writes did not reproduce seed exactly");
  require(device.pointer == 0xf8 && device.generation == 12, "second write pointer/generation incorrect");
  require(region(consumed, 0, 174, 0xae) && region(consumed, 0xd0, 22, 0xe6) &&
          region(consumed, 0xe8, 14, 0xf6), "replayed consumed image CRC mismatch");
  device.receive(0xe8); device.start();
  for (unsigned i = 0; i < 16; ++i)
    require(device.prefetched(i) == consumed[0xe8 + i], "RW2 random readback differs");
  device.completedRead(16);
  require(device.pointer == 0xf8, "readback did not consume final NACKed byte");
  require(!device.stop(true), "readback STOP fabricated a write");
}

static void exhaustionAndSnapshot(const Image &fixture) {
  ++groups;
  Device device;
  device.load(fixture.data(), UINT32_MAX);
  device.receive(0x40); device.receive(fixture[0x40] ^ 0x80);
  require(!device.stop(true), "generation overflow was silently committed");
  require(copyImage(device) == fixture && device.generation == UINT32_MAX,
          "generation exhaustion modified image or wrapped counter");
  device.imageSequence = 1;
  Image output{}; uint32_t generation = 0;
  require(!device.snapshot(output.data(), generation), "snapshot accepted an odd writer sequence");
  device.imageSequence = 2;
  require(device.snapshot(output.data(), generation) && output == fixture && generation == UINT32_MAX,
          "even stable snapshot failed");
}

static void fifoAccounting() {
  ++groups;
  uint32_t count = 999;
  require(eeprom_model::txConsumed(32, 31, 1, count) && count == 1,
          "prefetched FIFO bytes were confused with transmitted bytes");
  require(eeprom_model::txConsumed(288, 32, 256, count) && count == 256,
          "full read final NACK accounting failed");
  require(!eeprom_model::txConsumed(32, 31, 0, count), "missing byte IRQ accepted");
  require(!eeprom_model::txConsumed(32, 31, 32, count), "prefetched bytes passed as completed");
  require(!eeprom_model::txConsumed(32, 32, 0, count), "zero completed bytes accepted");
  require(!eeprom_model::txConsumed(31, 32, 1, count), "FIFO accounting underflow accepted");
  require(!eeprom_model::txConsumed(64, 33, 31, count), "out-of-range FIFO remainder accepted");
}

int main(int argc, char **argv) {
  try {
    if (argc != 3) throw std::runtime_error("usage: test_v6_model VERIFIED_RESTORE.bin ASSEMBLED_CONSUMED.bin");
    const Image restored = fileImage(argv[1]), consumed = fileImage(argv[2]);
    readSemantics(consumed);
    stopAndRestart(consumed);
    pageWrites(consumed);
    normalPrinterWritePattern(restored, consumed);
    exhaustionAndSnapshot(consumed);
    fifoAccounting();
    std::cout << "PASS: " << groups << " native groups, " << assertions
              << " assertions. Pure-model replay only; slave peripheral timing remains unqualified.\n";
  } catch (const std::exception &error) {
    std::cerr << "FAIL: " << error.what() << '\n';
    return EXIT_FAILURE;
  }
}
