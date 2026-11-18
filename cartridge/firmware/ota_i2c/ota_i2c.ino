// Brother cartridge investigation: passive GPIO capture, classic ESP32 only.
// GPIO21=SDA, GPIO22=SCL, shared GND. Printer VCC MUST remain disconnected.
// No I2C peripheral, output pin, internal pull, cartridge write or emulation.
// Networking is available only while capture is disarmed; credentials arrive over USB.
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
static constexpr char REVISION[] = "i2c-passive-edge-v2";

struct Edge { uint32_t timestamp_us; uint32_t cycles; uint32_t levels; };
static_assert(sizeof(Edge) == 12, "Host record format is three little-endian u32s");
// Allocate once from internal RAM, never PSRAM or from inside an ISR.
DRAM_ATTR static Edge *edges = nullptr;
DRAM_ATTR static volatile uint32_t used = 0;
DRAM_ATTR static volatile uint32_t overflow = 0;
DRAM_ATTR static volatile uint32_t callbacks = 0;
DRAM_ATTR static volatile uint32_t lastLevels = 3;
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
                "\"pins_input_only\":true,\"internal_pulls\":false,\"cpu_mhz\":%u,"
                "\"buffer_ready\":%s,\"update_busy\":%s,\"recovery_required\":%s}\n",
                REVISION, active ? "true" : "false", (unsigned long)count,
                (unsigned long)drops, (unsigned long)calls, (unsigned long)levels,
                getCpuFrequencyMhz(), edges ? "true" : "false",
                updateBusy ? "true" : "false", recoveryRequired ? "true" : "false");
}

static void startCapture(uint32_t milliseconds) {
  stopCapture();
  if (!edges || !pauseNetworking()) {
    Serial.println("{\"type\":\"error\",\"message\":\"capture_buffer_or_network_suspend_failed\"}");
    return;
  }
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
  if (!edges) { Serial.println("{\"type\":\"error\",\"message\":\"capture_buffer_unavailable\"}"); return; }
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
  edges = static_cast<Edge *>(heap_caps_malloc(CAPACITY * sizeof(Edge), MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
  Serial.setRxBufferSize(4096);
  Serial.begin(BAUD);
  Serial.setTimeout(20);
  WiFi.persistent(false);
  WiFi.mode(WIFI_OFF);
  configureOTA();
  loadNetwork();
  resumeNetworking();
  delay(100);
  status();
}

void loop() {
  if ((armed && esp_timer_get_time() - captureStartUs >= int64_t(durationMs) * 1000)
      || (!armed && used > 0 && captureEndUs == 0)) {
    stopCapture();
    resumeNetworking();
  }
  while (Serial.available()) {
    const char c = static_cast<char>(Serial.read());
    if (c == '\n') {
      command[commandLength] = 0;
      if (!commandTooLong) processCommand(command);
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
  if (!armed && !updateBusy) serviceNetworking();
  delay(1);
}
