// Template: reviewed V6/V7 native model/guard tests. No device, network or UART.
#include "../firmware/eeprom_emulator_v8/irq_rules.h"
#include <iostream>
#include <stdexcept>

static unsigned checks=0;
static void require(bool value,const char *message) {
  ++checks; if (!value) throw std::runtime_error(message);
}
int main() {
  try {
    irq_rules::Budget budget;
    budget.reset(1000);
    // Representative100kHz byte servicing plus boundary/refill interrupts.
    for (uint32_t i=0;i<16;++i) {
      require(budget.enter(1000+i*12000),"Normal sparse IRQ rejected");
      require(budget.finish(1000),"Normal ISR time rejected");
    }
    budget.reset(0);
    for (uint32_t i=0;i<64;++i) require(budget.enter(i*100),"Bounded IRQ entry rejected");
    require(!budget.enter(6400),"Persistent level IRQ did not hit entry bound");
    budget.reset(0);
    require(budget.enter(0) && budget.finish(120000),"Exact half-ms budget boundary rejected");
    require(!budget.finish(1),"ISR cycle overrun accepted");
    require(!budget.finish(UINT32_MAX),"Large elapsed overflow evaded bound");
    budget.reset(0);
    require(budget.enter(0) && budget.finish(119999),"Normal initial budget failed");
    require(budget.enter(240000) && budget.cycles==0 && budget.calls==1,"One-ms window did not reset");
    require(budget.finish(120000),"Fresh window inherited old budget");
    const uint32_t nearWrap=UINT32_MAX-1000;
    budget.reset(nearWrap);
    require(budget.enter(500) && budget.calls==1,"CCOUNT wrap misread as expired window");
    require(budget.enter(uint32_t(nearWrap+240000)) && budget.calls==1,"Wrapped one-ms window failed");
    std::cout<<"PASS: "<<checks<<" bounded IRQ entry/time/wrap assertions. Hardware IRQ source remains unqualified.\n";
  } catch (const std::exception &error) { std::cerr<<"FAIL: "<<error.what()<<'\n'; return 1; }
}
