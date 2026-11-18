#pragma once
#include "eeprom_model.h"
#include "irq_rules.h"
#include "read_accounting.h"
#include "write_rules.h"
#include <driver/periph_ctrl.h>
#include <soc/i2c_struct.h>
#include <soc/i2c_reg.h>
#include <soc/gpio_sig_map.h>
#include <rom/gpio.h>
#include <esp_system.h>

// Diagnostic breadcrumbs only, never a source of EEPROM or recovery authority.
// No normal per-byte RTC writes: record task stages and already-detached faults.
RTC_NOINIT_ATTR static volatile uint32_t emuRtcMagic, emuRtcStage, emuRtcInverse;
static uint32_t emuResetReason=0, emuBootStage=0, emuBootInverse=0;
static bool emuBootBreadcrumbValid=false;
static void IRAM_ATTR emuRecordStage(uint32_t stage) {
  emuRtcMagic=0x53424538;
  emuRtcInverse=~stage;
  __asm__ __volatile__("memw" ::: "memory");
  emuRtcStage=stage;
  __asm__ __volatile__("memw" ::: "memory");
}
static void emuInitBreadcrumbs() {
  emuResetReason=uint32_t(esp_reset_reason());
  emuBootStage=emuRtcStage; emuBootInverse=emuRtcInverse;
  emuBootBreadcrumbValid=emuRtcMagic==0x53424538 && emuBootInverse==~emuBootStage && emuBootStage<=0xffff;
}

// Experimental classic-ESP32 adapter. Completed-byte semantics are cross-checked
// against the hardware FIFO; this is not a claim of measured bus qualification.
DRAM_ATTR static eeprom_model::Device emuDevice;
DRAM_ATTR static volatile bool emulationActive = false;
static bool emulationSessionOpen = false;
static bool emuLoaded = false, emuUncleanBoot = false, emuSessionMarker = false;
static uint32_t emuDurableGeneration = 0, emuSnapshotSequence = 0;
static char emuSeedHash[65] = {}, emuIdleHash[65] = {};
static intr_handle_t emuInterrupt = nullptr;
DRAM_ATTR static i2c_dev_t *emuHw = &I2C0;
DRAM_ATTR static volatile uint32_t emuFaultCode = 0;
struct EmuMetrics {
  uint32_t rxBytes=0, txCompleted=0, txPrefetched=0, byteIrqs=0, starts=0, stops=0,
    nacks=0, rxOverflows=0, txUnderflows=0, fifoErrors=0, maxIsrCycles=0,
    lastHwStatus=0, invalidStops=0, isrCalls=0, lastIrqFlags=0, lastRxCount=0,
    lastTxCount=0, irqStorms=0, emptyRxIrqs=0,
    readLoaded=0, readRemaining=0, readConsumed=0, readTxIrqs=0, readRxIrqs=0, readTerminalNack=0;
};
DRAM_ATTR static volatile EmuMetrics emuMetrics;
DRAM_ATTR static uint32_t emuTxLoaded=0, emuTxByteIrqs=0, emuRxByteIrqs=0;
DRAM_ATTR static bool emuReadFinished=false;
DRAM_ATTR static irq_rules::Budget emuIrqBudget;
struct EmuIrqSample { uint32_t flags, status, rx, tx; };
static constexpr uint32_t EMU_HISTORY_CAPACITY=64;
DRAM_ATTR static EmuIrqSample emuIrqHistory[EMU_HISTORY_CAPACITY];
DRAM_ATTR static uint32_t emuHistoryNext=0, emuHistoryCount=0;

#include "write_evidence.h"

// Task-only attachment diagnostics. No logging, strings or flash access in ISR.
struct EmuAttachDiagnostics {
  const char *stage="not_attempted";
  uint32_t core=0, levels=0, outputEnable=0, sdaMux=0, sclMux=0;
  bool inputOnly=true, pullsOff=true;
  esp_err_t irqError=ESP_OK, gpioError=ESP_OK;
};
static EmuAttachDiagnostics emuAttachDiagnostics;
static bool emuAttachPreflight() {
  emuAttachDiagnostics.stage="preflight";
  emuAttachDiagnostics.core=xPortGetCoreID();
  emuAttachDiagnostics.levels=busLevels();
  emuAttachDiagnostics.outputEnable=REG_READ(GPIO_ENABLE_REG);
  emuAttachDiagnostics.sdaMux=REG_READ(IO_MUX_GPIO21_REG);
  emuAttachDiagnostics.sclMux=REG_READ(IO_MUX_GPIO22_REG);
  emuAttachDiagnostics.inputOnly=(emuAttachDiagnostics.outputEnable&BUS_MASK)==0;
  emuAttachDiagnostics.pullsOff=((emuAttachDiagnostics.sdaMux|emuAttachDiagnostics.sclMux)&(FUN_PU|FUN_PD))==0;
  emuAttachDiagnostics.irqError=emuAttachDiagnostics.gpioError=ESP_OK;
  if (emuAttachDiagnostics.core!=1) { emuAttachDiagnostics.stage="core_not_1"; return false; }
  if (!emuAttachDiagnostics.inputOnly) { emuAttachDiagnostics.stage="pins_not_input"; return false; }
  if (!emuAttachDiagnostics.pullsOff) { emuAttachDiagnostics.stage="pulls_enabled"; return false; }
  if (emuAttachDiagnostics.levels!=3) { emuAttachDiagnostics.stage="bus_not_idle_high"; return false; }
  emuAttachDiagnostics.stage="preflight_passed";
  return true;
}

static void IRAM_ATTR emuDetachFast() {
  emuHw->int_ena.val=0;
  // Route SDA back to GPIO-controlled output-enable, then disable both outputs.
  // SCL is never routed to a peripheral output or driven by this adapter.
  REG_WRITE(GPIO_FUNC21_OUT_SEL_CFG_REG, SIG_GPIO_OUT_IDX | GPIO_FUNC21_OEN_SEL);
  REG_WRITE(GPIO_ENABLE_W1TC_REG, BUS_MASK);
  REG_WRITE(GPIO_OUT_W1TS_REG, BUS_MASK);
  emulationActive=false;
}
static void IRAM_ATTR emuFault(uint32_t code) {
  if (!emuFaultCode) emuFaultCode=code;
  emuCaptureRejected(code); // Preserve supplied payload before discarding the latch.
  emuDevice.start(); // Discard an uncommitted page latch; never invent STOP.
  emuDetachFast();
  emuRecordStage(0xf000|(code&0xfff));
}
static void IRAM_ATTR emuFillTx() {
  uint32_t available=32-emuHw->status_reg.tx_fifo_cnt;
  if (available>32 || emuTxLoaded>UINT32_MAX-32) { ++emuMetrics.fifoErrors; emuFault(1); return; }
  while (available--) {
    // Pinned i2c_ll_write_txfifo uses the classic ESP32 AHB alias for TX.
    REG_WRITE(0x6001301c,emuDevice.prefetched(emuTxLoaded++));
    ++emuMetrics.txPrefetched;
  }
}
static void IRAM_ATTR emuPrimeTx() {
  emuHw->fifo_conf.tx_fifo_rst=1; emuHw->fifo_conf.tx_fifo_rst=0;
  emuTxLoaded=emuTxByteIrqs=0; emuReadFinished=false;
  emuFillTx();
}
static bool IRAM_ATTR emuFinishRead(bool terminalNack) {
  if (emuReadFinished) return true;
  uint32_t consumed=0;
  const uint32_t remaining=emuHw->status_reg.tx_fifo_cnt;
  emuMetrics.readLoaded=emuTxLoaded; emuMetrics.readRemaining=remaining; emuMetrics.readConsumed=0;
  emuMetrics.readTxIrqs=emuTxByteIrqs; emuMetrics.readRxIrqs=emuRxByteIrqs;
  emuMetrics.readTerminalNack=terminalNack?1:0;
  if (!read_accounting::consumedAtNack(emuTxLoaded,remaining,terminalNack,consumed)) {
    ++emuMetrics.fifoErrors; emuFault(2); return false;
  }
  emuMetrics.readConsumed=consumed;
  emuDevice.completedRead(consumed); emuMetrics.txCompleted+=consumed;
  emuReadFinished=true;
  emuHw->int_ena.val &= ~I2C_TXFIFO_EMPTY_INT_ENA_M;
  return true;
}
static void IRAM_ATTR __attribute__((noinline)) emuService() {
  const uint32_t flags=emuHw->int_status.val;
  const uint32_t hwStatus=emuHw->status_reg.val;
  const uint32_t lastState=emuHw->status_reg.scl_main_state_last;
  const bool reading=emuHw->status_reg.slave_rw;
  emuMetrics.lastHwStatus=hwStatus;
  emuMetrics.lastIrqFlags=flags;
  emuMetrics.lastRxCount=emuHw->status_reg.rx_fifo_cnt;
  emuMetrics.lastTxCount=emuHw->status_reg.tx_fifo_cnt;
  // RXFULL is level-sensitive: drain its FIFO before W1C, as in pinned IDF.
  emuHw->int_clr.val=flags & ~I2C_RXFIFO_FULL_INT_ENA_M;
  if (!emulationActive) return;
  const uint32_t history=emuHistoryNext;
  emuIrqHistory[history]={flags,hwStatus,emuMetrics.lastRxCount,emuMetrics.lastTxCount};
  emuHistoryNext=(history+1)&(EMU_HISTORY_CAPACITY-1);
  if (emuHistoryCount<EMU_HISTORY_CAPACITY) ++emuHistoryCount;
  if ((flags&I2C_RXFIFO_FULL_INT_ENA_M) && emuHw->status_reg.rx_fifo_cnt==0) {
    // A stale watermark can arrive after the preceding drain. Clear it below;
    // the unchanged total ISR budget still detaches a persistent source.
    ++emuMetrics.emptyRxIrqs;
  }
  if (flags & I2C_RXFIFO_OVF_INT_ENA_M) { ++emuMetrics.rxOverflows; emuFault(3); return; }
  const bool start=flags&I2C_TRANS_START_INT_ENA_M, stop=flags&I2C_TRANS_COMPLETE_INT_ENA_M;
  if (start && stop) { ++emuMetrics.fifoErrors; emuFault(4); return; }
  if (start && emuHw->status_reg.rx_fifo_cnt) { ++emuMetrics.fifoErrors; emuFault(4); return; }
  // Count only a completed data/ACK state. The TRM/header dispute is deliberately
  // a qualification gate; FIFO disagreement below detaches rather than guessing.
  if (flags & I2C_SLAVE_TRAN_COMP_INT_ENA_M) {
    ++emuMetrics.byteIrqs;
    if (reading && lastState==6) ++emuTxByteIrqs;
    else if (!reading && lastState==5) ++emuRxByteIrqs;
  }
  if ((flags&I2C_ACK_ERR_INT_ENA_M) && reading) {
    ++emuMetrics.nacks;
    if (lastState!=6 || !emuFinishRead(true)) { if (emulationActive) emuFault(5); return; }
  }
  if (start) {
    ++emuMetrics.starts;
    emuDevice.start();
    emuRxByteIrqs=0; emuRxEvidence.reset(); emuPrimeTx();
  }
  uint32_t rxCount=emuHw->status_reg.rx_fifo_cnt;
  if (rxCount>32 || (reading && rxCount)) { ++emuMetrics.fifoErrors; emuFault(6); return; }
  emuRxEvidence.observe(rxCount,reading,lastState,hwStatus);
  while (rxCount--) {
    const bool pointer=emuDevice.expectsPointer;
    emuDevice.receive(uint8_t(emuHw->fifo_data.val)); ++emuMetrics.rxBytes;
    if (pointer) emuPrimeTx();
  }
  if (flags&I2C_RXFIFO_FULL_INT_ENA_M) emuHw->int_clr.val=I2C_RXFIFO_FULL_INT_ENA_M;
  if (stop) {
    ++emuMetrics.stops;
    if (reading && !emuReadFinished && !emuFinishRead(false)) return;
    if (emuDevice.writeDataSeen) {
      if (!write_rules::eligible(emuRxEvidence,emuDevice.rxBytes,emuDevice.writtenBytes,
          emuDevice.generation,stop,emuHw->status_reg.bus_busy,reading,emuHw->status_reg.rx_fifo_cnt)) {
        ++emuMetrics.invalidStops; emuFault(7); return;
      }
      volatile EmuWriteRecord &record=emuWriteHistory[emuWriteNext];
      emuCaptureWrite(record,0,flags,hwStatus);
      emuDevice.stop(true);
      record.generationAfter=emuDevice.generation; record.committed=1; record.valid=1;
      emuWriteNext=(emuWriteNext+1)&3; if (emuWriteCount<4) ++emuWriteCount;
    } else emuDevice.stop(false);
    emuRxByteIrqs=0; emuRxEvidence.reset(); emuPrimeTx();
  }
  if ((flags&I2C_TXFIFO_EMPTY_INT_ENA_M) && !emuReadFinished) {
    if (reading && emuHw->status_reg.tx_fifo_cnt==0) { ++emuMetrics.txUnderflows; emuFault(8); return; }
    emuFillTx();
  }
  if (emulationActive && !emuReadFinished) emuHw->int_ena.val |= I2C_TXFIFO_EMPTY_INT_ENA_M;
}
static void IRAM_ATTR __attribute__((noinline)) emuIsr(void *) {
  const uint32_t started=XTHAL_GET_CCOUNT();
  ++emuMetrics.isrCalls;
  if (!emuIrqBudget.enter(started)) { ++emuMetrics.irqStorms; emuFault(14); return; }
  emuService();
  const uint32_t elapsed=XTHAL_GET_CCOUNT()-started;
  if (elapsed>emuMetrics.maxIsrCycles) emuMetrics.maxIsrCycles=elapsed;
  if (emulationActive && !emuIrqBudget.finish(elapsed)) { ++emuMetrics.irqStorms; emuFault(14); }
}

static void emuConfigureDetached() {
  periph_module_enable(PERIPH_I2C0_MODULE);
  // Reset only the detached peripheral FSM. This is not the SDK recovery path:
  // neither GPIO is connected to a peripheral output and no clocks are emitted.
  emuHw->int_ena.val=0;
  periph_module_reset(PERIPH_I2C0_MODULE);
  emuHw->int_ena.val=0; emuHw->int_clr.val=0xffffffff;
  // Exact register operations of pinned i2c_ll slave/FIFO helpers. The full C
  // header is not C++-compatible in this core; do not relax compiler checks.
  emuHw->ctr.val=0; emuHw->ctr.sda_force_out=1; emuHw->ctr.scl_force_out=1;
  emuHw->fifo_conf.nonfifo_en=0;
  emuHw->fifo_conf.fifo_addr_cfg_en=0; // Software must receive the pointer byte.
  emuHw->slave_addr.addr=0x50; emuHw->slave_addr.en_10bit=0;
  // Actual V8 detached hardware reasserted RXFULL at empty/threshold0. Use1
  // empirically; first-pointer service still requires printer qualification.
  emuHw->fifo_conf.rx_fifo_full_thrhd=1; emuHw->fifo_conf.tx_fifo_empty_thrhd=16;
  // Pinned IDF4.4.7 i2c_ll_cal_bus_clk / set_bus_timing, APB80MHz / 100kHz.
  // Fully initialize timing even though SCL has no output matrix connection.
  emuHw->scl_filter_cfg.thres=3; emuHw->scl_filter_cfg.en=1;
  emuHw->sda_filter_cfg.thres=3; emuHw->sda_filter_cfg.en=1;
  emuHw->scl_low_period.period=399; emuHw->scl_high_period.period=391;
  emuHw->sda_hold.time=200; emuHw->sda_sample.time=200;
  emuHw->scl_rstart_setup.time=400; emuHw->scl_stop_setup.time=400;
  emuHw->scl_start_hold.time=400; emuHw->scl_stop_hold.time=400;
  emuHw->timeout.tout=8000;
  emuHw->fifo_conf.rx_fifo_rst=1; emuHw->fifo_conf.rx_fifo_rst=0;
  emuHw->fifo_conf.tx_fifo_rst=1; emuHw->fifo_conf.tx_fifo_rst=0;
}
static void emuStartPhase(const char *phase);
static bool emuAttachHardware() {
  if (!emuAttachPreflight()) return false;
  emuAttachDiagnostics.stage="peripheral_reset";
  emuRecordStage(18); emuConfigureDetached();
  emuDevice.start(); emuRxByteIrqs=0; emuRxEvidence.reset();
  emuRejectedWrite.valid=0; emuWriteNext=emuWriteCount=0;
  emuPrimeTx();
  emuRecordStage(19);
  if (emuHw->status_reg.tx_fifo_cnt!=32) { emuAttachDiagnostics.stage="fifo_prime_mismatch"; return false; }
  emuStartPhase("fifo_loaded"); // Still GPIO input-only; no live response deadline.
  emuAttachDiagnostics.stage="interrupt_alloc";
  if (!emuInterrupt) {
    emuAttachDiagnostics.irqError=esp_intr_alloc(ETS_I2C_EXT0_INTR_SOURCE,
      ESP_INTR_FLAG_IRAM|ESP_INTR_FLAG_LEVEL3,emuIsr,nullptr,&emuInterrupt);
    if (emuAttachDiagnostics.irqError!=ESP_OK) return false;
  }
  emuRecordStage(20);
  emuAttachDiagnostics.stage="gpio_set_level";
  emuAttachDiagnostics.gpioError=gpio_set_level(SDA_PIN,1);
  if (emuAttachDiagnostics.gpioError!=ESP_OK) return false;
  gpio_config_t pins={}; pins.pin_bit_mask=1ULL<<SDA_PIN; pins.mode=GPIO_MODE_INPUT_OUTPUT_OD;
  pins.pull_up_en=GPIO_PULLUP_DISABLE; pins.pull_down_en=GPIO_PULLDOWN_DISABLE;
  pins.intr_type=GPIO_INTR_DISABLE;
  emuAttachDiagnostics.stage="gpio_config";
  emuAttachDiagnostics.gpioError=gpio_config(&pins);
  if (emuAttachDiagnostics.gpioError!=ESP_OK) return false;
  emuRecordStage(21);
  gpio_matrix_in(SDA_PIN,I2CEXT0_SDA_IN_IDX,false);
  gpio_matrix_in(SCL_PIN,I2CEXT0_SCL_IN_IDX,false);
  gpio_matrix_out(SDA_PIN,I2CEXT0_SDA_OUT_IDX,false,false);
  emulationActive=true;
  emuHw->int_clr.val=0xffffffff;
  emuIrqBudget.reset(XTHAL_GET_CCOUNT());
  emuRecordStage(23);
  emuHw->int_ena.val=I2C_RXFIFO_FULL_INT_ENA_M|I2C_RXFIFO_OVF_INT_ENA_M|
    I2C_TXFIFO_EMPTY_INT_ENA_M|I2C_TRANS_START_INT_ENA_M|I2C_TRANS_COMPLETE_INT_ENA_M|
    I2C_SLAVE_TRAN_COMP_INT_ENA_M|I2C_ACK_ERR_INT_ENA_M;
  emuAttachDiagnostics.stage="attached";
  emuRecordStage(24);
  return true;
}
static void emuReleaseHardware() {
  emuDetachFast();
  if (emuInterrupt) { esp_intr_free(emuInterrupt); emuInterrupt=nullptr; }
  gpio_matrix_in(0x30,I2CEXT0_SDA_IN_IDX,false);
  gpio_matrix_in(0x30,I2CEXT0_SCL_IN_IDX,false);
  BenchPins pins; pins.releaseInputs();
}
