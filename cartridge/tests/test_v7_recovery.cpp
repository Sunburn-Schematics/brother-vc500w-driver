// Template: V6 native model tests. Offline recovery guard checks; no device I/O.
#include "../firmware/eeprom_emulator_v7/recovery_rules.h"
#include <iostream>
#include <stdexcept>

static unsigned checks=0;
static void require(bool condition,const char *message) {
  ++checks;
  if (!condition) throw std::runtime_error(message);
}
int main() {
  try {
    const emu_recovery::State actual={10,1,1,false,false,false,true,false,false,false,false,true,true,true};
    require(emu_recovery::eligible(actual,1),"Actual closed V6 attach-failure snapshot is ineligible");
    require(!emu_recovery::eligible(actual,0),"Unpinned generation accepted");
    auto value=actual;
    for (uint32_t fault: {0U,1U,2U,7U,9U,11U,12U,13U,UINT32_MAX}) {
      value=actual; value.fault=fault;
      require(!emu_recovery::eligible(value,1),"Nonlegacy or healthy fault accepted");
    }
    value=actual; value.live=2; require(!emu_recovery::eligible(value,1),"Changed RAM generation accepted");
    value=actual; value.durable=2; require(!emu_recovery::eligible(value,1),"Changed durable generation accepted");
    bool emu_recovery::State::* forbidden[]={&emu_recovery::State::active,&emu_recovery::State::session,
      &emu_recovery::State::marker,&emu_recovery::State::dirty,&emu_recovery::State::armed,
      &emu_recovery::State::update,&emu_recovery::State::usbRecovery};
    for (auto member:forbidden) {
      value=actual; value.*member=true;
      require(!emu_recovery::eligible(value,1),"Active/mutating state accepted");
    }
    bool emu_recovery::State::* required[]={&emu_recovery::State::loaded,&emu_recovery::State::inputOnly,
      &emu_recovery::State::pullsOff,&emu_recovery::State::exactValidSeed};
    for (auto member:required) {
      value=actual; value.*member=false;
      require(!emu_recovery::eligible(value,1),"Missing image/pin/hash/CRC qualification accepted");
    }
    require(actual.live==1 && actual.durable==1 && actual.fault==10,
      "Eligibility check mutated preserved evidence");
    std::cout<<"PASS: "<<checks<<" exact legacy-attach recovery guard assertions; no bytes/generation mutation.\n";
  } catch (const std::exception &error) {
    std::cerr<<"FAIL: "<<error.what()<<'\n'; return 1;
  }
}
