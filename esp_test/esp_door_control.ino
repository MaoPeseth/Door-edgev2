/*
 * esp_door_control.ino — ESP32 Door Control Main Firmware
 * 
 * Features:
 * - RFID card reading (MFRC522)
 * - Local card_uid → person matching (NVS storage)
 * - MQTT communication with Edge PC
 * - Face unlock via MQTT (from Edge)
 * - Door lock control (relay)
 * - Warning alert handling
 * - Card enrolment mode (armed by Edge via door/cmd/enroll)
 * 
 * OR Gate Logic:
 * - RFID match → Unlock locally (no Edge needed)
 * - Face match → Edge sends MQTT unlock command
 */

#include <WiFi.h>
#include "config.h"
#include "rfid_reader.h"
#include "nvs_storage.h"
#include "mqtt_handler.h"
#include "door_lock.h"

// ── Global Objects ───────────────────────────────────────────────────────────
WiFiClient espClient;
RFIDReader rfid;
NVSStorage storage;
MQTTHandler mqtt;
DoorLock door;

// ── Health Report Timer ──────────────────────────────────────────────────────
unsigned long lastHealthReport = 0;

// ── RFID Access Control ──────────────────────────────────────────────────────
// Optional: Cooldown between RFID reads to prevent spam
unsigned long lastRFIDRead = 0;
const unsigned long RFID_COOLDOWN = 2000;  // 2 seconds

// ── Pending Face-Unlock Log (published to Edge in loop) ─────────────────────
String pendingFaceLogPersonID = "";
String pendingFaceLogName = "";

// ── Enrolment Mode (armed by Edge via door/cmd/enroll) ─────────────────────
bool enrolArmed = false;
unsigned long enrolDeadline = 0;

// ── WiFi Connection ──────────────────────────────────────────────────────────
void connectToWiFi() {
    Serial.print("[WiFi] Connecting to ");
    Serial.println(WIFI_SSID);

    WiFi.mode(WIFI_STA);
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

    int timeout = WIFI_TIMEOUT / 500;
    while (WiFi.status() != WL_CONNECTED && timeout > 0) {
        delay(500);
        Serial.print(".");
        timeout--;
    }

    if (WiFi.status() == WL_CONNECTED) {
        Serial.println();
        Serial.println("[WiFi] Connected!");
        Serial.print("[WiFi] IP Address: ");
        Serial.println(WiFi.localIP());
        Serial.print("[WiFi] RSSI: ");
        Serial.print(WiFi.RSSI());
        Serial.println(" dBm");
    } else {
        Serial.println();
        Serial.println("[WiFi] Connection failed!");
    }
}

// ── MQTT Callbacks ───────────────────────────────────────────────────────────
void onUnlock(const char* personID, const char* name) {
    // Face match unlock from Edge
    door.unlock(personID, name);

    // Queue the access log — published in loop() so it isn't sent from
    // inside the MQTT callback (blocking there can stall the connection).
    pendingFaceLogPersonID = personID;
    pendingFaceLogName = name;
    door.publishAccessLog = true;
}

void onCardSync(const char* type, const char* cardUID, 
                const char* personID, const char* name) {
    if (strcmp(type, "add") == 0) {
        storage.addCard(cardUID, personID, name);
    }
    else if (strcmp(type, "delete") == 0) {
        storage.removeCard(cardUID);
    }
    else if (strcmp(type, "full_sync") == 0) {
        // Full sync handled in loop
    }
}

void onEnroll(bool arm, unsigned long timeoutS) {
    if (arm) {
        enrolArmed = true;
        enrolDeadline = millis() + timeoutS * 1000UL;
        Serial.print("[Main] ENROLMENT ARMED for ");
        Serial.print(timeoutS);
        Serial.println("s — present a card");
        door.deny();   // visual cue (double beep)
    } else {
        enrolArmed = false;
        Serial.println("[Main] ENROLMENT DISARMED");
    }
}

// ── WiFi Reconnect ───────────────────────────────────────────────────────────
void reconnectWiFi() {
    if (WiFi.status() != WL_CONNECTED) {
        Serial.println("[WiFi] Disconnected, reconnecting...");
        WiFi.reconnect();
        delay(2000);
    }
}

// ── Setup ────────────────────────────────────────────────────────────────────
void setup() {
    Serial.begin(115200);
    delay(2000);

    Serial.println("\n\n");
    Serial.println("╔════════════════════════════════════════════════════╗");
    Serial.println("║  ESP32 Door Control System                        ║");
    Serial.println("║  Edge-Cloud Integrated                            ║");
    Serial.println("╚════════════════════════════════════════════════════╝");
    Serial.println();

    // Initialize modules
    Serial.println("[Init] Starting initialization...");

    // 1. Door lock
    Serial.println("[Init] Initializing door lock...");
    door.begin();

    // 2. NVS Storage
    Serial.println("[Init] Initializing NVS storage...");
    storage.begin();
    storage.printAllCards();

    // 3. RFID Reader
    Serial.println("[Init] Initializing RFID reader...");
    rfid.begin();

    // 4. WiFi
    Serial.println("[Init] Connecting to WiFi...");
    connectToWiFi();

    // 5. MQTT
    Serial.println("[Init] Initializing MQTT...");
    mqtt.begin();
    mqtt.setUnlockCallback(onUnlock);
    mqtt.setCardSyncCallback(onCardSync);
    mqtt.setEnrollCallback(onEnroll);
    mqtt.connect();

    // Ready!
    Serial.println();
    Serial.println("[Init] ====================");
    Serial.println("[Init] System ready!");
    Serial.println("[Init] Waiting for RFID cards or MQTT commands...");
    Serial.println();
}

// ── Loop ─────────────────────────────────────────────────────────────────────
void loop() {
    // 1. Maintain WiFi connection
    reconnectWiFi();

    // 2. Maintain MQTT connection
    mqtt.loop();

    // 3. Update door lock (auto-lock timer)
    door.update();

    // 4. Flush pending face-unlock log to Edge
    if (door.publishAccessLog) {
        door.publishAccessLog = false;
        mqtt.publishAccessLog(
            "", 
            pendingFaceLogPersonID.c_str(), 
            pendingFaceLogName.c_str(), 
            "face", 
            true
        );
    }

    // 5. Check for RFID card
    unsigned long now = millis();
    if (now - lastRFIDRead > RFID_COOLDOWN) {
        String cardUID = rfid.readCard();
        
        if (cardUID.length() > 0) {
            lastRFIDRead = now;

            // Enrolment mode: relay the card to the Edge, skip access logic
            if (enrolArmed) {
                if (now >= enrolDeadline) {
                    enrolArmed = false;
                    Serial.println("[Main] ENROLMENT TIMEOUT — disarmed");
                } else {
                    Serial.print("[Main] Enrol capture: ");
                    Serial.println(cardUID);
                    mqtt.publishEnrollCapture(cardUID.c_str());
                    rfid.haltCard();
                    return;
                }
            }

            // Look up card in local storage
            String personID, name;
            if (storage.lookupCard(cardUID, personID, name)) {
                // Card found — unlock locally (OR gate: RFID)
                Serial.print("[Main] RFID match: ");
                Serial.print(name);
                Serial.print(" (");
                Serial.print(personID);
                Serial.println(")");
                
                door.unlock(personID.c_str(), name.c_str());
                
                // Log access to Edge via MQTT
                mqtt.publishAccessLog(
                    cardUID.c_str(), 
                    personID.c_str(), 
                    name.c_str(), 
                    "rfid", 
                    true
                );
            } else {
                // Unknown card
                Serial.print("[Main] Unknown card: ");
                Serial.println(cardUID);
                
                door.deny();
                
                // Log denied access
                mqtt.publishAccessLog(
                    cardUID.c_str(), 
                    "", 
                    "", 
                    "rfid", 
                    false
                );
            }
            
            rfid.haltCard();
        }
    }

    // 6. Periodic health report
    if (now - lastHealthReport >= HEALTH_INTERVAL) {
        lastHealthReport = now;
        mqtt.publishHealth(storage.getCardCount());
    }

    // Small delay to prevent watchdog issues
    delay(10);
}
