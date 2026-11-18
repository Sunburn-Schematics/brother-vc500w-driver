#include "../firmware/eeprom_emulator_v11/read_accounting.h"
#include "../firmware/eeprom_emulator_v11/read_recovery_rules.h"
#include <cassert>
#include <cstdio>
#include <initializer_list>

int main() {
  unsigned checks=0;
  const auto check=[&](bool ok) { assert(ok); ++checks; };
  uint32_t count=99;
  check(read_accounting::consumedAtNack(288,32,true,count) && count==256); // Actual V10 read.
  check(!read_accounting::consumedAtNack(288,32,false,count) && count==0); // STOP/abort is not NACK.
  for (uint32_t loaded: {0U,1U,16U,31U,32U,33U,48U,63U,64U,287U,288U,UINT32_MAX}) {
    for (uint32_t remaining: {0U,1U,15U,16U,31U,32U,33U,UINT32_MAX}) {
      const bool eligible=remaining<=32 && loaded>remaining;
      count=99; const bool ok=read_accounting::consumedAtNack(loaded,remaining,true,count);
      check(ok==eligible && count==(eligible?loaded-remaining:0));
      count=99; check(!read_accounting::consumedAtNack(loaded,remaining,false,count) && count==0);
    }
  }
  // Offset advances from consumed bytes, never from speculative prefill.
  eeprom_model::Device device;
  uint8_t image[256]={}; device.load(image,1); device.receive(0);
  check(read_accounting::consumedAtNack(288,32,true,count)); device.completedRead(count);
  check(device.pointer==0 && device.generation==1);
  using v10_rules::State;
  const State ready={2,1,1,false,false,false,true,false,false,false,false,true,true,true,true,true};
  check(read_recovery_rules::eligible(ready,1));
  for (uint32_t fault: {0U,1U,10U,12U,13U,14U,15U,UINT32_MAX}) {
    State s=ready; s.fault=fault; check(!read_recovery_rules::eligible(s,1));
  }
  for (uint32_t gen: {0U,2U,UINT32_MAX}) {
    check(!read_recovery_rules::eligible(ready,gen));
    State s=ready; s.live=s.durable=gen; check(!read_recovery_rules::eligible(s,gen));
  }
  for (bool State::* member: {&State::active,&State::session,&State::marker,&State::dirty,
      &State::armed,&State::update,&State::usbRecovery}) {
    State s=ready; s.*member=true; check(!read_recovery_rules::eligible(s,1));
  }
  for (bool State::* member: {&State::loaded,&State::inputOnly,&State::pullsOff,
      &State::matricesDetached,&State::pullupOff,&State::exactValidSeed}) {
    State s=ready; s.*member=false; check(!read_recovery_rules::eligible(s,1));
  }
  { State s=ready; s.live=2; check(!read_recovery_rules::eligible(s,1)); }
  { State s=ready; s.durable=2; check(!read_recovery_rules::eligible(s,1)); }
  std::printf("PASS: %u V11 FIFO-accounting and narrow-recovery assertions\n",checks);
}
