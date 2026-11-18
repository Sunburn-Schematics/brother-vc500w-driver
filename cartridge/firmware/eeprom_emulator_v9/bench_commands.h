#pragma once
#include "bench_rules.h"
#include <soc/io_mux_reg.h>

// Included after the frozen management functions. Only explicit BENCH commands
// can enter this synchronous isolated-bus path; ArduinoOTA.handle is not called
// until cleanup has restored input-only pins and this command has returned.
class BenchPins {
 public:
  ~BenchPins() { releaseInputs(); }
  bool activate(bool weak) {
    // Latches are released before enabling open-drain outputs. Never push high.
    if (gpio_set_level(SDA_PIN, 1) != ESP_OK || gpio_set_level(SCL_PIN, 1) != ESP_OK) return false;
    gpio_config_t config = {};
    config.pin_bit_mask = (1ULL << SDA_PIN) | (1ULL << SCL_PIN);
    config.mode = GPIO_MODE_INPUT_OUTPUT_OD;
    config.pull_up_en = weak ? GPIO_PULLUP_ENABLE : GPIO_PULLUP_DISABLE;
    config.pull_down_en = GPIO_PULLDOWN_DISABLE;
    config.intr_type = GPIO_INTR_DISABLE;
    return gpio_config(&config) == ESP_OK;
  }
  bool releaseInputs() {
    gpio_config_t config = {};
    config.pin_bit_mask = (1ULL << SDA_PIN) | (1ULL << SCL_PIN);
    config.mode = GPIO_MODE_INPUT;
    config.pull_up_en = GPIO_PULLUP_DISABLE;
    config.pull_down_en = GPIO_PULLDOWN_DISABLE;
    config.intr_type = GPIO_INTR_DISABLE;
    return gpio_config(&config) == ESP_OK && inputOnly() && pullsOff();
  }
  bool inputOnly() const { return benchPinsInputOnly(); }
  bool pullsOff() const {
    return benchPinsPullsOff();
  }
  uint64_t nowUs() const { return uint64_t(esp_timer_get_time()); }
  void lowSda() { gpio_set_level(SDA_PIN, 0); }
  void lowScl() { gpio_set_level(SCL_PIN, 0); }
  void releaseSda() { gpio_set_level(SDA_PIN, 1); }
  void releaseScl() { gpio_set_level(SCL_PIN, 1); }
  bool sda() const { return gpio_get_level(SDA_PIN) != 0; }
  bool scl() const { return gpio_get_level(SCL_PIN) != 0; }
  void waitUs(uint32_t us) { delayMicroseconds(us); }
  void yield() { delay(1); }
};

static bool benchHash(const uint8_t *image, char *hex) {
  uint8_t digest[32] = {};
  if (mbedtls_sha256_ret(image, bench_rules::IMAGE_BYTES, digest, 0) != 0) return false;
  for (size_t i = 0; i < sizeof(digest); ++i) snprintf(hex + i * 2, 3, "%02x", digest[i]);
  return true;
}

static void benchPhase(const char *phase) {
  Serial.printf("{\"type\":\"bench_phase\",\"phase\":\"%s\"}\n", phase);
}

static void benchImage(const char *phase, const uint8_t *image, const char *hash) {
  const bench_rules::Crcs crc = bench_rules::check(image);
  Serial.printf("{\"type\":\"bench_image\",\"phase\":\"%s\",\"bytes\":256,\"hex\":\"", phase);
  for (size_t i = 0; i < bench_rules::IMAGE_BYTES; ++i) Serial.printf("%02x", image[i]);
  Serial.printf("\",\"sha256\":\"%s\",\"crc_r0\":%s,\"crc_rw1\":%s,\"crc_rw2\":%s}\n",
                hash, crc.r0 ? "true" : "false", crc.rw1 ? "true" : "false", crc.rw2 ? "true" : "false");
}

static void benchPlan(const uint8_t *before, const uint8_t *after, const char *preHash, const char *expectedHash) {
  Serial.printf("{\"type\":\"bench_plan\",\"preimage_sha256\":\"%s\",\"expected_sha256\":\"%s\","
                "\"initial_u16\":%u,\"old_remaining_u16\":%u,\"new_remaining_u16\":%u,\"changed_offsets\":[",
                preHash, expectedHash, unsigned(bench_rules::u16le(before + 0x60)),
                unsigned(bench_rules::u16le(before + 0xe8)), unsigned(bench_rules::u16le(after + 0xe8)));
  bool first = true;
  for (size_t i = 0; i < bench_rules::IMAGE_BYTES; ++i) if (before[i] != after[i]) {
    Serial.printf("%s%u", first ? "" : ",", unsigned(i)); first = false;
  }
  Serial.println("]}");
}

static void runBench(bool restore, bool weak, const char *requiredHash) {
  const char *op = restore ? "restore" : "read";
  const char *phase = "prepare";
  const char *mutation = "none";
  const char *message = "unknown_failure";
  char preHash[65] = {}, expectedHash[65] = {}, postHash[65] = {}, otherHash[65] = {};
  uint8_t before[256] = {}, second[256] = {}, expected[256] = {}, after[256] = {}, last[256] = {};
  bool identical = false, crcsValid = false;
  BenchPins pins;
  Serial.printf("{\"type\":\"bench_start\",\"op\":\"%s\",\"pullups\":\"%s\","
                "\"firmware\":\"%s\",\"nominal_max_clock_hz\":5000,\"total_timeout_ms\":8000}\n",
                op, weak ? "WEAK" : "NONE", REVISION);
  bool ok = [&]() {
    if (!pauseNetworking()) { message = "network_suspend_failed"; return false; }
    if (!pins.activate(weak)) { message = "gpio_activation_failed"; return false; }
    bench_rules::Master<BenchPins> master(pins);
    auto readImage = [&](const char *readPhase, uint8_t *image, char *hash) {
      phase = readPhase; benchPhase(phase);
      if (!master.read(0, image, 256)) { message = master.error(); return false; }
      if (!benchHash(image, hash)) { message = "sha256_failed"; return false; }
      benchImage(phase, image, hash);
      return !master.expired();
    };
    if (!readImage("pre_read_1", before, preHash) || !readImage("pre_read_2", second, otherHash)) {
      if (!master.error() && strcmp(message, "unknown_failure") == 0) message = "read_failed";
      else if (master.error()) message = master.error();
      return false;
    }
    phase = "precheck"; benchPhase(phase);
    identical = memcmp(before, second, 256) == 0;
    crcsValid = bench_rules::check(before).valid() && bench_rules::check(second).valid();
    if (!identical) { message = "independent_reads_differ"; return false; }
    if (!crcsValid) { message = "preimage_crc_invalid"; return false; }
    if (!restore) { phase = "complete"; message = "qualified_read_complete"; return true; }
    if (strcmp(preHash, requiredHash) != 0) { message = "preimage_sha256_mismatch"; return false; }
    const char *planError = bench_rules::restoreImage(before, expected);
    if (planError) { message = planError; return false; }
    if (!benchHash(expected, expectedHash)) { message = "sha256_failed"; return false; }
    benchPlan(before, expected, preHash, expectedHash);
    if (memcmp(before, expected, 256) != 0) {
      phase = "counter_write"; benchPhase(phase); mutation = "possible_partial";
      if (!master.writePage(0xe8, expected + 0xe8, 2)) { message = master.error(); return false; }
      phase = "counter_ack_poll"; benchPhase(phase);
      if (!master.ackPoll()) { message = master.error(); return false; }
      phase = "counter_verify"; benchPhase(phase);
      uint8_t counter[2] = {};
      if (!master.read(0xe8, counter, 2)) { message = master.error(); return false; }
      if (memcmp(counter, expected + 0xe8, 2) != 0) { message = "counter_readback_mismatch"; return false; }
      mutation = "counter_verified_crc_pending";
      phase = "crc_write"; benchPhase(phase); mutation = "possible_partial";
      if (!master.writePage(0xf6, expected + 0xf6, 2)) { message = master.error(); return false; }
      phase = "crc_ack_poll"; benchPhase(phase);
      if (!master.ackPoll()) { message = master.error(); return false; }
    } else mutation = "unchanged";
    if (!readImage("post_read_1", after, postHash) || !readImage("post_read_2", last, otherHash)) {
      if (master.error()) message = master.error();
      return false;
    }
    if (memcmp(after, expected, 256) != 0 || memcmp(last, expected, 256) != 0 ||
        strcmp(postHash, expectedHash) != 0 || strcmp(otherHash, expectedHash) != 0 ||
        !bench_rules::check(after).valid() || !bench_rules::check(last).valid()) {
      message = "postimage_exact_verification_failed"; return false;
    }
    if (strcmp(mutation, "unchanged") != 0) mutation = "verified";
    phase = "complete"; message = "restore_verified"; return true;
  }();
  const bool released = pins.releaseInputs();
  if (!released) { ok = false; message = "gpio_release_verification_failed"; updateBusy = true; }
  Serial.printf("{\"type\":\"bench_result\",\"op\":\"%s\",\"ok\":%s,\"phase\":\"%s\","
                "\"mutation_state\":\"%s\",\"preimage_sha256\":\"%s\",\"expected_sha256\":\"%s\","
                "\"postimage_sha256\":\"%s\",\"reads_identical\":%s,\"crcs_valid\":%s,"
                "\"pins_input_only\":%s,\"internal_pulls\":%s,\"message\":\"%s\"}\n",
                op, ok ? "true" : "false", phase, mutation, preHash, expectedHash, postHash,
                identical ? "true" : "false", crcsValid ? "true" : "false", pins.inputOnly() ? "true" : "false",
                pins.pullsOff() ? "false" : "true", message ? message : "transport_failed");
  Serial.flush();
  if (released) resumeNetworking();
}

static bool processBenchCommand(char *line) {
  if (strncmp(line, "BENCH_", 6) != 0) return false;
  if (recoveryRequired || updateBusy || armed) { commandError("bench_requires_idle_control_state"); return true; }
  char *saved = nullptr;
  char *verb = strtok_r(line, " ", &saved);
  char *isolation = strtok_r(nullptr, " ", &saved);
  char *pullups = strtok_r(nullptr, " ", &saved);
  char *hash = strtok_r(nullptr, " ", &saved);
  char *extra = strtok_r(nullptr, " ", &saved);
  const bool restore = strcmp(verb, "BENCH_RESTORE") == 0;
  if ((!restore && strcmp(verb, "BENCH_READ") != 0) || !isolation || strcmp(isolation, "ISOLATED") != 0 ||
      !pullups || (strcmp(pullups, "NONE") != 0 && strcmp(pullups, "WEAK") != 0) || extra ||
      (restore ? (!hash || !control_rules::validHex(hash, 64)) : hash != nullptr)) {
    commandError("BENCH_requires_ISOLATED_NONE_or_WEAK_and_restore_preimage_SHA256"); return true;
  }
  if (hash) control_rules::lowerHex(hash);
  runBench(restore, strcmp(pullups, "WEAK") == 0, hash);
  return true;
}
