#pragma once
#include <stddef.h>
#include <stdint.h>
#include <string.h>

namespace control_rules {
inline int hexDigit(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  return -1;
}
inline bool validHex(const char *text, size_t digits) {
  if (strlen(text) != digits) return false;
  for (size_t i = 0; i < digits; ++i) if (hexDigit(text[i]) < 0) return false;
  return true;
}
inline bool decodeTextHex(const char *text, char *out, size_t maxBytes) {
  const size_t digits = strlen(text);
  if (digits % 2 || digits > maxBytes * 2) return false;
  for (size_t i = 0; i < digits / 2; ++i) {
    const int high = hexDigit(text[i * 2]), low = hexDigit(text[i * 2 + 1]);
    if (high < 0 || low < 0 || (high == 0 && low == 0)) return false;
    out[i] = char((high << 4) | low);
  }
  out[digits / 2] = 0;
  return true;
}
inline bool decimalBounded(const char *text, uint32_t minimum, uint32_t maximum, uint32_t &value) {
  if (!*text) return false;
  uint32_t candidate = 0;
  for (const char *p = text; *p; ++p) {
    if (*p < '0' || *p > '9') return false;
    const uint32_t digit = uint32_t(*p - '0');
    if (candidate > UINT32_MAX / 10 ||
        (candidate == UINT32_MAX / 10 && digit > UINT32_MAX % 10)) return false;
    candidate = candidate * 10 + digit;
  }
  if (candidate < minimum || candidate > maximum) return false;
  value = candidate;
  return true;
}
inline bool applicationPrefixValid(const uint8_t *bytes, size_t length) {
  if (length < 36 || bytes[0] != 0xe9) return false;
  const uint32_t magic = uint32_t(bytes[32]) | (uint32_t(bytes[33]) << 8) |
                         (uint32_t(bytes[34]) << 16) | (uint32_t(bytes[35]) << 24);
  return magic == 0xabcd5432U;  // ESP_APP_DESC_MAGIC_WORD in pinned ESP-IDF 4.4.7.
}
inline void lowerHex(char *text) {
  for (; *text; ++text) if (*text >= 'A' && *text <= 'F') *text += 'a' - 'A';
}
}  // namespace control_rules
