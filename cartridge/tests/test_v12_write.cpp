#include "../firmware/eeprom_emulator_v12/read_accounting.h"
#include "../firmware/eeprom_emulator_v12/read_recovery_rules.h"
#include "../firmware/eeprom_emulator_v12/write_rules.h"
#include "../firmware/eeprom_emulator_v12/write_recovery_rules.h"
#define DRAM_ATTR
#define IRAM_ATTR
static eeprom_model::Device emuDevice;
struct MetricsStub { uint32_t lastIrqFlags=0,lastHwStatus=0; };
static MetricsStub emuMetrics;
#include "../firmware/eeprom_emulator_v12/write_evidence.h"
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
  {
    write_rules::RxState actual;
    for (unsigned i=0;i<9;++i) actual.observe(1,false,5,i?0x05800110:0x05800130);
    check(write_rules::eligible(actual,9,8,1,true,false,false,0));
    check(!write_rules::eligible(actual,9,8,1,false,false,false,0));
    check(!write_rules::eligible(actual,9,8,1,true,true,false,0));
    check(!write_rules::eligible(actual,9,8,1,true,false,true,0));
    check(!write_rules::eligible(actual,9,8,1,true,false,false,1));
    check(!write_rules::eligible(actual,9,8,UINT32_MAX,true,false,false,0));
    check(!write_rules::eligible(actual,8,8,1,true,false,false,0));
    check(!write_rules::eligible(actual,9,0,1,true,false,false,0));
    check(!write_rules::eligible(actual,9,UINT32_MAX,1,true,false,false,0));
    auto missing=actual; --missing.delivered;
    check(!write_rules::eligible(missing,9,8,1,true,false,false,0));
    auto missingAck=actual; --missingAck.ackDelivered;
    check(!write_rules::eligible(missingAck,9,8,1,true,false,false,0));
    for (uint32_t state: {0U,1U,2U,3U,4U,6U,7U}) {
      write_rules::RxState bad; bad.observe(9,false,state,0x03800000);
      check(!write_rules::eligible(bad,9,8,1,true,false,false,0));
    }
    write_rules::RxState wrongDirection; wrongDirection.observe(9,true,5,0);
    check(!write_rules::eligible(wrongDirection,9,8,1,true,false,false,0));
    write_rules::RxState overflow; overflow.delivered=UINT32_MAX; overflow.observe(1,false,5,0);
    check(overflow.badSamples==UINT32_MAX);
    write_rules::RxState tooMany; tooMany.observe(33,false,5,0);
    check(tooMany.badSamples==UINT32_MAX);
    const auto before=actual; actual.observe(0,true,3,123);
    check(actual.delivered==before.delivered && actual.badSamples==before.badSamples && actual.lastStatus==before.lastStatus);
    actual.reset(); check(actual.delivered==0 && actual.ackDelivered==0 && actual.badSamples==0 && actual.lastStatus==0);
  }
  {
    uint8_t blank[256]={}; emuDevice.load(blank,1); emuRxEvidence.reset();
    emuDevice.receive(0xe8); emuRxEvidence.observe(1,false,5,0x05800130);
    for (unsigned i=0;i<8;++i) { emuDevice.receive(uint8_t(0xa0+i)); emuRxEvidence.observe(1,false,5,0x05800110); }
    emuMetrics.lastIrqFlags=128; emuMetrics.lastHwStatus=0x03800000;
    emuCaptureRejected(7);
    check(emuRejectedWrite.valid==1 && emuRejectedWrite.committed==0 && emuRejectedWrite.faultCode==7);
    check(emuRejectedWrite.pointer==0xe8 && emuRejectedWrite.pageBase==0xe0 && emuRejectedWrite.pageMask==0xff00);
    check(emuRejectedWrite.dataBytes==8 && emuRejectedWrite.rxBytes==9 && emuRejectedWrite.rxAckBytes==9);
    check(emuRejectedWrite.generationBefore==1 && emuRejectedWrite.generationAfter==1);
    check(emuRejectedWrite.irqFlags==128 && emuRejectedWrite.hwStatus==0x03800000);
    for (unsigned i=0;i<16;++i) check(emuRejectedWrite.page[i]==(i<8?0:uint8_t(0xa0+i-8)));
    emuDevice.start(); check(!emuDevice.writeDataSeen && emuRejectedWrite.valid==1 && emuRejectedWrite.page[8]==0xa0);
    for (unsigned i=0;i<256;++i) check(emuDevice.image[i]==0);
    emuDevice.receive(0xe8);
    for (unsigned i=0;i<8;++i) emuDevice.receive(uint8_t(0xa0+i));
    emuCaptureWrite(emuWriteHistory[0],0,128,0x03800000);
    check(emuWriteHistory[0].valid==0 && emuWriteHistory[0].page[8]==0xa0);
    check(emuDevice.stop(true) && emuDevice.generation==2);
    check(emuWriteHistory[0].pageMask==0xff00 && emuWriteHistory[0].dataBytes==8);
    for (unsigned i=0;i<8;++i) check(emuDevice.image[0xe8+i]==0xa0+i);
    check(emuWriteCount==0 && emuWriteNext==0); // Model/capture helper alone never publishes or invents a commit.
  }
  {
    const State writeReady={7,1,1,false,false,false,true,false,false,false,false,true,true,true,true,true};
    check(write_recovery_rules::eligible(writeReady,1));
    for (uint32_t fault: {0U,1U,2U,10U,12U,13U,14U,15U,UINT32_MAX}) {
      State s=writeReady; s.fault=fault; check(!write_recovery_rules::eligible(s,1));
    }
    for (bool State::* member: {&State::active,&State::session,&State::marker,&State::dirty,&State::armed,&State::update,&State::usbRecovery}) {
      State s=writeReady; s.*member=true; check(!write_recovery_rules::eligible(s,1));
    }
    for (bool State::* member: {&State::loaded,&State::inputOnly,&State::pullsOff,&State::matricesDetached,&State::pullupOff,&State::exactValidSeed}) {
      State s=writeReady; s.*member=false; check(!write_recovery_rules::eligible(s,1));
    }
    for (uint32_t gen: {0U,2U,UINT32_MAX}) check(!write_recovery_rules::eligible(writeReady,gen));
  }
  std::printf("PASS: %u V12 accounting, write qualification/evidence and recovery assertions\n",checks);
}
