#pragma once

struct EmuDiagRegisters {
  uint32_t raw, enabled, masked, status, ctr, fifo, rx, tx, levels, outputEnable, sdaOutput, sclOutput;
};
static EmuDiagRegisters emuDiagRegisters() {
  return {emuHw->int_raw.val,emuHw->int_ena.val,emuHw->int_status.val,
    emuHw->status_reg.val,emuHw->ctr.val,emuHw->fifo_conf.val,
    emuHw->status_reg.rx_fifo_cnt,emuHw->status_reg.tx_fifo_cnt,busLevels(),REG_READ(GPIO_ENABLE_REG),
    REG_READ(GPIO_FUNC21_OUT_SEL_CFG_REG),REG_READ(GPIO_FUNC22_OUT_SEL_CFG_REG)};
}
static void emuPrintDiagRegisters(const EmuDiagRegisters &r) {
  Serial.printf("\"raw_interrupts\":%lu,\"enabled_interrupts\":%lu,\"masked_interrupts\":%lu,"
    "\"hw_status\":%lu,\"ctr\":%lu,\"fifo_conf\":%lu,\"rx_fifo\":%lu,\"tx_fifo\":%lu,\"levels\":%lu,"
    "\"gpio_output_enable\":%lu,\"sda_output_select\":%lu,\"scl_output_select\":%lu",
    (unsigned long)r.raw,(unsigned long)r.enabled,(unsigned long)r.masked,(unsigned long)r.status,
    (unsigned long)r.ctr,(unsigned long)r.fifo,(unsigned long)r.rx,(unsigned long)r.tx,(unsigned long)r.levels,
    (unsigned long)r.outputEnable,(unsigned long)r.sdaOutput,(unsigned long)r.sclOutput);
}
static void emuDiagPhase(const char *phase) {
  Serial.printf("{\"type\":\"emu_diag_phase\",\"op\":\"diag\",\"phase\":\"%s\",",phase);
  emuPrintDiagRegisters(emuDiagRegisters()); Serial.println("}"); Serial.flush();
}
DRAM_ATTR static volatile uint32_t emuDiagIrqs=0, emuDiagObservedIrq=0, emuDiagObservedStatus=0;
static bool emuDiagOutputsDetached() {
  return benchPinsInputOnly() &&
    REG_READ(GPIO_FUNC21_OUT_SEL_CFG_REG)==(SIG_GPIO_OUT_IDX|GPIO_FUNC21_OEN_SEL) &&
    REG_READ(GPIO_FUNC22_OUT_SEL_CFG_REG)==(SIG_GPIO_OUT_IDX|GPIO_FUNC22_OEN_SEL);
}
static void IRAM_ATTR __attribute__((noinline)) emuDiagIsr(void *) {
  const uint32_t flags=emuHw->int_status.val, status=emuHw->status_reg.val;
  emuHw->int_ena.val=0; // One shot: no repeated level IRQ can starve UART/task.
  emuDiagObservedIrq=flags; emuDiagObservedStatus=status; ++emuDiagIrqs;
  emuHw->int_clr.val=flags;
}
static void emuDetachedDiagnostic() {
  bool ok=false; const char *message="diagnostic_guard_failed";
  intr_handle_t handle=nullptr;
  EmuDiagRegisters finalRegisters={};
  emuDiagIrqs=emuDiagObservedIrq=emuDiagObservedStatus=0;
  if (emulationActive || emulationSessionOpen || armed || updateBusy || recoveryRequired ||
      xPortGetCoreID()!=1 || !benchPinsInputOnly() || !benchPinsPullsOff()) {
    Serial.print("{\"type\":\"emu_diag\",\"ok\":false,\"message\":\"requires_closed_passive_state\",");
    emuState(emuDevice.generation); Serial.println("}"); return;
  }
  // Explicit GPIO/OEN detachment, not just GPIO_ENABLE inspection: a leftover
  // peripheral OEN must never bypass the GPIO output-disable register.
  REG_WRITE(GPIO_FUNC21_OUT_SEL_CFG_REG,SIG_GPIO_OUT_IDX|GPIO_FUNC21_OEN_SEL);
  REG_WRITE(GPIO_FUNC22_OUT_SEL_CFG_REG,SIG_GPIO_OUT_IDX|GPIO_FUNC22_OEN_SEL);
  REG_WRITE(GPIO_ENABLE_W1TC_REG,BUS_MASK);
  REG_WRITE(GPIO_OUT_W1TS_REG,BUS_MASK);
  emuRecordStage(1);
  const bool networkSuspended=pauseNetworking();
  if (networkSuspended && emuDiagOutputsDetached() && benchPinsPullsOff()) {
    // Both outputs remain GPIO-disabled for the entire diagnostic. Input matrix
    // observation cannot ACK an address or drive a signal. No recovery clocks.
    emuConfigureDetached(); emuRecordStage(2); emuDiagPhase("reset");
    for (uint32_t i=0;i<32;++i) REG_WRITE(0x6001301c,emuDevice.prefetched(i));
    emuRecordStage(3); emuDiagPhase("fifo_loaded");
    gpio_matrix_in(SDA_PIN,I2CEXT0_SDA_IN_IDX,false);
    gpio_matrix_in(SCL_PIN,I2CEXT0_SCL_IN_IDX,false);
    emuHw->int_clr.val=0xffffffff;
    emuRecordStage(4); emuDiagPhase("pre_enable");
    const esp_err_t error=esp_intr_alloc(ETS_I2C_EXT0_INTR_SOURCE,
      ESP_INTR_FLAG_IRAM|ESP_INTR_FLAG_LEVEL3,emuDiagIsr,nullptr,&handle);
    if (error==ESP_OK && emuDiagOutputsDetached() && benchPinsPullsOff()) {
      emuRecordStage(5);
      emuHw->int_ena.val=I2C_RXFIFO_FULL_INT_ENA_M|I2C_RXFIFO_OVF_INT_ENA_M|
        I2C_TXFIFO_EMPTY_INT_ENA_M|I2C_TRANS_START_INT_ENA_M|I2C_TRANS_COMPLETE_INT_ENA_M|
        I2C_SLAVE_TRAN_COMP_INT_ENA_M|I2C_ACK_ERR_INT_ENA_M;
      delay(5);
      emuHw->int_ena.val=0;
      finalRegisters=emuDiagRegisters();
      ok=emuDiagIrqs<=1; message=ok?"detached_diagnostic_complete":"one_shot_irq_bound_failed";
    } else message="diagnostic_irq_or_pin_setup_failed";
    emuHw->int_ena.val=0;
    if (handle) esp_intr_free(handle);
    gpio_matrix_in(0x30,I2CEXT0_SDA_IN_IDX,false);
    gpio_matrix_in(0x30,I2CEXT0_SCL_IN_IDX,false);
    BenchPins pins;
    if (!pins.releaseInputs()) { ok=false; message="diagnostic_gpio_release_failed"; }
  } else message=networkSuspended?"diagnostic_matrix_not_detached":"network_suspend_failed";
  emuRecordStage(6);
  const bool detached=emuDiagOutputsDetached();
  if (!detached) { ok=false; message="diagnostic_matrix_not_detached"; }
  Serial.printf("{\"type\":\"emu_diag\",\"ok\":%s,\"message\":\"%s\",",ok?"true":"false",message);
  emuPrintDiagRegisters(finalRegisters);
  Serial.printf(",\"one_shot_irq_count\":%lu,\"observed_irq\":%lu,\"observed_status\":%lu,"
    "\"observation_ms\":5,\"outputs_enabled\":%s,",(unsigned long)emuDiagIrqs,
    (unsigned long)emuDiagObservedIrq,(unsigned long)emuDiagObservedStatus,detached?"false":"true");
  emuState(emuDevice.generation); Serial.println("}");
  resumeNetworking();
}
