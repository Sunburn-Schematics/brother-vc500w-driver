// Template: V7 exact-recovery guards; offline only, no device/network/UART.
#include "../firmware/eeprom_emulator_v9/adoption_rules.h"
#include <iostream>
#include <stdexcept>
static unsigned checks=0;
static void require(bool value,const char *message) { ++checks; if (!value) throw std::runtime_error(message); }
int main() {
  try {
    const adoption_rules::State actual={0,1,1,false,false,true,true,true,false,false,false,false,true,true,true,true};
    require(adoption_rules::eligible(actual,1),"Recorded last-durable marked state rejected");
    require(!adoption_rules::eligible(actual,0),"Unpinned generation accepted");
    auto value=actual;
    for (uint32_t fault: {1U,2U,7U,10U,11U,12U,13U,14U,15U,UINT32_MAX}) {
      value=actual; value.fault=fault; require(!adoption_rules::eligible(value,1),"Faulted state accepted");
    }
    value=actual; value.live=2; require(!adoption_rules::eligible(value,1),"Volatile generation advance accepted");
    value=actual; value.durable=2; require(!adoption_rules::eligible(value,1),"Different checkpoint accepted");
    bool adoption_rules::State::* forbidden[]={&adoption_rules::State::active,&adoption_rules::State::session,
      &adoption_rules::State::dirty,&adoption_rules::State::armed,&adoption_rules::State::update,&adoption_rules::State::usbRecovery};
    for (auto member:forbidden) { value=actual; value.*member=true; require(!adoption_rules::eligible(value,1),"Active/mutating state accepted"); }
    bool adoption_rules::State::* needed[]={&adoption_rules::State::marker,&adoption_rules::State::unclean,
      &adoption_rules::State::loaded,&adoption_rules::State::inputs,&adoption_rules::State::pullsOff,
      &adoption_rules::State::matrixDetached,&adoption_rules::State::exactValidSeed};
    for (auto member:needed) { value=actual; value.*member=false; require(!adoption_rules::eligible(value,1),"Missing provenance/pin/image qualification accepted"); }
    require(actual.live==1 && actual.durable==1 && actual.marker && actual.unclean,"Guard altered preserved state");
    std::cout<<"PASS: "<<checks<<" explicit last-durable adoption guard assertions. Unknown pre-reset RAM remains unknown.\n";
  } catch (const std::exception &error) { std::cerr<<"FAIL: "<<error.what()<<'\n'; return 1; }
}
