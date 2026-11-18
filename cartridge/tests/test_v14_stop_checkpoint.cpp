#include "../firmware/eeprom_emulator_v14/stop_checkpoint_rules.h"
#include <cassert>
#include <cstdio>
#include <initializer_list>
int main() {
  unsigned n=0;const auto check=[&](bool value){assert(value);++n;};
  using virtual_paper::PassiveState;
  PassiveState ready;ready.live=ready.durable=63;ready.loaded=ready.exactPreimagePin=true;
  check(stop_checkpoint_rules::eligible(ready,9,false,true,63));
  ready.unclean=true;check(stop_checkpoint_rules::eligible(ready,9,false,true,63));
  for (uint32_t fault:{0U,1U,2U,7U,8U,10U,12U,13U,14U,15U,16U,UINT32_MAX})
    check(!stop_checkpoint_rules::eligible(ready,fault,false,true,63));
  check(!stop_checkpoint_rules::eligible(ready,9,true,true,63));
  check(!stop_checkpoint_rules::eligible(ready,9,false,false,63));
  for (uint32_t gen:{0U,1U,43U,62U,64U,UINT32_MAX})
    check(!stop_checkpoint_rules::eligible(ready,9,false,true,gen));
  for (bool PassiveState::* member:{&PassiveState::active,&PassiveState::session,&PassiveState::marker,
      &PassiveState::dirty,&PassiveState::armed,&PassiveState::update,&PassiveState::recovery}) {
    auto s=ready;s.*member=true;check(!stop_checkpoint_rules::eligible(s,9,false,true,63));
  }
  for (bool PassiveState::* member:{&PassiveState::loaded,&PassiveState::inputs,&PassiveState::pullsOff,
      &PassiveState::pullupOff,&PassiveState::matricesDetached,&PassiveState::exactPreimagePin}) {
    auto s=ready;s.*member=false;check(!stop_checkpoint_rules::eligible(s,9,false,true,63));
  }
  {auto s=ready;s.live=64;check(!stop_checkpoint_rules::eligible(s,9,false,true,63));}
  {auto s=ready;s.durable=62;check(!stop_checkpoint_rules::eligible(s,9,false,true,63));}
  // Actual idle post-STOP TXEMPTY: stale read direction cannot start a view read.
  check(!virtual_paper::shouldBeginRead(true,true,false,false));
  check(virtual_paper::shouldBeginRead(true,true,true,false));
  check(!virtual_paper::shouldBeginRead(false,true,true,false));
  check(!virtual_paper::shouldBeginRead(true,false,true,false));
  check(!virtual_paper::shouldBeginRead(true,true,true,true));
  std::printf("PASS: %u exact fault9/passive/device-read gating assertions; no image mutation\n",n);
}
