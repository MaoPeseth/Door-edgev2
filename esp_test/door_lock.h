/*
 * door_lock.h — Door Lock Control Module
 * 
 * Controls door lock via relay.
 * Handles unlock timing and status.
 */
#ifndef DOOR_LOCK_H
#define DOOR_LOCK_H

#include <Arduino.h>
#include "config.h"

class DoorLock {
public:
    // Set by the main sketch when an access event still needs to be
    // reported to Edge (e.g. face unlock) — the sketch reads + clears it.
    bool publishAccessLog = false;

    DoorLock() : _unlocked(false), _unlockTime(0) {}

    void begin() {
        pinMode(DOOR_RELAY_PIN, OUTPUT);
        pinMode(LED_GREEN_PIN, OUTPUT);
        pinMode(LED_RED_PIN, OUTPUT);
        pinMode(BUZZER_PIN, OUTPUT);

        // Lock door initially
        lock();
        
        Serial.println("[Door] Lock initialized — locked");
    }

    /**
     * Unlock the door.
     * Automatically locks after DOOR_LOCK_TIME milliseconds.
     */
    void unlock(const char* personID = nullptr, const char* name = nullptr) {
        if (name) {
            Serial.print("[Door] UNLOCK: ");
            Serial.print(name);
            Serial.print(" (");
            Serial.print(personID);
            Serial.println(")");
        }

        digitalWrite(DOOR_RELAY_PIN, HIGH);    // Activate relay
        digitalWrite(LED_GREEN_PIN, HIGH);      // Green LED on
        digitalWrite(LED_RED_PIN, LOW);         // Red LED off
        
        // Short beep for success
        digitalWrite(BUZZER_PIN, HIGH);
        delay(100);
        digitalWrite(BUZZER_PIN, LOW);

        _unlocked = true;
        _unlockTime = millis();
    }

    /**
     * Lock the door immediately.
     */
    void lock() {
        digitalWrite(DOOR_RELAY_PIN, LOW);     // Deactivate relay
        digitalWrite(LED_GREEN_PIN, LOW);       // Green LED off
        
        _unlocked = false;
        _unlockTime = 0;
        
        Serial.println("[Door] Locked");
    }

    /**
     * Deny access (flash red LED).
     */
    void deny() {
        digitalWrite(LED_RED_PIN, HIGH);        // Red LED on
        
        // Double beep for denial
        for (int i = 0; i < 2; i++) {
            digitalWrite(BUZZER_PIN, HIGH);
            delay(100);
            digitalWrite(BUZZER_PIN, LOW);
            delay(100);
        }
        
        delay(500);
        digitalWrite(LED_RED_PIN, LOW);         // Red LED off
        
        Serial.println("[Door] Access denied");
    }

    /**
     * Update door state (call in loop).
     * Handles auto-lock after timeout.
     */
    void update() {
        if (_unlocked && (millis() - _unlockTime >= DOOR_LOCK_TIME)) {
            lock();
        }
    }

    /**
     * Check if door is currently unlocked.
     */
    bool isUnlocked() const {
        return _unlocked;
    }

    /**
     * Get time remaining until auto-lock (ms).
     */
    unsigned long getTimeRemaining() const {
        if (!_unlocked) return 0;
        unsigned long elapsed = millis() - _unlockTime;
        if (elapsed >= DOOR_LOCK_TIME) return 0;
        return DOOR_LOCK_TIME - elapsed;
    }

private:
    bool _unlocked;
    unsigned long _unlockTime;
};

#endif // DOOR_LOCK_H
