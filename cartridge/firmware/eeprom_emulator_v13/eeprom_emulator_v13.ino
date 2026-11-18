// Brother cartridge investigation: passive GPIO capture, classic ESP32 only.
// GPIO21=SDA, GPIO22=SCL, shared GND. Printer VCC MUST remain disconnected.
// Default is passive. Explicit isolated BENCH commands temporarily use open-drain I/O.
// Both printer signal wires must be physically isolated before any BENCH command.
// Networking is available only while capture is disarmed; credentials arrive over USB.
// Experiment V6: hardware-slave EEPROM prototype; boots passive, explicitly armed only.
// Template: frozen V5; all fourteen management bodies and passive polling are preserved.
#include <Arduino.h>
#include <ArduinoOTA.h>
#include <Preferences.h>
#include <Update.h>
#include <WiFi.h>
#include <esp_heap_caps.h>
#include <esp_ota_ops.h>
#include <mbedtls/sha256.h>
#include <stddef.h>
#include "control_rules.h"
#include "poll_rules.h"
#include <driver/gpio.h>
#include <esp_intr_alloc.h>
#include <esp_timer.h>
#include <soc/gpio_reg.h>
#include <soc/io_mux_reg.h>
#include <soc/soc.h>
#include <xtensa/core-macros.h>

#if !defined(CONFIG_IDF_TARGET_ESP32)
#error "Review GPIO/register/interrupt implementation for this ESP32 variant first"
#endif

static constexpr gpio_num_t SDA_PIN = GPIO_NUM_21;
static constexpr gpio_num_t SCL_PIN = GPIO_NUM_22;
static constexpr uint32_t CAPACITY = 8192;
static constexpr uint32_t BAUD = 921600;
static constexpr char REVISION[] = "i2c-eeprom-emulator-v13";

struct Edge { uint32_t timestamp_us; uint32_t cycles; uint32_t levels; };
static_assert(sizeof(Edge) == 12, "Host record format is three little-endian u32s");
// Allocate once from internal RAM, never PSRAM or from inside the masked hot loop.
DRAM_ATTR static Edge *edges = nullptr;
DRAM_ATTR static volatile uint32_t used = 0;
DRAM_ATTR static volatile uint32_t overflow = 0;
DRAM_ATTR static volatile uint32_t callbacks = 0;
static constexpr uint32_t CYCLES_PER_US = 240;
static constexpr uint32_t WAIT_SLICE_US = 2000;
static constexpr uint32_t TRIGGER_TAIL_US = 100000;
static constexpr uint32_t MAX_MASKED_US = WAIT_SLICE_US + TRIGGER_TAIL_US;
static constexpr uint32_t MAX_WINDOWS = 128;
static constexpr uint32_t BUS_MASK = (1U << 21) | (1U << 22);
struct SamplingWindow {
  uint32_t firstEvent;
  uint32_t eventCount;
  int64_t anchorUs;
  uint32_t anchorCycles;
  uint32_t anchorSpanCycles;
  uint32_t entryGapCycles;
  uint32_t maxPollGapCycles;
  uint32_t maskedCycles;
  uint32_t pollSpanCycles;
  uint32_t endCycles;
  uint32_t polls;
  int32_t triggerEvent;
  uint32_t triggerCycles;
  uint32_t endReason; // 0=wait limit, 1=trigger tail, 2=duration limit, 3=edge capacity.
  bool edgeOverflow;
};
static SamplingWindow windows[MAX_WINDOWS];
static uint32_t windowCount = 0;
static uint32_t idleSlices = 0;
static uint32_t pollIterations = 0;
static uint32_t maxMaskedCycles = 0;
static bool metadataOverflow = false;
DRAM_ATTR static volatile bool armed = false;
DRAM_ATTR static portMUX_TYPE captureMux = portMUX_INITIALIZER_UNLOCKED;
static int64_t captureStartUs = 0;
static int64_t captureEndUs = 0;
static uint32_t durationMs = 30000;
static char command[320];
static bool commandTooLong = false;
static size_t commandLength = 0;

static constexpr uint16_t OTA_PORT = 3232;
static constexpr uint32_t USB_IDLE_TIMEOUT_MS = 15000;
static constexpr uint32_t USB_TOTAL_TIMEOUT_MS = 180000;
struct NetworkConfig {
  uint32_t magic;
  uint32_t version;
  char ssid[33];
  char password[65];
  char otaHash[33];
  uint32_t crc;
};
static NetworkConfig networkConfig = {};
static bool configured = false;
static bool networkPaused = true;
static bool otaStarted = false;
static bool updateBusy = false;
static bool recoveryRequired = false;
static char recoveryNonce[17] = {};
static char otaHostname[32] = {};
static uint32_t lastNetworkAttemptMs = 0;

static inline uint32_t IRAM_ATTR busLevels() {
  const uint32_t snapshot = REG_READ(GPIO_IN_REG);
  return ((snapshot >> 22) & 1U) | (((snapshot >> 21) & 1U) << 1);
}

// No timer/serial/allocation/network/flash call occurs inside this function's
// masked body. Only GPIO snapshots, CCOUNT, bounded arithmetic and internal RAM.
static void IRAM_ATTR __attribute__((noinline)) pollWindow(SamplingWindow &window, uint32_t budgetCycles) {
  Edge *const buffer = edges;
  uint32_t count = used;
  const uint32_t first = count;
  uint32_t polls = 0;
  uint32_t maximumGap = 0;
  bool triggered = false;
  bool full = false;
  int32_t triggerIndex = -1;
  uint32_t triggerCycle = 0;
  uint32_t limitCycles = WAIT_SLICE_US * CYCLES_PER_US;
  if (limitCycles > budgetCycles) limitCycles = budgetCycles;
  const uint32_t beforeEnter = XTHAL_GET_CCOUNT();
  portENTER_CRITICAL(&captureMux);
  uint32_t previousRaw = REG_READ(GPIO_IN_REG) & BUS_MASK;
  const uint32_t startCycle = XTHAL_GET_CCOUNT();
  uint32_t previousPoll = startCycle;
  buffer[count].timestamp_us = 0; // Filled after interrupts are restored.
  buffer[count].cycles = startCycle;
  buffer[count].levels = ((previousRaw >> 22) & 1U) | (((previousRaw >> 21) & 1U) << 1);
  ++count;
  uint32_t now = startCycle;
  while (true) {
    now = XTHAL_GET_CCOUNT();
    // Tested even if GPIO never changes. Repeated START cannot extend again.
    if (uint32_t(now - startCycle) >= limitCycles) break;
    const uint32_t raw = REG_READ(GPIO_IN_REG) & BUS_MASK;
    const uint32_t cycle = XTHAL_GET_CCOUNT();
    const uint32_t interval = cycle - previousPoll;
    previousPoll = cycle;
    if (interval > maximumGap) maximumGap = interval;
    ++polls;
    if (raw != previousRaw) {
      if (count >= CAPACITY) { full = true; break; }
      buffer[count].timestamp_us = 0;
      buffer[count].cycles = cycle;
      const uint32_t levels = ((raw >> 22) & 1U) | (((raw >> 21) & 1U) << 1);
      buffer[count].levels = levels;
      if (!triggered && previousRaw == BUS_MASK && raw == (1U << 22)) {
        triggered = true; // Exact observed SCL=1, SDA=1 -> SCL=1, SDA=0.
        triggerIndex = int32_t(count);
        triggerCycle = cycle;
        limitCycles = poll_rules::tailLimit(uint32_t(cycle - startCycle),
                                           TRIGGER_TAIL_US * CYCLES_PER_US, budgetCycles);
      }
      ++count;
      previousRaw = raw;
    }
  }
  const uint32_t endCycle = XTHAL_GET_CCOUNT();
  portEXIT_CRITICAL(&captureMux);
  const uint32_t afterExit = XTHAL_GET_CCOUNT();
  // The outer span conservatively includes critical-entry/exit overhead and any
  // interrupt that runs immediately after exit. It never hides such a delay.
  window.firstEvent = first;
  window.eventCount = count - first;
  window.entryGapCycles = startCycle - (window.anchorCycles + window.anchorSpanCycles / 2);
  window.maxPollGapCycles = maximumGap;
  window.maskedCycles = afterExit - beforeEnter;
  window.pollSpanCycles = endCycle - startCycle;
  window.endCycles = endCycle;
  window.polls = polls;
  window.triggerEvent = triggerIndex;
  window.triggerCycles = triggerCycle;
  window.edgeOverflow = full;
  window.endReason = full ? 3 : (limitCycles == budgetCycles ? 2 : (triggered ? 1 : 0));
  used = count;
}

static void stopCapture() {
  // There are no GPIO ISRs. Commands run only between bounded polling windows.
  armed = false;
  if (captureEndUs == 0) captureEndUs = esp_timer_get_time();
}

static void sampleOneWindow() {
  if (!armed) return;
  if (windowCount >= MAX_WINDOWS) { metadataOverflow = true; stopCapture(); return; }
  if (used >= CAPACITY) { ++overflow; stopCapture(); return; }
  SamplingWindow window = {};
  // Same-core anchor outside the masked loop. Midpoint uncertainty is exposed.
  const uint32_t anchorBefore = XTHAL_GET_CCOUNT();
  window.anchorUs = esp_timer_get_time();
  const uint32_t anchorAfter = XTHAL_GET_CCOUNT();
  window.anchorSpanCycles = anchorAfter - anchorBefore;
  window.anchorCycles = anchorBefore + window.anchorSpanCycles / 2;
  const int64_t remainingUs = captureStartUs + int64_t(durationMs) * 1000 - window.anchorUs;
  if (remainingUs <= 0) { stopCapture(); return; }
  const uint32_t budgetUs = remainingUs < MAX_MASKED_US ? uint32_t(remainingUs) : MAX_MASKED_US;
  pollWindow(window, budgetUs * CYCLES_PER_US);
  pollIterations += window.polls;
  if (window.maskedCycles > maxMaskedCycles) maxMaskedCycles = window.maskedCycles;
  if (window.eventCount == 1) {
    // Idle windows contain no edge and are excluded from raw records/metadata.
    used = window.firstEvent;
    ++idleSlices;
  } else {
    for (uint32_t i = window.firstEvent; i < window.firstEvent + window.eventCount; ++i)
      edges[i].timestamp_us = uint32_t(poll_rules::timestampUs(window.anchorUs, window.anchorCycles,
                                                            edges[i].cycles, CYCLES_PER_US));
    windows[windowCount++] = window;
  }
  if (window.edgeOverflow) { ++overflow; stopCapture(); }
}

static bool benchPinsInputOnly() {
  return (REG_READ(GPIO_ENABLE_REG) & BUS_MASK) == 0;
}

static bool benchPinsPullsOff() {
  return ((REG_READ(IO_MUX_GPIO21_REG) | REG_READ(IO_MUX_GPIO22_REG)) & (FUN_PU | FUN_PD)) == 0;
}

static void status() {
  const uint32_t levels = busLevels();
  Serial.printf("{\"type\":\"status\",\"firmware\":\"%s\",\"armed\":%s,"
                "\"event_count\":%lu,\"overflow\":%lu,\"isr_callbacks\":0,"
                "\"levels\":%lu,\"sda_gpio\":21,\"scl_gpio\":22,"
                "\"pins_input_only\":%s,\"internal_pulls\":%s,\"cpu_mhz\":%u,"
                "\"bench_capability\":\"isolated_restore_initial_v1\","
                "\"emulator_capability\":\"eeprom_256_experimental_v1\","
                "\"buffer_ready\":%s,\"update_busy\":%s,\"recovery_required\":%s,"
                "\"capture_mode\":\"windowed_polling\",\"sampling_window_count\":%lu,"
                "\"discarded_idle_slices\":%lu,\"sampling_metadata_overflow\":%s}\n",
                REVISION, armed ? "true" : "false", (unsigned long)used,
                (unsigned long)overflow, (unsigned long)levels, benchPinsInputOnly() ? "true" : "false",
                benchPinsPullsOff() ? "false" : "true", getCpuFrequencyMhz(),
                edges ? "true" : "false", updateBusy ? "true" : "false",
                recoveryRequired ? "true" : "false", (unsigned long)windowCount,
                (unsigned long)idleSlices, metadataOverflow ? "true" : "false");
}

static void startCapture(uint32_t milliseconds) {
  stopCapture();
  if (!edges || xPortGetCoreID() != 1 || getCpuFrequencyMhz() != CYCLES_PER_US || !pauseNetworking()) {
    Serial.println("{\"type\":\"error\",\"message\":\"capture_buffer_cpu_or_network_suspend_failed\"}");
    return;
  }
  used = 0; overflow = 0; callbacks = 0;
  windowCount = 0; idleSlices = 0; pollIterations = 0; maxMaskedCycles = 0;
  metadataOverflow = false;
  captureStartUs = esp_timer_get_time(); captureEndUs = 0;
  durationMs = milliseconds;
  armed = true;
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
  if (!edges) { Serial.println("{\"type\":\"error\",\"message\":\"capture_buffer_unavailable\"}"); return; }
  const uint32_t count = used;
  const size_t bytes = count * sizeof(Edge);
  const uint32_t crc = crc32(reinterpret_cast<const uint8_t *>(edges), bytes);
  Serial.printf("{\"type\":\"capture\",\"schema\":\"sunburn.i2c.raw-edges.v1\","
                "\"firmware\":\"%s\",\"record_format\":\"<III\",\"record_bytes\":12,"
                "\"event_count\":%lu,\"bytes\":%u,\"capacity\":%lu,"
                "\"overflow\":%lu,\"isr_callbacks\":0,\"cpu_mhz\":%u,"
                "\"capture_start_us\":%lld,\"capture_end_us\":%lld,\"crc32\":\"%08lx\","
                "\"levels_bits\":\"bit0=SCL,bit1=SDA\",\"pins_input_only\":%s,"
                "\"internal_pulls\":%s,\"lossless_claim\":false,"
                "\"capture_mode\":\"windowed_polling\",\"sampling_gaps_possible\":true,"
                "\"sampling_metadata_overflow\":%s,\"discarded_idle_slices\":%lu,"
                "\"poll_iterations\":%lu,\"max_masked_cycles\":%lu,\"max_masked_us_nominal\":102000,"
                "\"wait_slice_us\":2000,\"trigger_tail_us\":100000,\"sampling_windows\":[",
                REVISION, (unsigned long)count, unsigned(bytes), (unsigned long)CAPACITY,
                (unsigned long)overflow, getCpuFrequencyMhz(), (long long)captureStartUs,
                (long long)captureEndUs, (unsigned long)crc, benchPinsInputOnly() ? "true" : "false",
                benchPinsPullsOff() ? "false" : "true", metadataOverflow ? "true" : "false",
                (unsigned long)idleSlices, (unsigned long)pollIterations, (unsigned long)maxMaskedCycles);
  for (uint32_t i = 0; i < windowCount; ++i) {
    const SamplingWindow &window = windows[i];
    const int64_t startUs = poll_rules::timestampUs(window.anchorUs, window.anchorCycles,
                                                  edges[window.firstEvent].cycles, CYCLES_PER_US);
    const int64_t endUs = poll_rules::timestampUs(window.anchorUs, window.anchorCycles,
                                                window.endCycles, CYCLES_PER_US);
    const char *reason = window.endReason == 3 ? "edge_capacity" : (window.endReason == 2 ?
                        "duration_limit" : (window.endReason == 1 ? "trigger_tail" : "wait_limit"));
    Serial.printf("%s{\"first_event\":%lu,\"event_count\":%lu,\"start_us\":%lld,\"end_us\":%lld,"
                  "\"anchor_us\":%lld,\"anchor_cycles\":%lu,\"anchor_span_cycles\":%lu,"
                  "\"trigger_event\":%ld,\"trigger_cycles\":%lu,\"entry_gap_cycles\":%lu,"
                  "\"max_poll_gap_cycles\":%lu,\"masked_cycles\":%lu,\"poll_span_cycles\":%lu,"
                  "\"overflow\":%s,\"missing\":false,\"ended_reason\":\"%s\"}",
                  i ? "," : "", (unsigned long)window.firstEvent, (unsigned long)window.eventCount,
                  (long long)startUs, (long long)endUs, (long long)window.anchorUs,
                  (unsigned long)window.anchorCycles, (unsigned long)window.anchorSpanCycles,
                  (long)window.triggerEvent, (unsigned long)window.triggerCycles,
                  (unsigned long)window.entryGapCycles, (unsigned long)window.maxPollGapCycles,
                  (unsigned long)window.maskedCycles, (unsigned long)window.pollSpanCycles,
                  window.edgeOverflow ? "true" : "false", reason);
  }
  Serial.println("]}");
  Serial.write(reinterpret_cast<const uint8_t *>(edges), bytes);
  Serial.println();
  Serial.println("{\"type\":\"dump_end\"}");
  Serial.flush();
  resumeNetworking();
}

static void commandError(const char *message) {
  Serial.printf("{\"type\":\"error\",\"message\":\"%s\"}\n", message);
}

static uint32_t appCapacity() {
  const esp_partition_t *running = esp_ota_get_running_partition();
  const esp_partition_t *next = esp_ota_get_next_update_partition(nullptr);
  if (!running || !next || running->address == next->address || next->type != ESP_PARTITION_TYPE_APP)
    return 0;
  return next->size;
}

static void networkInfo() {
  const bool connected = !networkPaused && WiFi.status() == WL_CONNECTED;
  const String ip = connected ? WiFi.localIP().toString() : String("0.0.0.0");
  const esp_partition_t *running = esp_ota_get_running_partition();
  const esp_app_desc_t *description = esp_ota_get_app_description();
  char elfHash[65] = {};
  for (size_t i = 0; i < 32; ++i) snprintf(elfHash + i * 2, 3, "%02x", description->app_elf_sha256[i]);
  Serial.printf("{\"type\":\"netinfo\",\"firmware\":\"%s\",\"configured\":%s,"
                "\"connected\":%s,\"ip\":\"%s\",\"hostname\":\"%s\",\"ota_port\":%u,"
                "\"ota_enabled\":%s,\"network_paused\":%s,\"armed\":%s,\"update_busy\":%s,"
                "\"buffer_ready\":%s,\"capacity\":%lu,\"max_app_bytes\":%lu,"
                "\"free_heap\":%u,\"min_free_heap\":%u,\"largest_internal_block\":%u,"
                "\"recovery_required\":%s,\"running_label\":\"%s\",\"running_address\":%lu,\"app_elf_sha256\":\"%s\"}\n",
                REVISION, configured ? "true" : "false", connected ? "true" : "false",
                ip.c_str(), otaHostname, OTA_PORT, otaStarted ? "true" : "false",
                networkPaused ? "true" : "false", armed ? "true" : "false",
                updateBusy ? "true" : "false", edges ? "true" : "false",
                (unsigned long)CAPACITY, (unsigned long)appCapacity(),
                ESP.getFreeHeap(), ESP.getMinFreeHeap(),
                unsigned(heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT)),
                recoveryRequired ? "true" : "false", running ? running->label : "unknown",
                (unsigned long)(running ? running->address : 0), elfHash);
}

static bool pauseNetworking() {
  if (otaStarted) { ArduinoOTA.end(); otaStarted = false; }
  WiFi.disconnect(true, false);
  const bool disabled = WiFi.mode(WIFI_OFF) && WiFi.getMode() == WIFI_OFF;
  networkPaused = true;
  return disabled;
}

static void resumeNetworking() {
  if (!configured || armed || updateBusy || !networkPaused) return;
  WiFi.persistent(false);  // Our single application NVS blob owns provisioning.
  WiFi.setAutoReconnect(false);
  if (!WiFi.mode(WIFI_STA)) return;
  WiFi.setHostname(otaHostname);
  WiFi.setSleep(false);
  WiFi.begin(networkConfig.ssid, networkConfig.password);
  lastNetworkAttemptMs = millis();
  networkPaused = false;
}

static void serviceNetworking() {
  if (!configured || networkPaused || armed || updateBusy) return;
  if (WiFi.status() == WL_CONNECTED) {
    if (!otaStarted) {
      ArduinoOTA.begin();
      otaStarted = true;
    }
    // This is the only OTA service call; capture/update state gates it.
    ArduinoOTA.handle();
  } else {
    if (otaStarted) { ArduinoOTA.end(); otaStarted = false; }
    if (uint32_t(millis() - lastNetworkAttemptMs) >= 30000) {
      WiFi.begin(networkConfig.ssid, networkConfig.password);
      lastNetworkAttemptMs = millis();
    }
  }
}

static void configureOTA() {
  snprintf(otaHostname, sizeof(otaHostname), "sunburn-i2c-%06lx",
           (unsigned long)(ESP.getEfuseMac() & 0xffffffU));
  ArduinoOTA.setHostname(otaHostname).setPort(OTA_PORT).setRebootOnSuccess(true);
  ArduinoOTA.setTimeout(10000);
  ArduinoOTA.onStart([]() {
    stopCapture();
    updateBusy = true;
    if (ArduinoOTA.getCommand() != U_FLASH) {
      // Update.begin() has allocated staging buffers but has not written yet.
      Update.abort();
      Serial.println("{\"type\":\"ota_result\",\"ok\":false,\"message\":\"application_only\"}");
      return;
    }
    Serial.println("{\"type\":\"ota_start\",\"armed\":false}");
  });
  ArduinoOTA.onEnd([]() {
    Serial.println("{\"type\":\"ota_result\",\"ok\":true,\"reboot\":true}");
    Serial.flush();
  });
  ArduinoOTA.onError([](ota_error_t error) {
    stopCapture();
    Update.abort();
    updateBusy = false;
    Serial.printf("{\"type\":\"ota_result\",\"ok\":false,\"error\":%u}\n", unsigned(error));
  });
}

static void loadNetwork() {
  Preferences preferences;
  if (!preferences.begin("sb-i2c-ota", true)) return;
  const size_t size = preferences.getBytesLength("config");
  if (size == sizeof(networkConfig))
    preferences.getBytes("config", &networkConfig, sizeof(networkConfig));
  preferences.end();
  if (size != sizeof(networkConfig) || networkConfig.magic != 0x53424932U || networkConfig.version != 1 ||
      networkConfig.ssid[32] != 0 || networkConfig.password[64] != 0 || networkConfig.otaHash[32] != 0 ||
      networkConfig.ssid[0] == 0 || !control_rules::validHex(networkConfig.otaHash, 32) ||
      networkConfig.crc != crc32(reinterpret_cast<const uint8_t *>(&networkConfig), offsetof(NetworkConfig, crc))) {
    memset(&networkConfig, 0, sizeof(networkConfig));
    return;
  }
  configured = true;
  // setPasswordHash ignores later replacements in core 2.0.17; provisioning reboots.
  ArduinoOTA.setPasswordHash(networkConfig.otaHash);
}

static void provisionNetwork(char *arguments) {
  char *cursor = nullptr;
  char *ssidHex = strtok_r(arguments, " ", &cursor);
  char *passwordHex = strtok_r(nullptr, " ", &cursor);
  char *otaHash = strtok_r(nullptr, " ", &cursor);
  if (!ssidHex || !passwordHex || !otaHash || strtok_r(nullptr, " ", &cursor)) {
    commandError("WIFI_SET_requires_ssidhex_passwordhex_or_dash_otaMD5"); return;
  }
  NetworkConfig candidate = {};
  if (!control_rules::decodeTextHex(ssidHex, candidate.ssid, 32) || candidate.ssid[0] == 0 ||
      (strcmp(passwordHex, "-") != 0 && !control_rules::decodeTextHex(passwordHex, candidate.password, 64)) ||
      !control_rules::validHex(otaHash, 32)) {
    memset(&candidate, 0, sizeof(candidate));
    commandError("invalid_provisioning_fields"); return;
  }
  const size_t passwordLength = strlen(candidate.password);
  if ((passwordLength > 0 && passwordLength < 8) ||
      (passwordLength == 64 && !control_rules::validHex(candidate.password, 64))) {
    memset(&candidate, 0, sizeof(candidate));
    commandError("invalid_wifi_password_length"); return;
  }
  memcpy(candidate.otaHash, otaHash, 32);
  control_rules::lowerHex(candidate.otaHash);
  candidate.magic = 0x53424932U; candidate.version = 1;
  candidate.crc = crc32(reinterpret_cast<const uint8_t *>(&candidate), offsetof(NetworkConfig, crc));
  stopCapture();
  pauseNetworking();
  Preferences preferences;
  bool saved = preferences.begin("sb-i2c-ota", false);
  NetworkConfig readback = {};
  if (saved) {
    saved = preferences.putBytes("config", &candidate, sizeof(candidate)) == sizeof(candidate) &&
            preferences.getBytes("config", &readback, sizeof(readback)) == sizeof(readback) &&
            memcmp(&candidate, &readback, sizeof(candidate)) == 0;
    preferences.end();
  }
  memset(&candidate, 0, sizeof(candidate));
  memset(&readback, 0, sizeof(readback));
  if (!saved) { commandError("provisioning_save_failed"); resumeNetworking(); return; }
  Serial.println("{\"type\":\"wifi_saved\",\"ok\":true,\"reboot\":true}");
  Serial.flush(); delay(250); ESP.restart();
}

static void clearNetwork() {
  stopCapture(); pauseNetworking();
  Preferences preferences;
  bool cleared = preferences.begin("sb-i2c-ota", false);
  if (cleared) {
    cleared = preferences.clear() && preferences.getBytesLength("config") == 0;
    preferences.end();
  }
  if (!cleared) { commandError("provisioning_clear_failed"); resumeNetworking(); return; }
  Serial.println("{\"type\":\"wifi_cleared\",\"ok\":true,\"reboot\":true}");
  Serial.flush(); delay(250); ESP.restart();
}

static void failUsbUpdate(const char *message, uint32_t received) {
  Update.abort();
  updateBusy = false;
  recoveryRequired = true;
  snprintf(recoveryNonce, sizeof(recoveryNonce), "%08lx%08lx",
           (unsigned long)esp_random(), (unsigned long)esp_random());
  Serial.printf("{\"type\":\"update_result\",\"ok\":false,\"bytes\":%lu,"
                "\"message\":\"%s\",\"recovery_nonce\":\"%s\"}\n",
                (unsigned long)received, message, recoveryNonce);
  resumeNetworking();
}

static void usbUpdate(char *arguments) {
  char *cursor = nullptr;
  char *sizeText = strtok_r(arguments, " ", &cursor);
  char *hashText = strtok_r(nullptr, " ", &cursor);
  uint32_t expectedBytes = 0;
  if (!sizeText || !hashText || strtok_r(nullptr, " ", &cursor) ||
      !control_rules::decimalBounded(sizeText, 1024, appCapacity(), expectedBytes) ||
      !control_rules::validHex(hashText, 64)) {
    commandError("UPDATE_requires_bounded_app_size_and_SHA256"); return;
  }
  char expectedHash[65];
  memcpy(expectedHash, hashText, 65); control_rules::lowerHex(expectedHash);
  stopCapture();
  pauseNetworking();
  updateBusy = true;
  if (!Update.begin(expectedBytes, U_FLASH)) {
    updateBusy = false;
    Serial.println("{\"type\":\"update_result\",\"ok\":false,\"bytes\":0,\"message\":\"begin_failed\"}");
    resumeNetworking(); return;
  }
  mbedtls_sha256_context sha;
  mbedtls_sha256_init(&sha);
  if (mbedtls_sha256_starts_ret(&sha, 0) != 0) {
    mbedtls_sha256_free(&sha); failUsbUpdate("sha256_init_failed", 0); return;
  }
  Serial.printf("{\"type\":\"update_ready\",\"bytes\":%lu,\"sha256\":\"%s\","
                "\"idle_timeout_ms\":%lu,\"total_timeout_ms\":%lu,\"chunk_bytes\":1024}\n",
                (unsigned long)expectedBytes, expectedHash,
                (unsigned long)USB_IDLE_TIMEOUT_MS, (unsigned long)USB_TOTAL_TIMEOUT_MS);
  Serial.flush();
  uint8_t block[1024];
  uint32_t received = 0;
  const uint32_t started = millis();
  uint32_t lastData = started;
  while (received < expectedBytes) {
    size_t target = expectedBytes - received;
    if (target > sizeof(block)) target = sizeof(block);
    size_t collected = 0;
    // The host must wait for each cumulative ACK before sending the next chunk.
    while (collected < target) {
      if (uint32_t(millis() - lastData) >= USB_IDLE_TIMEOUT_MS ||
          uint32_t(millis() - started) >= USB_TOTAL_TIMEOUT_MS) {
        mbedtls_sha256_free(&sha); failUsbUpdate("receive_timeout", received + collected); return;
      }
      const int available = Serial.available();
      if (available <= 0) { delay(1); continue; }
      size_t amount = size_t(available);
      if (amount > target - collected) amount = target - collected;
      const size_t count = Serial.readBytes(block + collected, amount);
      if (!count) { delay(1); continue; }
      collected += count;
      lastData = millis();
      delay(1);
    }
    // Bootloader/merged/non-ESP images cannot be activated through the USB path.
    if (received == 0 && !control_rules::applicationPrefixValid(block, collected)) {
      mbedtls_sha256_free(&sha); failUsbUpdate("invalid_application_header", 0); return;
    }
    if (mbedtls_sha256_update_ret(&sha, block, collected) != 0 || Update.write(block, collected) != collected) {
      mbedtls_sha256_free(&sha); failUsbUpdate("hash_or_flash_write_failed", received); return;
    }
    received += collected;
    // This ACK grants the next <=1024 bytes only after flash/staging accepted this chunk.
    Serial.printf("{\"type\":\"update_progress\",\"bytes\":%lu}\n", (unsigned long)received);
    Serial.flush();
    delay(1);
  }
  uint8_t digest[32] = {};
  const int hashResult = mbedtls_sha256_finish_ret(&sha, digest);
  mbedtls_sha256_free(&sha);
  char actualHash[65];
  for (size_t i = 0; i < sizeof(digest); ++i) snprintf(actualHash + i * 2, 3, "%02x", digest[i]);
  if (hashResult != 0 || memcmp(expectedHash, actualHash, 64) != 0) {
    failUsbUpdate("sha256_mismatch", received); return;
  }
  // Update.end validates the image and selects the inactive slot only after SHA256.
  if (!Update.end(false) || !Update.isFinished()) {
    failUsbUpdate("image_finalize_failed", received); return;
  }
  Serial.printf("{\"type\":\"update_result\",\"ok\":true,\"bytes\":%lu,"
                "\"sha256\":\"%s\",\"reboot\":true}\n", (unsigned long)received, actualHash);
  Serial.flush(); delay(250); ESP.restart();
}

#include "bench_commands.h"
#include "pullup_control.h"
#include "emulator_commands.h"

static void processCommand(char *line) {
  if (strcmp(line, "INFO") == 0 || strcmp(line, "STATUS") == 0) { status(); return; }
  if (strcmp(line, "NETINFO") == 0) { networkInfo(); return; }
  if (recoveryRequired) {
    // Never interpret residual firmware bytes as ARM/provision/update commands.
    if (strncmp(line, "CONTROL_RESET ", 14) == 0 && strcmp(line + 14, recoveryNonce) == 0) {
      recoveryRequired = false;
      memset(recoveryNonce, 0, sizeof(recoveryNonce));
      Serial.println("{\"type\":\"control_reset\",\"ok\":true}");
    }
    return;
  }
  if (strcmp(line, "STOP") == 0) { stopCapture(); resumeNetworking(); status(); }
  else if (strcmp(line, "DUMP") == 0) dumpCapture();
  else if (strncmp(line, "ARM ", 4) == 0) {
    uint32_t ms = 0;
    if (!control_rules::decimalBounded(line + 4, 100, 120000, ms))
      commandError("ARM_duration_must_be_100_to_120000_ms");
    else if (updateBusy) commandError("update_busy");
    else startCapture(ms);
  } else if (strncmp(line, "WIFI_SET ", 9) == 0) provisionNetwork(line + 9);
  else if (strcmp(line, "WIFI_CLEAR") == 0) clearNetwork();
  else if (strncmp(line, "UPDATE ", 7) == 0) usbUpdate(line + 7);
  else commandError("unknown_command");
}

void setup() {
  // USB-powered default: GPIO19 must not source the printer while booting.
  if (!pullupOff()) updateBusy=true;
  setCpuFrequencyMhz(240);
  gpio_config_t pins = {};
  pins.pin_bit_mask = (1ULL << SDA_PIN) | (1ULL << SCL_PIN);
  pins.mode = GPIO_MODE_INPUT;
  pins.pull_up_en = GPIO_PULLUP_DISABLE;
  pins.pull_down_en = GPIO_PULLDOWN_DISABLE;
  pins.intr_type = GPIO_INTR_DISABLE;
  ESP_ERROR_CHECK(gpio_config(&pins));
  REG_WRITE(GPIO_FUNC21_OUT_SEL_CFG_REG,SIG_GPIO_OUT_IDX|GPIO_FUNC21_OEN_SEL);
  REG_WRITE(GPIO_FUNC22_OUT_SEL_CFG_REG,SIG_GPIO_OUT_IDX|GPIO_FUNC22_OEN_SEL);
  REG_WRITE(GPIO_ENABLE_W1TC_REG,BUS_MASK);
  // GPIO interrupts stay disabled; capture uses bounded direct register reads.
  edges = static_cast<Edge *>(heap_caps_malloc(CAPACITY * sizeof(Edge), MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
  Serial.setRxBufferSize(4096);
  Serial.begin(BAUD);
  Serial.setTimeout(20);
  WiFi.persistent(false);
  WiFi.mode(WIFI_OFF);
  configureOTA();
  loadNetwork();
  emuLoadStored();
  emuInitBreadcrumbs();
  resumeNetworking();
  delay(100);
  status();
}

void loop() {
  if (!emulationSessionOpen && !pullupConfiguredOn &&
      ((armed && esp_timer_get_time() - captureStartUs >= int64_t(durationMs) * 1000)
      || (!armed && used > 0 && captureEndUs == 0))) {
    stopCapture();
    resumeNetworking();
  }
  while (Serial.available()) {
    const char c = static_cast<char>(Serial.read());
    if (c == '\n') {
      command[commandLength] = 0;
      if (!commandTooLong) {
        if (!processEmuCommand(command) && !processBenchCommand(command)) processCommand(command);
      }
      else if (!recoveryRequired) commandError("command_too_long");
      // Provisioning material is never echoed and is cleared after processing.
      memset(command, 0, sizeof(command));
      commandLength = 0;
      commandTooLong = false;
    } else if (c != '\r') {
      if (static_cast<uint8_t>(c) < 32 || static_cast<uint8_t>(c) > 126) commandTooLong = true;
      if (!commandTooLong && commandLength + 1 < sizeof(command)) command[commandLength++] = c;
      else commandTooLong = true;
    }
  }
  if (armed) sampleOneWindow();
  if (!armed && !updateBusy && !emulationSessionOpen && !pullupConfiguredOn) { resumeNetworking(); serviceNetworking(); }
  // Restore interrupts before every yield, including empty wait slices.
  delay(1);
}
