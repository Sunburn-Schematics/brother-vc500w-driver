#include "../firmware/ota_i2c/control_rules.h"
#include <cassert>
#include <cstdio>
#include <string>
#include <vector>
int main() {
  using namespace control_rules;
  uint32_t value = 77;
  const char *invalid[] = {"", "-1", "+1024", " 1024", "1024 ", "0x1000", "1024x", "4294967296", "999999999999999999999"};
  for (const char *text : invalid) { assert(!decimalBounded(text, 1024, 0x140000, value)); assert(value == 77); }
  assert(!decimalBounded("1023", 1024, 0x140000, value));
  assert(!decimalBounded("1310721", 1024, 0x140000, value));
  assert(decimalBounded("1024", 1024, 0x140000, value) && value == 1024);
  assert(decimalBounded("1310720", 1024, 0x140000, value) && value == 0x140000);
  assert(decimalBounded("4294967295", 0, UINT32_MAX, value) && value == UINT32_MAX);
  assert(!decimalBounded("1024", 1024, 0, value));
  assert(validHex("aAbBcCdDeEfF0123", 16));
  assert(!validHex("aabbccddeeff012z", 16));
  assert(!validHex("aabbccddeeff0123 ", 16));
  assert(!validHex("00", 32));
  char decoded[65] = {};
  assert(decodeTextHex("4578616d706c65204e6574776f726b", decoded, 32));
  assert(std::string(decoded) == "Example Network");
  assert(decodeTextHex("", decoded, 64) && decoded[0] == 0);
  assert(!decodeTextHex("00", decoded, 64));
  assert(!decodeTextHex("610062", decoded, 64));
  assert(!decodeTextHex("6", decoded, 64));
  assert(!decodeTextHex("GG", decoded, 64));
  assert(decodeTextHex(std::string(64, '1').c_str(), decoded, 32) && strlen(decoded) == 32);
  assert(!decodeTextHex(std::string(66, '1').c_str(), decoded, 32));
  assert(decodeTextHex(std::string(128, 'a').c_str(), decoded, 64) && strlen(decoded) == 64);
  assert(!decodeTextHex(std::string(130, 'a').c_str(), decoded, 64));
  char hash[] = "AB01cDef"; lowerHex(hash); assert(std::string(hash) == "ab01cdef");
  uint8_t prefix[36] = {}; prefix[0] = 0xe9;
  assert(!applicationPrefixValid(prefix, 36)); // A bootloader is not an app image.
  prefix[32] = 0x32; prefix[33] = 0x54; prefix[34] = 0xcd; prefix[35] = 0xab;
  assert(applicationPrefixValid(prefix, 36));
  assert(!applicationPrefixValid(prefix, 35));
  prefix[0] = 0; assert(!applicationPrefixValid(prefix, 36));
  std::puts("PASS: bounded length parsing, overflow/sign/junk rejection, provisioning hex/NUL/length validation, application-versus-bootloader guard");
}
