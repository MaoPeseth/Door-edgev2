/*
 * rfid_reader.h — MFRC522 RFID Reader Module
 * 
 * Handles RFID card reading and UID extraction.
 * Returns card UID as hex string.
 */
#ifndef RFID_READER_H
#define RFID_READER_H

#include <SPI.h>
#include <MFRC522.h>
#include "config.h"

class RFIDReader {
public:
    RFIDReader() : _rfid(RFID_SS_PIN, RFID_RST_PIN), _lastCardUID("") {}

    void begin() {
        SPI.begin(RFID_SCK_PIN, RFID_MISO_PIN, RFID_MOSI_PIN, RFID_SS_PIN);
        _rfid.PCD_Init();
        delay(100);
        
        Serial.print("[RFID] Reader initialized — UID: ");
        Serial.print(_rfid.PCD_ReadRegister(_rfid.VersionReg), HEX);
        Serial.println();
    }

    /**
     * Check if a new card is present.
     * Returns true if card detected.
     */
    bool isCardPresent() {
        return _rfid.PICC_IsNewCardPresent() && _rfid.PICC_ReadCardSerial();
    }

    /**
     * Get card UID as hex string.
     * Returns empty string if no card.
     */
    String getCardUID() {
        String uid = "";
        
        for (byte i = 0; i < _rfid.uid.size; i++) {
            if (_rfid.uid.uidByte[i] < 0x10) {
                uid += "0";
            }
            uid += String(_rfid.uid.uidByte[i], HEX);
        }
        
        uid.toUpperCase();
        return uid;
    }

    /**
     * Read card and return UID.
     * Call this in loop to check for new cards.
     * Returns empty string if no new card.
     */
    String readCard() {
        if (isCardPresent()) {
            String uid = getCardUID();
            
            // Only return if different from last card (debounce)
            if (uid != _lastCardUID) {
                _lastCardUID = uid;
                Serial.print("[RFID] Card detected: ");
                Serial.println(uid);
                return uid;
            }
        } else {
            // Card removed, reset last UID
            _lastCardUID = "";
        }
        
        return "";
    }

    /**
     * Stop card communication.
     */
    void haltCard() {
        _rfid.PICC_HaltA();
        _rfid.PCD_StopCrypto1();
    }

private:
    MFRC522 _rfid;
    String _lastCardUID;
};

#endif // RFID_READER_H
