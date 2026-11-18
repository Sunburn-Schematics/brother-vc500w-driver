#include "../firmware/isolated_bench_i2c/bench_rules.h"
#include <algorithm>
#include <array>
#include <cassert>
#include <cstdio>
#include <fstream>
#include <iterator>
#include <vector>

// Electrical open-drain model of a 256-byte 0x50 EEPROM. It observes START,
// STOP, bytes and ninth-bit ACKs. Writes commit only on STOP and NACK while busy.
struct FakeIo {
  enum State { ADDRESS, POINTER, WRITE, READ, MASTER_ACK, IGNORE } state = IGNORE, next = IGNORE;
  std::array<uint8_t,256> image{};
  uint64_t time = 0, busyUntil = 0;
  bool masterSdaLow = false, masterSclLow = false, slaveSdaLow = false;
  bool heldSclLow = false, heldSdaLow = false, nackData = false;
  unsigned rising = 0, starts = 0, stops = 0, writeStops = 0, dataBytes = 0;
  unsigned bits = 0, txBit = 0;
  uint8_t byte = 0, pointer = 0, tx = 0;
  bool ackClock = false, ackValue = false, ackSeen = false, masterAck = false;
  std::vector<std::pair<uint8_t,uint8_t>> pending;
  uint64_t nowUs() { return time; }
  bool scl() const { return !masterSclLow && !heldSclLow; }
  bool sda() const { return !masterSdaLow && !slaveSdaLow && !heldSdaLow; }
  void waitUs(uint32_t us) { time += us; }
  void yield() { time += 1000; }
  void lowSda() { changeSda(true); }
  void releaseSda() { changeSda(false); }
  void lowScl() { changeScl(true); }
  void releaseScl() { changeScl(false); }
  void changeSda(bool low) {
    const bool before = sda(); masterSdaLow = low; const bool after = sda();
    if (scl() && before != after) {
      if (!after) onStart(); else onStop();
    }
  }
  void changeScl(bool low) {
    const bool before = scl(); masterSclLow = low; const bool after = scl();
    if (before != after) { if (after) onRise(); else onFall(); }
  }
  void onStart() {
    ++starts; state = ADDRESS; bits = 0; byte = 0; ackClock = ackSeen = false;
    slaveSdaLow = false; pending.clear();
  }
  void onStop() {
    ++stops;
    if (!pending.empty()) {
      for (auto item : pending) image[item.first] = item.second;
      ++writeStops; busyUntil = time + 5000;
    }
    pending.clear(); state = IGNORE; slaveSdaLow = false;
  }
  void onRise() {
    ++rising;
    if (state == READ) return;
    if (state == MASTER_ACK) { masterAck = !sda(); return; }
    if (state == IGNORE) return;
    if (ackClock) { ackSeen = true; return; }
    byte = uint8_t((byte << 1) | (sda() ? 1 : 0));
    if (++bits != 8) return;
    ackValue = true;
    if (state == ADDRESS) {
      ackValue = (byte >> 1) == 0x50 && time >= busyUntil;
      next = ackValue ? ((byte & 1) ? READ : POINTER) : IGNORE;
    } else if (state == POINTER) { pointer = byte; next = WRITE; }
    else {
      ++dataBytes; ackValue = !nackData;
      if (ackValue) { pending.push_back({pointer,byte}); ++pointer; }
      next = ackValue ? WRITE : IGNORE;
    }
    ackClock = true; ackSeen = false;
  }
  void setupRead() { tx = image[pointer]; txBit = 0; slaveSdaLow = (tx & 0x80) == 0; }
  void onFall() {
    if (state == READ) {
      if (++txBit == 8) { slaveSdaLow = false; state = MASTER_ACK; }
      else slaveSdaLow = (tx & (0x80 >> txBit)) == 0;
      return;
    }
    if (state == MASTER_ACK) {
      ++pointer;
      if (masterAck) { state = READ; setupRead(); }
      else { state = IGNORE; slaveSdaLow = false; }
      return;
    }
    if (!ackClock) return;
    if (!ackSeen) { slaveSdaLow = ackValue; return; }
    slaveSdaLow = false; state = next; bits = 0; byte = 0; ackClock = ackSeen = false;
    if (state == READ) setupRead();
  }
};

static std::array<uint8_t,256> fixture(const char *path) {
  std::ifstream stream(path, std::ios::binary);
  std::vector<uint8_t> bytes{std::istreambuf_iterator<char>(stream), {}};
  assert(bytes.size() == 256);
  std::array<uint8_t,256> image{};
  std::copy(bytes.begin(),bytes.end(),image.begin()); return image;
}
static void fixCrcs(std::array<uint8_t,256> &image) {
  for (auto region : {std::array<size_t,3>{0,174,174}, {208,22,230}, {232,14,246}}) {
    const uint16_t crc = bench_rules::crc16(image.data()+region[0],region[1]);
    image[region[2]] = uint8_t(crc); image[region[2]+1] = uint8_t(crc>>8);
  }
}
int main(int argc, char **argv) {
  assert(argc == 2);
  using bench_rules::Master;
  const auto before = fixture(argv[1]);
  assert(bench_rules::crc16(reinterpret_cast<const uint8_t *>("123456789"),9) == 0x906e);
  assert(bench_rules::check(before.data()).valid());
  assert(bench_rules::u16le(before.data()+0x60) == 6299);
  assert(bench_rules::u16le(before.data()+0xe8) == 4);
  std::array<uint8_t,256> expected{};
  assert(!bench_rules::restoreImage(before.data(), expected.data()));
  const std::vector<size_t> allow{0xe8,0xe9,0xf6,0xf7};
  size_t differences = 0;
  for (size_t i=0;i<256;++i) if (before[i] != expected[i]) {
    assert(std::find(allow.begin(),allow.end(),i) != allow.end()); ++differences;
  }
  assert(differences == 4 && bench_rules::check(expected.data()).valid());
  assert(bench_rules::u16le(expected.data()+0xe8) == 6299);
  for (size_t address=0;address<256;++address) for (size_t length=0;length<=17;++length) {
    const bool valid = length>0 && length<=16 && address+length<=256 && address/16 == (address+length-1)/16;
    assert(bench_rules::pageBounded(uint16_t(address),length) == valid);
  }
  assert(!bench_rules::pageBounded(256,1));
  auto bad = before; bad[7] ^= 1;
  assert(bench_rules::restoreImage(bad.data(),expected.data()));
  bad=before; bad[1]=3; fixCrcs(bad);
  assert(bench_rules::restoreImage(bad.data(),expected.data()));
  bad=before; bad[0x60]=0; bad[0x61]=0; fixCrcs(bad);
  assert(bench_rules::restoreImage(bad.data(),expected.data()));
  bad=before; bad[0x60]=0xff; bad[0x61]=0xff; fixCrcs(bad);
  assert(bench_rules::restoreImage(bad.data(),expected.data()));
  bad=before; bad[0xe8]=0xff; bad[0xe9]=0xff; fixCrcs(bad);
  assert(bench_rules::restoreImage(bad.data(),expected.data()));
  assert(!bench_rules::restoreImage(before.data(),expected.data()));

  FakeIo io; io.image=before; Master<FakeIo> bus(io);
  std::array<uint8_t,256> read1{}, read2{}, post{};
  assert(bus.read(0,read1.data(),256)); assert(read1==before);
  assert(bus.read(0,read2.data(),256)); assert(read2==before);
  assert(io.writeStops==0);
  assert(bus.writePage(0xe8,expected.data()+0xe8,2));
  assert(bus.ackPoll());
  uint8_t counter[2]{}; assert(bus.read(0xe8,counter,2));
  assert(bench_rules::u16le(counter)==6299);
  assert(bus.writePage(0xf6,expected.data()+0xf6,2)); assert(bus.ackPoll());
  assert(bus.read(0,post.data(),256)); assert(post==expected);
  assert(io.writeStops==2 && io.dataBytes==4 && io.time<8000000);

  FakeIo stuckClock; stuckClock.heldSclLow=true; Master<FakeIo> clockBus(stuckClock);
  assert(!clockBus.read(0,read1.data(),256)); assert(stuckClock.rising==0);
  assert(stuckClock.stops==0 && stuckClock.time<=1100);
  const auto beforeRises=stuckClock.rising;
  assert(!clockBus.read(0,read1.data(),256)); assert(stuckClock.rising==beforeRises);
  FakeIo stuckData; stuckData.heldSdaLow=true; Master<FakeIo> dataBus(stuckData);
  assert(!dataBus.read(0,read1.data(),256)); assert(stuckData.rising==0 && stuckData.stops==0);
  FakeIo nacked; nacked.image=before; nacked.nackData=true; Master<FakeIo> nackBus(nacked);
  assert(!nackBus.writePage(0xe8,expected.data()+0xe8,2));
  assert(nacked.stops==0 && nacked.writeStops==0 && nacked.dataBytes==1);
  const unsigned clocksAfterFailure=nacked.rising;
  assert(!nackBus.ackPoll()); assert(nacked.rising==clocksAfterFailure);
  FakeIo busy; busy.busyUntil=1000000; Master<FakeIo> busyBus(busy);
  assert(!busyBus.ackPoll()); assert(busy.time<=10100 && busy.writeStops==0);
  const unsigned busyClocks=busy.rising;
  assert(!busyBus.read(0,read1.data(),256)); assert(busy.rising==busyClocks);
  FakeIo invalid; Master<FakeIo> boundsBus(invalid);
  assert(!boundsBus.writePage(0xef,expected.data(),2)); assert(invalid.starts==0 && invalid.rising==0);
  FakeIo total; Master<FakeIo> totalBus(total); total.time=8000000;
  assert(!totalBus.read(0,read1.data(),256)); assert(total.starts==0);
  std::puts("PASS: synthetic fixture CRC/allowlist, all page bounds, full EEPROM protocol model, ACK polling, sticky errors, stuck lines and deadlines");
}
