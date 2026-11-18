#pragma once
#include <stddef.h>
#include <stdint.h>
#ifdef ARDUINO
#include <esp_attr.h>
#define EMU_IRAM IRAM_ATTR
#else
#define EMU_IRAM
#endif

// Pure EEPROM semantics; no peripheral, flash, allocation, hashes or CRC repair.
// Hardware adapter supplies only completed bytes and qualified STOP eligibility.
namespace eeprom_model {
struct Device {
  volatile uint8_t image[256] = {};
  uint8_t page[16] = {};
  uint8_t pointer = 0, pageBase = 0, writeOffset = 0, lastAddress = 0;
  uint16_t pageMask = 0;
  uint32_t rxBytes = 0, writtenBytes = 0;
  volatile uint32_t generation = 0, imageSequence = 0;
  bool expectsPointer = true, writeDataSeen = false;

  void load(const uint8_t *source, uint32_t gen) {
    for (size_t i=0;i<256;++i) image[i]=source[i];
    generation=gen; imageSequence=0; pointer=0; start();
  }
  void EMU_IRAM start() {
    pageMask=0; expectsPointer=true; writeDataSeen=false; rxBytes=writtenBytes=0;
  }
  void EMU_IRAM receive(uint8_t byte) {
    ++rxBytes;
    if (expectsPointer) {
      pointer=byte; pageBase=byte&0xf0; writeOffset=byte&15;
      expectsPointer=false; return;
    }
    page[writeOffset]=byte; pageMask|=uint16_t(1U<<writeOffset);
    lastAddress=uint8_t(pageBase|writeOffset); writeOffset=(writeOffset+1)&15;
    writeDataSeen=true; ++writtenBytes;
  }
  bool EMU_IRAM stop(bool eligible) {
    bool committed=false;
    if (writeDataSeen && eligible && generation != UINT32_MAX) {
      const uint32_t sequence=imageSequence;
      __atomic_store_n(&imageSequence,sequence+1,__ATOMIC_SEQ_CST);
      __atomic_thread_fence(__ATOMIC_SEQ_CST);
      for (unsigned i=0;i<16;++i) if (pageMask&(1U<<i)) image[pageBase|i]=page[i];
      __atomic_store_n(&generation,generation+1,__ATOMIC_RELAXED);
      __atomic_thread_fence(__ATOMIC_RELEASE);
      __atomic_store_n(&imageSequence,sequence+2,__ATOMIC_RELEASE);
      pointer=uint8_t(lastAddress+1); committed=true;
    }
    start(); return committed;
  }
  uint8_t EMU_IRAM prefetched(uint32_t offset) const { return image[uint8_t(pointer+offset)]; }
  void EMU_IRAM completedRead(uint32_t count) { pointer=uint8_t(pointer+count); }
  bool snapshot(uint8_t *copy, uint32_t &gen) const {
    for (unsigned attempt=0;attempt<8;++attempt) {
      const uint32_t before=__atomic_load_n(&imageSequence,__ATOMIC_ACQUIRE);
      if (before&1) continue;
      gen=__atomic_load_n(&generation,__ATOMIC_RELAXED);
      for (unsigned i=0;i<256;++i) copy[i]=image[i];
      __atomic_thread_fence(__ATOMIC_ACQUIRE);
      if (__atomic_load_n(&imageSequence,__ATOMIC_ACQUIRE)==before) return true;
    }
    return false;
  }
};

inline bool EMU_IRAM txConsumed(uint32_t loaded, uint32_t fifoRemaining, uint32_t byteIrqs, uint32_t &count) {
  if (fifoRemaining>32 || loaded<fifoRemaining) return false;
  count=loaded-fifoRemaining;
  return count>0 && count==byteIrqs;
}
} // namespace eeprom_model
