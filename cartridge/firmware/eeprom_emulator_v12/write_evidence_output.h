#pragma once
static void emuWriteRecord(const volatile EmuWriteRecord &r) {
  Serial.printf("{\"valid\":true,\"committed\":%s,\"fault_code\":%lu,\"generation_before\":%lu,\"generation_after\":%lu,"
    "\"pointer\":%lu,\"page_base\":%lu,\"page_mask\":%lu,\"data_bytes\":%lu,\"rx_bytes\":%lu,"
    "\"rx_ack_bytes\":%lu,\"rx_bad_ack_samples\":%lu,\"irq_flags\":%lu,\"hw_status\":%lu,\"last_rx_hw_status\":%lu,\"page_hex\":\"",
    r.committed?"true":"false",(unsigned long)r.faultCode,(unsigned long)r.generationBefore,(unsigned long)r.generationAfter,
    (unsigned long)r.pointer,(unsigned long)r.pageBase,(unsigned long)r.pageMask,(unsigned long)r.dataBytes,
    (unsigned long)r.rxBytes,(unsigned long)r.rxAckBytes,(unsigned long)r.rxBadSamples,
    (unsigned long)r.irqFlags,(unsigned long)r.hwStatus,(unsigned long)r.lastRxStatus);
  for (unsigned i=0;i<16;++i) Serial.printf("%02x",unsigned(r.page[i]));
  Serial.print("\",\"payload_hex\":\"");
  if (r.dataBytes<=16) for (unsigned i=0;i<r.dataBytes;++i) Serial.printf("%02x",unsigned(r.page[(r.pointer+i)&15]));
  Serial.printf("\",\"payload_truncated\":%s}",r.dataBytes>16?"true":"false");
}
static void emuWriteEvidenceState() {
  const bool available=!emulationActive;
  Serial.printf(",\"write_evidence\":{\"available\":%s,\"pending_rejected\":",available?"true":"false");
  if (available && emuRejectedWrite.valid) emuWriteRecord(emuRejectedWrite); else Serial.print("null");
  Serial.print(",\"committed\":[");
  if (available) {
    const uint32_t count=emuWriteCount,first=(emuWriteNext+4-count)&3;
    for (uint32_t i=0;i<count;++i) {
      if (i) Serial.print(",");
      emuWriteRecord(emuWriteHistory[(first+i)&3]);
    }
  }
  Serial.print("]}");
}
