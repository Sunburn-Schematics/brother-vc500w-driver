#include "../firmware/eeprom_emulator_v10/v10_rules.h"
#include <cassert>
#include <cstdio>
#include <initializer_list>

int main() {
  using v10_rules::State;
  unsigned checked=0;
  const auto check=[&](bool condition) { assert(condition); ++checked; };
  const State recovered={15,1,1,false,false,false,true,false,false,false,false,true,true,true,true,true};
  check(v10_rules::recoverPointerFault(recovered,1));
  for (uint32_t fault: {0U,1U,10U,12U,13U,14U,16U,UINT32_MAX}) {
    State s=recovered; s.fault=fault; check(!v10_rules::recoverPointerFault(s,1));
  }
  for (uint32_t generation: {0U,2U,UINT32_MAX}) {
    check(!v10_rules::recoverPointerFault(recovered,generation));
    State s=recovered; s.live=s.durable=generation; check(!v10_rules::recoverPointerFault(s,generation));
  }
  for (bool State::* member: {&State::active,&State::session,&State::marker,&State::dirty,
      &State::armed,&State::update,&State::usbRecovery}) {
    State s=recovered; s.*member=true; check(!v10_rules::recoverPointerFault(s,1));
  }
  for (bool State::* member: {&State::loaded,&State::inputOnly,&State::pullsOff,
      &State::matricesDetached,&State::pullupOff,&State::exactValidSeed}) {
    State s=recovered; s.*member=false; check(!v10_rules::recoverPointerFault(s,1));
  }
  { State s=recovered; s.live=2; check(!v10_rules::recoverPointerFault(s,1)); }
  { State s=recovered; s.durable=2; check(!v10_rules::recoverPointerFault(s,1)); }
  State ready=recovered; ready.fault=0;
  check(v10_rules::enablePullup(ready,false));
  check(!v10_rules::enablePullup(ready,true));
  check(!v10_rules::enablePullup(recovered,false));
  for (bool State::* member: {&State::active,&State::session,&State::marker,&State::dirty,
      &State::armed,&State::update,&State::usbRecovery}) {
    State s=ready; s.*member=true; check(!v10_rules::enablePullup(s,false));
  }
  for (bool State::* member: {&State::loaded,&State::inputOnly,&State::pullsOff,
      &State::matricesDetached,&State::pullupOff,&State::exactValidSeed}) {
    State s=ready; s.*member=false; check(!v10_rules::enablePullup(s,false));
  }
  { State s=ready; s.live=2; check(!v10_rules::enablePullup(s,false)); }
  // Normal later checkpoints can enable the pullup without resetting generation.
  { State s=ready; s.live=s.durable=17; check(v10_rules::enablePullup(s,false)); }
  std::printf("PASS: %u V10 recovery/pullup guard assertions\n",checked);
}
