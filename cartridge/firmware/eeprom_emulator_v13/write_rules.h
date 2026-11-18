#pragma once
#include "eeprom_model.h"
namespace write_rules {
struct RxState {
  uint32_t delivered=0,ackDelivered=0,badSamples=0,lastStatus=0;
  void EMU_IRAM reset() { delivered=ackDelivered=badSamples=lastStatus=0; }
  void EMU_IRAM observe(uint32_t count,bool reading,uint32_t lastState,uint32_t status) {
    if (!count) return;
    lastStatus=status;
    if (count>32 || delivered>UINT32_MAX-count || ackDelivered>UINT32_MAX-count) {
      badSamples=UINT32_MAX; return;
    }
    delivered+=count;
    if (!reading && lastState==5) ackDelivered+=count;
    else if (badSamples!=UINT32_MAX) ++badSamples;
  }
};
// Qualified normal printer path: FIFO deliveries observed in SEND_ACK5 followed
// by a separately detected write-direction, empty-FIFO, idle-bus STOP. Bit4 IRQ
// count and FSM lastState at STOP are diagnostics, not byte/ACK authorities.
// Generic malformed mid-byte STOP cannot be distinguished by these observations.
inline bool EMU_IRAM eligible(const RxState &s,uint32_t rxBytes,uint32_t dataBytes,
    uint32_t generation,bool stop,bool busy,bool reading,uint32_t rxRemaining) {
  return stop && !busy && !reading && rxRemaining==0 && generation!=UINT32_MAX &&
    dataBytes>0 && dataBytes!=UINT32_MAX && rxBytes==dataBytes+1 &&
    s.delivered==rxBytes && s.ackDelivered==rxBytes && s.badSamples==0;
}
}
