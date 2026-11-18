// Brother cartridge investigation: passive GPIO capture, classic ESP32 only.
// GPIO21=SDA, GPIO22=SCL, shared GND. Printer VCC MUST remain disconnected.
// No I2C peripheral, output pin, internal pull, Wi-Fi, EEPROM write or emulation.
#include <Arduino.h>
#include <driver/gpio.h>
#include <esp_intr_alloc.h>
#include <esp_timer.h>
#include <soc/gpio_reg.h>
#include <soc/soc.h>
#include <xtensa/core-macros.h>

#if !defined(CONFIG_IDF_TARGET_ESP32)
#error "Review GPIO/register/interrupt implementation for this ESP32 variant first"
#endif

static constexpr gpio_num_t SDA_PIN = GPIO_NUM_21;
static constexpr gpio_num_t SCL_PIN = GPIO_NUM_22;
static constexpr uint32_t CAPACITY = 8192;
static constexpr uint32_t BAUD = 921600;
static constexpr char REVISION[] = "i2c-passive-edge-v1";

struct Edge { uint32_t timestamp_us; uint32_t cycles; uint32_t levels; };
static_assert(sizeof(Edge) == 12, "Host record format is three little-endian u32s");
DRAM_ATTR static Edge edges[CAPACITY];
DRAM_ATTR static volatile uint32_t used = 0;
DRAM_ATTR static volatile uint32_t overflow = 0;
DRAM_ATTR static volatile uint32_t callbacks = 0;
DRAM_ATTR static volatile uint32_t lastLevels = 3;
DRAM_ATTR static volatile bool armed = false;
DRAM_ATTR static portMUX_TYPE captureMux = portMUX_INITIALIZER_UNLOCKED;
static int64_t captureStartUs = 0;
static int64_t captureEndUs = 0;
static uint32_t durationMs = 30000;
static char command[64];
static size_t commandLength = 0;

static inline uint32_t IRAM_ATTR busLevels() {
  const uint32_t snapshot = REG_READ(GPIO_IN_REG);
  return ((snapshot >> 22) & 1U) | (((snapshot >> 21) & 1U) << 1);
}

static void IRAM_ATTR onEdge(void *) {
  // Both ISR handlers run on the registration core. No serial/allocation/flash.
  const uint32_t levels = busLevels();
  const uint32_t cycles = XTHAL_GET_CCOUNT();
  const uint32_t us = static_cast<uint32_t>(esp_timer_get_time());
  portENTER_CRITICAL_ISR(&captureMux);
  if (armed) {
    ++callbacks;
    if (levels != lastLevels) {
      if (used < CAPACITY) {
        edges[used++] = {us, cycles, levels};
        lastLevels = levels;
      } else {
        ++overflow;
        armed = false;  // Preserve earliest data; never wrap over evidence.
      }
    }
  }
  portEXIT_CRITICAL_ISR(&captureMux);
}

static void stopCapture() {
  gpio_intr_disable(SDA_PIN);
  gpio_intr_disable(SCL_PIN);
  portENTER_CRITICAL(&captureMux);
  armed = false;
  if (captureEndUs == 0) captureEndUs = esp_timer_get_time();
  portEXIT_CRITICAL(&captureMux);
}

static void status() {
  uint32_t count, drops, calls, levels;
  bool active;
  portENTER_CRITICAL(&captureMux);
  count = used; drops = overflow; calls = callbacks; active = armed;
  levels = busLevels();
  portEXIT_CRITICAL(&captureMux);
  Serial.printf("{\"type\":\"status\",\"firmware\":\"%s\",\"armed\":%s,"
                "\"event_count\":%lu,\"overflow\":%lu,\"isr_callbacks\":%lu,"
                "\"levels\":%lu,\"sda_gpio\":21,\"scl_gpio\":22,"
                "\"pins_input_only\":true,\"internal_pulls\":false,\"cpu_mhz\":%u}\n",
                REVISION, active ? "true" : "false", (unsigned long)count,
                (unsigned long)drops, (unsigned long)calls, (unsigned long)levels,
                getCpuFrequencyMhz());
}

static void startCapture(uint32_t milliseconds) {
  stopCapture();
  portENTER_CRITICAL(&captureMux);
  used = 0; overflow = 0; callbacks = 0;
  captureStartUs = esp_timer_get_time(); captureEndUs = 0;
  lastLevels = busLevels();
  edges[used++] = {static_cast<uint32_t>(captureStartUs), XTHAL_GET_CCOUNT(), lastLevels};
  durationMs = milliseconds;
  armed = true;
  portEXIT_CRITICAL(&captureMux);
  gpio_intr_enable(SDA_PIN);
  gpio_intr_enable(SCL_PIN);
  status();
}

static uint32_t crc32(const uint8_t *data, size_t length) {
  uint32_t crc = 0xffffffffU;
  for (size_t i = 0; i < length; ++i) {
    crc ^= data[i];
    for (unsigned int bit = 0; bit < 8; ++bit)
      crc = (crc >> 1) ^ (0xedb88320U & (0U - (crc & 1U)));
  }
  return ~crc;
}

static void dumpCapture() {
  stopCapture();
  const uint32_t count = used;
  const size_t bytes = count * sizeof(Edge);
  const uint32_t crc = crc32(reinterpret_cast<const uint8_t *>(edges), bytes);
  Serial.printf("{\"type\":\"capture\",\"schema\":\"sunburn.i2c.raw-edges.v1\","
                "\"firmware\":\"%s\",\"record_format\":\"<III\",\"record_bytes\":12,"
                "\"event_count\":%lu,\"bytes\":%u,\"capacity\":%lu,"
                "\"overflow\":%lu,\"isr_callbacks\":%lu,\"cpu_mhz\":%u,"
                "\"capture_start_us\":%lld,\"capture_end_us\":%lld,\"crc32\":\"%08lx\","
                "\"levels_bits\":\"bit0=SCL,bit1=SDA\",\"pins_input_only\":true,"
                "\"internal_pulls\":false,\"lossless_claim\":false}\n",
                REVISION, (unsigned long)count, unsigned(bytes), (unsigned long)CAPACITY,
                (unsigned long)overflow, (unsigned long)callbacks, getCpuFrequencyMhz(),
                (long long)captureStartUs, (long long)captureEndUs, (unsigned long)crc);
  Serial.write(reinterpret_cast<const uint8_t *>(edges), bytes);
  Serial.println();
  Serial.println("{\"type\":\"dump_end\"}");
}

static void processCommand(const char *line) {
  if (strcmp(line, "INFO") == 0 || strcmp(line, "STATUS") == 0) status();
  else if (strcmp(line, "STOP") == 0) { stopCapture(); status(); }
  else if (strcmp(line, "DUMP") == 0) dumpCapture();
  else if (strncmp(line, "ARM ", 4) == 0) {
    char *end = nullptr;
    const unsigned long ms = strtoul(line + 4, &end, 10);
    if (!end || *end || ms < 100 || ms > 120000)
      Serial.println("{\"type\":\"error\",\"message\":\"ARM duration must be100..120000ms\"}");
    else startCapture(static_cast<uint32_t>(ms));
  } else Serial.println("{\"type\":\"error\",\"message\":\"Commands:INFO,ARM milliseconds,STOP,DUMP\"}");
}

void setup() {
  setCpuFrequencyMhz(240);
  gpio_config_t pins = {};
  pins.pin_bit_mask = (1ULL << SDA_PIN) | (1ULL << SCL_PIN);
  pins.mode = GPIO_MODE_INPUT;
  pins.pull_up_en = GPIO_PULLUP_DISABLE;
  pins.pull_down_en = GPIO_PULLDOWN_DISABLE;
  pins.intr_type = GPIO_INTR_DISABLE;
  ESP_ERROR_CHECK(gpio_config(&pins));
  ESP_ERROR_CHECK(gpio_install_isr_service(ESP_INTR_FLAG_IRAM | ESP_INTR_FLAG_LEVEL3));
  ESP_ERROR_CHECK(gpio_isr_handler_add(SDA_PIN, onEdge, nullptr));
  ESP_ERROR_CHECK(gpio_isr_handler_add(SCL_PIN, onEdge, nullptr));
  ESP_ERROR_CHECK(gpio_set_intr_type(SDA_PIN, GPIO_INTR_ANYEDGE));
  ESP_ERROR_CHECK(gpio_set_intr_type(SCL_PIN, GPIO_INTR_ANYEDGE));
  gpio_intr_disable(SDA_PIN); gpio_intr_disable(SCL_PIN);
  Serial.begin(BAUD);
  Serial.setTimeout(20);
  delay(100);
  status();
}

void loop() {
  if ((armed && esp_timer_get_time() - captureStartUs >= int64_t(durationMs) * 1000)
      || (!armed && used > 0 && captureEndUs == 0)) stopCapture();
  while (Serial.available()) {
    const char c = static_cast<char>(Serial.read());
    if (c == '\n') {
      command[commandLength] = 0;
      processCommand(command);
      commandLength = 0;
    } else if (c != '\r') {
      if (commandLength + 1 < sizeof(command)) command[commandLength++] = c;
      else commandLength = 0;
    }
  }
  delay(1);
}
