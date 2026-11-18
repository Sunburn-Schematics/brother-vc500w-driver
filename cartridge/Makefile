PYTHON ?= python3
SKETCH ?= eeprom_emulator_v12
ARDUINO_CLI ?=

.PHONY: bootstrap firmware test test-host test-native verify-sources

bootstrap:
	$(PYTHON) tools/build_firmware.py --install $(if $(ARDUINO_CLI),--arduino-cli "$(ARDUINO_CLI)",)

firmware:
	$(PYTHON) tools/build_firmware.py --sketch "$(SKETCH)" $(if $(ARDUINO_CLI),--arduino-cli "$(ARDUINO_CLI)",)

test: test-host test-native verify-sources

test-host:
	$(PYTHON) tools/run_tests.py

test-native:
	$(PYTHON) tools/run_native_tests.py

verify-sources:
	$(PYTHON) tools/verify_firmware_sources.py
