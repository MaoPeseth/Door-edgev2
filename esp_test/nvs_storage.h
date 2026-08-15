/*
 * nvs_storage.h — NVS Storage Module for Card UID Mapping
 * 
 * Stores card_uid → person mapping in ESP32's Non-Volatile Storage.
 * Persists across reboots.
 */
#ifndef NVS_STORAGE_H
#define NVS_STORAGE_H

#include <Preferences.h>
#include "config.h"

// Card data structure
struct CardInfo {
    char cardUID[CARD_UID_MAX_LEN];
    char personID[32];
    char name[64];
};

class NVSStorage {
public:
    NVSStorage() : _cardCount(0) {}

    void begin() {
        _preferences.begin(NVS_NAMESPACE, false);  // Read/Write
        _cardCount = _preferences.getUInt("count", 0);
        Serial.print("[NVS] Loaded ");
        Serial.print(_cardCount);
        Serial.println(" cards from storage");
    }

    void end() {
        _preferences.end();
    }

    /**
     * Add or update a card mapping.
     */
    bool addCard(const String& cardUID, const String& personID, const String& name) {
        // Check if card already exists
        for (int i = 0; i < _cardCount; i++) {
            String existingUID = _preferences.getString(("uid_" + String(i)).c_str(), "");
            if (existingUID == cardUID) {
                // Update existing card
                _preferences.putString(("pid_" + String(i)).c_str(), personID);
                _preferences.putString(("name_" + String(i)).c_str(), name);
                Serial.print("[NVS] Updated card: ");
                Serial.println(cardUID);
                return true;
            }
        }

        // Add new card (if space available)
        if (_cardCount >= NVS_MAX_CARDS) {
            Serial.println("[NVS] Storage full!");
            return false;
        }

        int idx = _cardCount;
        _preferences.putString(("uid_" + String(idx)).c_str(), cardUID);
        _preferences.putString(("pid_" + String(idx)).c_str(), personID);
        _preferences.putString(("name_" + String(idx)).c_str(), name);
        _cardCount++;
        _preferences.putUInt("count", _cardCount);

        Serial.print("[NVS] Added card: ");
        Serial.print(cardUID);
        Serial.print(" → ");
        Serial.println(personID);
        return true;
    }

    /**
     * Remove a card by UID.
     */
    bool removeCard(const String& cardUID) {
        for (int i = 0; i < _cardCount; i++) {
            String existingUID = _preferences.getString(("uid_" + String(i)).c_str(), "");
            if (existingUID == cardUID) {
                // Shift remaining cards down
                for (int j = i; j < _cardCount - 1; j++) {
                    String nextUID = _preferences.getString(("uid_" + String(j + 1)).c_str(), "");
                    String nextPID = _preferences.getString(("pid_" + String(j + 1)).c_str(), "");
                    String nextName = _preferences.getString(("name_" + String(j + 1)).c_str(), "");

                    _preferences.putString(("uid_" + String(j)).c_str(), nextUID);
                    _preferences.putString(("pid_" + String(j)).c_str(), nextPID);
                    _preferences.putString(("name_" + String(j)).c_str(), nextName);
                }

                // Remove last entry
                _preferences.remove(("uid_" + String(_cardCount - 1)).c_str());
                _preferences.remove(("pid_" + String(_cardCount - 1)).c_str());
                _preferences.remove(("name_" + String(_cardCount - 1)).c_str());
                _cardCount--;
                _preferences.putUInt("count", _cardCount);

                Serial.print("[NVS] Removed card: ");
                Serial.println(cardUID);
                return true;
            }
        }
        return false;
    }

    /**
     * Clear all cards.
     */
    void clearAll() {
        for (int i = 0; i < _cardCount; i++) {
            _preferences.remove(("uid_" + String(i)).c_str());
            _preferences.remove(("pid_" + String(i)).c_str());
            _preferences.remove(("name_" + String(i)).c_str());
        }
        _cardCount = 0;
        _preferences.putUInt("count", 0);
        Serial.println("[NVS] Cleared all cards");
    }

    /**
     * Look up person by card UID.
     * Returns true if found, fills in personID and name.
     */
    bool lookupCard(const String& cardUID, String& personID, String& name) {
        for (int i = 0; i < _cardCount; i++) {
            String existingUID = _preferences.getString(("uid_" + String(i)).c_str(), "");
            if (existingUID == cardUID) {
                personID = _preferences.getString(("pid_" + String(i)).c_str(), "");
                name = _preferences.getString(("name_" + String(i)).c_str(), "");
                return true;
            }
        }
        return false;
    }

    /**
     * Get card count.
     */
    int getCardCount() const {
        return _cardCount;
    }

    /**
     * Print all cards (for debugging).
     */
    void printAllCards() {
        Serial.println("[NVS] Card list:");
        for (int i = 0; i < _cardCount; i++) {
            String uid = _preferences.getString(("uid_" + String(i)).c_str(), "");
            String pid = _preferences.getString(("pid_" + String(i)).c_str(), "");
            String name = _preferences.getString(("name_" + String(i)).c_str(), "");
            Serial.print("  [");
            Serial.print(i);
            Serial.print("] ");
            Serial.print(uid);
            Serial.print(" → ");
            Serial.print(pid);
            Serial.print(" (");
            Serial.print(name);
            Serial.println(")");
        }
    }

private:
    Preferences _preferences;
    int _cardCount;
};

#endif // NVS_STORAGE_H
