#pragma once
#include <stdint.h>
#include "eeprom_model.h"
namespace irq_rules {
// Experimental100kHz qualification budget at the explicitly pinned240MHz CPU.
// Timer/entry bounds prevent a persistent level IRQ from starving UART/tasks.
struct Budget {
  uint32_t start=0, calls=0, cycles=0;
  void reset(uint32_t now) { start=now; calls=cycles=0; }
  bool EMU_IRAM enter(uint32_t now) {
    if (uint32_t(now-start)>=240000) { start=now; calls=cycles=0; }
    return ++calls<=64;
  }
  bool EMU_IRAM finish(uint32_t elapsed) {
    if (elapsed>120000-cycles) return false;
    cycles+=elapsed; return true;
  }
};
}
