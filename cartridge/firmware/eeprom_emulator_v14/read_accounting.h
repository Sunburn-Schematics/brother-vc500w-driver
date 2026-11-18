#pragma once
#include "eeprom_model.h"
namespace read_accounting {
// Adapter evidence, not a generic shifter/short-abort compatibility claim.
// Actual V10 full startup: current FIFO loaded288 - remaining32 =256 bytes,
// while only4 total SLAVE_TRAN_COMP flags were observed. That latched flag is
// diagnostic, not a reliable completed-byte counter on this measured path.
inline bool EMU_IRAM consumedAtNack(uint32_t loaded,uint32_t remaining,bool terminalNack,uint32_t &count) {
  count=0;
  if (!terminalNack || remaining>32 || loaded<remaining) return false;
  const uint32_t dequeued=loaded-remaining;
  if (!dequeued) return false;
  count=dequeued;
  return true;
}
}
