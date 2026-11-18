#pragma once
#include "eeprom_model.h"
#include "bench_rules.h"

// Separate virtual view: the backing Device remains the exact accepted-write
// audit mirror. Refresh ONLY at explicit passive enable or actual read-direction
// pointer0. No FIFO wrap, post-write, post-verification or arbitrary-read refill.
namespace virtual_paper {
constexpr uint16_t MAX_CAPACITY=0x2be0;
struct PassiveState {
  uint32_t live=0,durable=0;
  bool loaded=false,active=false,session=false,marker=false,dirty=false,unclean=false;
  bool armed=false,update=false,recovery=false,inputs=true,pullsOff=true,pullupOff=true;
  bool matricesDetached=true,exactPreimagePin=false;
};
inline bool passive(const PassiveState &s,uint32_t gen) {
  return s.loaded && !s.active && !s.session && !s.marker && !s.dirty && !s.unclean &&
    !s.armed && !s.update && !s.recovery && s.inputs && s.pullsOff && s.pullupOff &&
    s.matricesDetached && s.exactPreimagePin && s.live==gen && s.durable==gen && gen!=UINT32_MAX;
}
inline bool EMU_IRAM shouldBeginRead(bool enabled,bool reading,bool busy,bool alreadyActive) {
  return enabled && reading && busy && !alreadyActive;
}
// A non-COMDAT free function keeps classic Xtensa IRAM literals before their
// uses. Inline class methods with this CRC loop fail the pinned linker l32r
// range/placement check; do not relax that check or move ISR work to flash.
static uint16_t EMU_IRAM __attribute__((noinline)) rw2Crc14(
    const volatile uint8_t *view,uint16_t capacity,bool virtualRemaining) {
  uint16_t crc=0xffff;
  for (unsigned i=0;i<14;++i) {
    const uint8_t byte=virtualRemaining && i<2 ? uint8_t(capacity>>(8*i)) : view[0xe8+i];
    crc^=byte;
    for (unsigned bit=0;bit<8;++bit) crc=uint16_t((crc>>1)^((crc&1)?0x8408:0));
  }
  return uint16_t(crc^0xffff);
}
struct Policy {
  volatile uint8_t view[256]={};
  volatile uint32_t sequence=0,viewGeneration=0;
  volatile bool enabled=false,suspended=false,readOpen=false,verificationPending=false;
  volatile bool cacheRefreshRequired=false,readbackCandidate=false,fullReadCandidate=false,readIsInitialization=false;
  volatile uint16_t capacity=0,r0PreparedCrc=0,rw2PreparedCrc=0,suppliedMask=0;
  volatile uint32_t backingGeneration=0,readGeneration=0,refreshEvents=0,initializationReads=0;
  volatile uint32_t blockedRefreshes=0,verificationReads=0,lastInitReadBytes=0;
  volatile uint8_t readStart=0;
  void EMU_IRAM beginChange() {
    __atomic_store_n(&sequence,sequence+1,__ATOMIC_SEQ_CST);
    __atomic_thread_fence(__ATOMIC_SEQ_CST);
  }
  void EMU_IRAM endChange() {
    __atomic_thread_fence(__ATOMIC_RELEASE);
    __atomic_store_n(&sequence,sequence+1,__ATOMIC_RELEASE);
  }
  uint16_t EMU_IRAM word(unsigned at) const {return uint16_t(view[at])|uint16_t(view[at+1])<<8;}
  bool enable(const PassiveState &s,uint32_t gen,const uint8_t *backing,uint16_t requested) {
    if (enabled || readOpen || !backing || !passive(s,gen) || !bench_rules::check(backing).valid() ||
        backing[1]==3 || !requested || requested>MAX_CAPACITY) return false;
    uint8_t prepared[174];
    for (unsigned i=0;i<174;++i) prepared[i]=backing[i];
    prepared[0x60]=uint8_t(requested);prepared[0x61]=uint8_t(requested>>8);
    r0PreparedCrc=bench_rules::crc16(prepared,sizeof(prepared));
    capacity=requested;backingGeneration=gen;viewGeneration=gen;
    suspended=false;verificationPending=false;suppliedMask=0;
    refreshEvents=initializationReads=blockedRefreshes=verificationReads=lastInitReadBytes=0;
    beginChange();for (unsigned i=0;i<256;++i) view[i]=backing[i];endChange();
    rw2PreparedCrc=rw2Crc14(view,capacity,true);enabled=true;
    return refresh();
  }
  bool disable(const PassiveState &s,uint32_t gen) {
    if (readOpen || !passive(s,gen) || (enabled && gen!=backingGeneration)) return false;
    enabled=false;suspended=false;verificationPending=false;suppliedMask=0;
    cacheRefreshRequired=readbackCandidate=fullReadCandidate=false;
    return true;
  }
  bool EMU_IRAM refresh() {
    if (!enabled || suspended || verificationPending || viewGeneration==UINT32_MAX ||
        refreshEvents==UINT32_MAX) return false;
    beginChange();
    view[0x60]=view[0xe8]=uint8_t(capacity);
    view[0x61]=view[0xe9]=uint8_t(capacity>>8);
    view[0xae]=uint8_t(r0PreparedCrc);view[0xaf]=uint8_t(r0PreparedCrc>>8);
    view[0xf6]=uint8_t(rw2PreparedCrc);view[0xf7]=uint8_t(rw2PreparedCrc>>8);
    ++viewGeneration;endChange();++refreshEvents;cacheRefreshRequired=true;
    return true;
  }
  // Observe an already-qualified backing commit, never determine ACK/STOP.
  // Copy exactly its supplied page bytes, including intermediate invalid CRCs.
  bool EMU_IRAM acceptedWrite(const volatile uint8_t *backing,uint32_t gen,uint8_t pageBase,uint16_t mask) {
    if (!enabled) return true;
    if (!backing || readOpen || backingGeneration==UINT32_MAX || gen!=backingGeneration+1 ||
        viewGeneration==UINT32_MAX || !mask || (pageBase&15)) {suspended=true;return false;}
    beginChange();
    for (unsigned i=0;i<16;++i) if (mask&(1U<<i)) view[pageBase|i]=backing[pageBase|i];
    ++viewGeneration;endChange();backingGeneration=gen;
    uint16_t rw2Mask=0;
    if (pageBase==0xe0 && !(mask&0x00ff)) rw2Mask=uint16_t(mask>>8);
    else if (pageBase==0xf0 && !(mask&0xff00)) rw2Mask=uint16_t(mask<<8);
    else {suspended=true;return false;}
    suppliedMask|=rw2Mask;verificationPending=true;readbackCandidate=false;
    rw2PreparedCrc=rw2Crc14(view,capacity,true); // bounded14bytes at writeSTOP, not pointer prime.
    if (word(0xe8)==0) cacheRefreshRequired=true;
    return true;
  }
  // First read-direction service sees <=32 prefetched prefix bytes for pointer0.
  // All8 patched offsets are >=0x60. Patch before subsequent FIFO fill: no flush,
  // 174-byte CRC or pointer-prime delay. An explicitly received pointer is used.
  bool EMU_IRAM beginRead(uint8_t pointer,uint32_t gen,bool explicitlyReceived=true) {
    if (!enabled) return true;
    if (readOpen || gen!=backingGeneration) {suspended=true;return false;}
    readOpen=true;readStart=pointer;readGeneration=gen;
    readbackCandidate=fullReadCandidate=false;
    readIsInitialization=pointer==0 && explicitlyReceived;
    if (readIsInitialization) {
      if (!verificationPending && !suspended) return refresh();
      if (blockedRefreshes!=UINT32_MAX) ++blockedRefreshes;
    }
    return true;
  }
  void EMU_IRAM readNack(uint32_t count) {
    if (!enabled || !readOpen) return;
    if (readIsInitialization) lastInitReadBytes=count;
    fullReadCandidate=readIsInitialization && count==256 && !verificationPending && !suspended;
    readbackCandidate=readStart==0xe8 && count==16 && verificationPending && suppliedMask==0xffff &&
      rw2Crc14(view,capacity,false)==word(0xf6) && readGeneration==backingGeneration;
  }
  void EMU_IRAM readStop() {
    if (!enabled || !readOpen) return;
    if (readbackCandidate) {
      verificationPending=false;suppliedMask=0;
      if (verificationReads!=UINT32_MAX) ++verificationReads;
    }
    if (fullReadCandidate) {
      if (initializationReads!=UINT32_MAX) ++initializationReads;
      cacheRefreshRequired=false;
    }
    readOpen=false;readbackCandidate=fullReadCandidate=false;
  }
  void EMU_IRAM abandonRead() {readOpen=false;readbackCandidate=fullReadCandidate=false;}
  uint8_t EMU_IRAM byte(uint8_t address,uint8_t backingByte) const {return enabled?view[address]:backingByte;}
  bool snapshot(uint8_t *copy,uint32_t &gen) const {
    for (unsigned attempt=0;attempt<8;++attempt) {
      const uint32_t before=__atomic_load_n(&sequence,__ATOMIC_ACQUIRE);
      if (before&1) continue;
      gen=__atomic_load_n(&viewGeneration,__ATOMIC_RELAXED);
      for (unsigned i=0;i<256;++i) copy[i]=view[i];
      __atomic_thread_fence(__ATOMIC_ACQUIRE);
      if (__atomic_load_n(&sequence,__ATOMIC_ACQUIRE)==before) return true;
    }
    return false;
  }
};
} // namespace virtual_paper
