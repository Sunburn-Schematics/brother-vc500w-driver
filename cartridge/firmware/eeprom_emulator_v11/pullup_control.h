#pragma once
#include <soc/gpio_sig_map.h>

// Optional physical wiring: GPIO19 -- 4.7k -- SDA. This is a switched pullup,
// not a direct bus driver. GPIO19 may source USB power: printer MUST be powered
// whenever ON; no automatic printer-power sensing or backfeed protection exists.
static constexpr gpio_num_t PULLUP_PIN=GPIO_NUM_19;
static constexpr uint32_t PULLUP_MASK=1U<<19;
static bool pullupConfiguredOn=false;
static bool pullupPullsOff() {
  return (REG_READ(IO_MUX_GPIO19_REG)&(FUN_PU|FUN_PD))==0;
}
static bool pullupOffVerified() {
  return !pullupConfiguredOn && !(REG_READ(GPIO_ENABLE_REG)&PULLUP_MASK) &&
    REG_READ(GPIO_FUNC19_OUT_SEL_CFG_REG)==(SIG_GPIO_OUT_IDX|GPIO_FUNC19_OEN_SEL) && pullupPullsOff();
}
static bool pullupOff() {
  // Set only HIGH, including while output disabled; OFF never means LOW.
  REG_WRITE(GPIO_ENABLE_W1TC_REG,PULLUP_MASK);
  REG_WRITE(GPIO_OUT_W1TS_REG,PULLUP_MASK);
  gpio_config_t pins={}; pins.pin_bit_mask=1ULL<<PULLUP_PIN;
  pins.mode=GPIO_MODE_INPUT; pins.pull_up_en=GPIO_PULLUP_DISABLE;
  pins.pull_down_en=GPIO_PULLDOWN_DISABLE; pins.intr_type=GPIO_INTR_DISABLE;
  const bool configuredPin=gpio_config(&pins)==ESP_OK;
  // gpio_config's output-disable helper writes selector256; restore explicit OEN.
  REG_WRITE(GPIO_FUNC19_OUT_SEL_CFG_REG,SIG_GPIO_OUT_IDX|GPIO_FUNC19_OEN_SEL);
  REG_WRITE(GPIO_ENABLE_W1TC_REG,PULLUP_MASK);
  REG_WRITE(GPIO_OUT_W1TS_REG,PULLUP_MASK);
  pullupConfiguredOn=false;
  return configuredPin && pullupOffVerified();
}
static bool pullupOnVerified() {
  return pullupConfiguredOn && (REG_READ(GPIO_ENABLE_REG)&PULLUP_MASK) &&
    (REG_READ(GPIO_OUT_REG)&PULLUP_MASK) && (REG_READ(GPIO_IN_REG)&PULLUP_MASK) &&
    !(REG_READ(GPIO_PIN19_REG)&GPIO_PIN19_PAD_DRIVER) &&
    REG_READ(GPIO_FUNC19_OUT_SEL_CFG_REG)==(SIG_GPIO_OUT_IDX|GPIO_FUNC19_OEN_SEL) && pullupPullsOff();
}
static bool pullupOn() {
  if (!pullupOffVerified()) return false;
  REG_WRITE(GPIO_OUT_W1TS_REG,PULLUP_MASK); // HIGH before output-enable.
  gpio_config_t pins={}; pins.pin_bit_mask=1ULL<<PULLUP_PIN;
  pins.mode=GPIO_MODE_INPUT_OUTPUT; pins.pull_up_en=GPIO_PULLUP_DISABLE;
  pins.pull_down_en=GPIO_PULLDOWN_DISABLE; pins.intr_type=GPIO_INTR_DISABLE;
  if (gpio_config(&pins)!=ESP_OK) { pullupOff(); return false; }
  REG_WRITE(GPIO_FUNC19_OUT_SEL_CFG_REG,SIG_GPIO_OUT_IDX|GPIO_FUNC19_OEN_SEL);
  REG_WRITE(GPIO_OUT_W1TS_REG,PULLUP_MASK);
  pullupConfiguredOn=true;
  if (!pullupOnVerified()) { pullupOff(); return false; }
  return true;
}
static void pullupState() {
  Serial.printf(",\"pullup_control\":{\"gpio\":19,\"configured_on\":%s,\"output_enabled\":%s,"
    "\"output_high\":%s,\"input_high\":%s,\"output_select\":%lu,\"internal_pulls\":%s,\"off_verified\":%s}",
    pullupConfiguredOn?"true":"false",(REG_READ(GPIO_ENABLE_REG)&PULLUP_MASK)?"true":"false",
    (REG_READ(GPIO_OUT_REG)&PULLUP_MASK)?"true":"false",(REG_READ(GPIO_IN_REG)&PULLUP_MASK)?"true":"false",
    (unsigned long)REG_READ(GPIO_FUNC19_OUT_SEL_CFG_REG),pullupPullsOff()?"false":"true",
    pullupOffVerified()?"true":"false");
}
