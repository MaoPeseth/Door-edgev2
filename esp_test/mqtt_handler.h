/*
 * mqtt_handler.h — MQTT Handler Module
 * 
 * Handles MQTT communication with Edge PC.
 * Subscribes to unlock/denied/sync/alert topics.
 * Publishes health and access logs.
 */
#ifndef MQTT_HANDLER_H
#define MQTT_HANDLER_H

#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include "config.h"

// Callback function type for door unlock
typedef void (*UnlockCallback)(const char* personID, const char* name);

// Callback function type for card sync
typedef void (*CardSyncCallback)(const char* type, const char* cardUID, 
                                  const char* personID, const char* name);

// Callback function type for enrolment arm/disarm
typedef void (*EnrollCallback)(bool arm, unsigned long timeoutS);

class MQTTHandler {
public:
    MQTTHandler() : _client(_espClient), _connected(false) {}

    void begin() {
        _client.setServer(MQTT_BROKER, MQTT_PORT);
        _client.setCallback([this](char* topic, byte* payload, unsigned int length) {
            this->_onMessage(topic, payload, length);
        });
    }

    void setUnlockCallback(UnlockCallback callback) {
        _unlockCallback = callback;
    }

    void setCardSyncCallback(CardSyncCallback callback) {
        _cardSyncCallback = callback;
    }

    void setEnrollCallback(EnrollCallback callback) {
        _enrollCallback = callback;
    }

    void connect() {
        Serial.print("[MQTT] Connecting to ");
        Serial.print(MQTT_BROKER);
        Serial.print(":");
        Serial.println(MQTT_PORT);

        if (_client.connect(MQTT_CLIENT_ID)) {
            Serial.println("[MQTT] Connected!");
            _connected = true;

            // Subscribe to topics
            _client.subscribe(TOPIC_UNLOCK);
            _client.subscribe(TOPIC_DENIED);
            _client.subscribe(TOPIC_SYNC_CARD);
            _client.subscribe(TOPIC_ALERT);
            _client.subscribe(TOPIC_ENROLL_CMD);

            Serial.println("[MQTT] Subscribed to topics:");
            Serial.println("  - " TOPIC_UNLOCK);
            Serial.println("  - " TOPIC_DENIED);
            Serial.println("  - " TOPIC_SYNC_CARD);
            Serial.println("  - " TOPIC_ALERT);
            Serial.println("  - " TOPIC_ENROLL_CMD);

            // Publish online status
            publishHealth(0);
        } else {
            Serial.print("[MQTT] Connection failed, rc=");
            Serial.println(_client.state());
            _connected = false;
        }
    }

    void loop() {
        if (!_client.connected()) {
            connect();
        }
        _client.loop();
    }

    bool isConnected() const {
        return _connected;
    }

    // ── Publish Methods ──────────────────────────────────────────────────────

    void publishHealth(int enrolledFaces) {
        StaticJsonDocument<256> doc;
        doc["door_id"] = DOOR_ID;
        doc["status"] = "online";
        doc["enrolled_faces"] = enrolledFaces;
        doc["uptime"] = millis() / 1000;
        doc["free_heap"] = ESP.getFreeHeap();

        char buffer[256];
        serializeJson(doc, buffer);
        _client.publish(TOPIC_HEALTH, buffer);
    }

    void publishAccessLog(const char* cardUID, const char* personID, 
                          const char* name, const char* method, bool granted) {
        StaticJsonDocument<256> doc;
        doc["door_id"] = DOOR_ID;
        doc["card_uid"] = cardUID;
        doc["person_id"] = personID;
        doc["name"] = name;
        doc["method"] = method;
        doc["granted"] = granted;
        doc["timestamp"] = millis();

        char buffer[256];
        serializeJson(doc, buffer);
        _client.publish(TOPIC_ACCESS_LOG, buffer);
    }

    void publishEnrollCapture(const char* tagID) {
        StaticJsonDocument<128> doc;
        doc["tagID"] = tagID;
        doc["door_id"] = DOOR_ID;
        doc["timestamp"] = millis();

        char buffer[128];
        serializeJson(doc, buffer);
        _client.publish(TOPIC_ENROLL_CAPTURE, buffer);
        Serial.print("[MQTT] ENROLL CAPTURE → tagID=");
        Serial.println(tagID);
    }

private:
    WiFiClient _espClient;
    PubSubClient _client;
    bool _connected;

    UnlockCallback _unlockCallback = nullptr;
    CardSyncCallback _cardSyncCallback = nullptr;
    EnrollCallback _enrollCallback = nullptr;

    void _onMessage(char* topic, byte* payload, unsigned int length) {
        // Convert payload to string
        char message[length + 1];
        memcpy(message, payload, length);
        message[length] = '\0';

        Serial.print("[MQTT] Received on ");
        Serial.print(topic);
        Serial.print(": ");
        Serial.println(message);

        // Parse JSON
        StaticJsonDocument<512> doc;
        DeserializationError error = deserializeJson(doc, message);
        if (error) {
            Serial.print("[MQTT] JSON parse error: ");
            Serial.println(error.c_str());
            return;
        }

        // Handle different topics
        if (strcmp(topic, TOPIC_UNLOCK) == 0) {
            _handleUnlock(doc);
        }
        else if (strcmp(topic, TOPIC_SYNC_CARD) == 0) {
            _handleCardSync(doc);
        }
        else if (strcmp(topic, TOPIC_ALERT) == 0) {
            _handleAlert(doc);
        }
        else if (strcmp(topic, TOPIC_ENROLL_CMD) == 0) {
            _handleEnroll(doc);
        }
    }

    void _handleEnroll(JsonDocument& doc) {
        bool arm = doc["enroll"] | false;
        unsigned long timeoutS = doc["timeoutS"] | 0;

        Serial.print("[MQTT] ENROLL CMD: arm=");
        Serial.print(arm);
        Serial.print(", timeoutS=");
        Serial.println(timeoutS);

        if (_enrollCallback) {
            _enrollCallback(arm, timeoutS);
        }
    }

    void _handleUnlock(JsonDocument& doc) {
        const char* personID = doc["person_id"] | "";
        const char* name = doc["name"] | "";
        const char* method = doc["method"] | "face";

        Serial.print("[MQTT] UNLOCK: ");
        Serial.print(name);
        Serial.print(" (");
        Serial.print(personID);
        Serial.print(") via ");
        Serial.println(method);

        if (_unlockCallback) {
            _unlockCallback(personID, name);
        }
    }

    void _handleCardSync(JsonDocument& doc) {
        const char* type = doc["type"] | "";

        if (strcmp(type, "full_sync") == 0) {
            // Full sync: {"type":"full_sync","cards":[...]}
            JsonArray cards = doc["cards"].as<JsonArray>();
            for (JsonObject card : cards) {
                const char* cardUID = card["card_uid"] | "";
                const char* personID = card["person_id"] | "";
                const char* name = card["name"] | "";

                if (_cardSyncCallback) {
                    _cardSyncCallback("add", cardUID, personID, name);
                }
            }
            Serial.print("[MQTT] Full sync: ");
            Serial.print(cards.size());
            Serial.println(" cards");
        }
        else if (strcmp(type, "add") == 0) {
            // Single add: {"type":"add","card_uid":"...","person_id":"...","name":"..."}
            const char* cardUID = doc["card_uid"] | "";
            const char* personID = doc["person_id"] | "";
            const char* name = doc["name"] | "";

            if (_cardSyncCallback) {
                _cardSyncCallback("add", cardUID, personID, name);
            }
        }
        else if (strcmp(type, "delete") == 0) {
            // Single delete: {"type":"delete","card_uid":"..."}
            const char* cardUID = doc["card_uid"] | "";

            if (_cardSyncCallback) {
                _cardSyncCallback("delete", cardUID, "", "");
            }
        }
    }

    void _handleAlert(JsonDocument& doc) {
        const char* type = doc["type"] | "";
        const char* reason = doc["reason"] | "";
        int failCount = doc["fail_count"] | 0;

        Serial.print("[MQTT] ALERT: ");
        Serial.print(type);
        Serial.print(" - ");
        Serial.print(reason);
        Serial.print(" (count: ");
        Serial.print(failCount);
        Serial.println(")");

        // Trigger local alarm (buzzer/LED)
        _triggerLocalAlarm();
    }

    void _triggerLocalAlarm() {
        // Activate buzzer for alert
        for (int i = 0; i < BUZZER_ALERT_COUNT; i++) {
            digitalWrite(BUZZER_PIN, HIGH);
            delay(200);
            digitalWrite(BUZZER_PIN, LOW);
            delay(100);
        }
    }
};

#endif // MQTT_HANDLER_H
