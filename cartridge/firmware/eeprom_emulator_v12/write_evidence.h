#pragma once
struct EmuWriteRecord {
  uint32_t valid=0,committed=0,faultCode=0,generationBefore=0,generationAfter=0;
  uint32_t pointer=0,pageBase=0,pageMask=0,dataBytes=0,rxBytes=0,rxAckBytes=0,rxBadSamples=0;
  uint32_t irqFlags=0,hwStatus=0,lastRxStatus=0;
  uint8_t page[16]={};
};
DRAM_ATTR static write_rules::RxState emuRxEvidence;
DRAM_ATTR static volatile EmuWriteRecord emuRejectedWrite;
DRAM_ATTR static volatile EmuWriteRecord emuWriteHistory[4];
DRAM_ATTR static uint32_t emuWriteNext=0,emuWriteCount=0;
static void IRAM_ATTR emuCaptureWrite(volatile EmuWriteRecord &record,uint32_t fault,uint32_t flags,uint32_t status) {
  record.valid=0; record.committed=0; record.faultCode=fault;
  record.generationBefore=record.generationAfter=emuDevice.generation;
  record.pointer=emuDevice.pointer; record.pageBase=emuDevice.pageBase; record.pageMask=emuDevice.pageMask;
  record.dataBytes=emuDevice.writtenBytes; record.rxBytes=emuDevice.rxBytes;
  record.rxAckBytes=emuRxEvidence.ackDelivered; record.rxBadSamples=emuRxEvidence.badSamples;
  record.irqFlags=flags; record.hwStatus=status; record.lastRxStatus=emuRxEvidence.lastStatus;
  for (unsigned i=0;i<16;++i) record.page[i]=(emuDevice.pageMask&(1U<<i))?emuDevice.page[i]:0;
}
static void IRAM_ATTR emuCaptureRejected(uint32_t fault) {
  if (!emuDevice.writeDataSeen) return;
  emuCaptureWrite(emuRejectedWrite,fault,emuMetrics.lastIrqFlags,emuMetrics.lastHwStatus);
  emuRejectedWrite.valid=1;
}
