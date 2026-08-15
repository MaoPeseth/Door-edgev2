/*
 * config.h — ESP32 Door Control Configuration
 * 
 * All settings in one place. Modify before uploading.
 */
#ifndef CONFIG_H
#define CONFIG_H

// ── WiFi ─────────────────────────────────────────────────────────────────────
#define WIFI_SSID           "seth"           // WiFi SSID
#define WIFI_PASSWORD       "123456789"      // WiFi password
#define WIFI_TIMEOUT        20000            // WiFi connection timeout (ms)

// ── MQTT ─────────────────────────────────────────────────────────────────────
#define MQTT_BROKER         "10.4.70.127"    // MQTT broker IP (Edge PC)
#define MQTT_PORT           1883             // MQTT broker port
#define MQTT_CLIENT_ID      "ESP32_Door_01"  // Unique client ID

// MQTT Topics - Subscribe (receive from Edge)
#define TOPIC_UNLOCK        "door/cmd/unlock"
#define TOPIC_DENIED        "door/access/denied"
#define TOPIC_SYNC_CARD     "door/sync/card_uid"
#define TOPIC_ALERT         "door/alert/warning"
#define TOPIC_ENROLL_CMD    "door/cmd/enroll"

// MQTT Topics - Publish (send to Edge)
#define TOPIC_HEALTH        "door/health"
#define TOPIC_ACCESS_LOG    "door/access/log"
#define TOPIC_ENROLL_CAPTURE "door/enroll/capture"

// ── Door ID ──────────────────────────────────────────────────────────────────
#define DOOR_ID             "door-01"

// ── RFID (MFRC522) ──────────────────────────────────────────────────────────
#define RFID_SS_PIN         5               // SDA/SS pin
#define RFID_RST_PIN        4               // RST pin
#define RFID_MOSI_PIN       23              // MOSI pin
#define RFID_MISO_PIN       19              // MISO pin
#define RFID_SCK_PIN        18              // SCK pin

// ── Door Lock (Relay) ────────────────────────────────────────────────────────
#define DOOR_RELAY_PIN      26              // Relay GPIO pin
#define DOOR_LOCK_TIME      3000            // Door unlock duration (ms)

// ── Status LED ───────────────────────────────────────────────────────────────
#define LED_STATUS_PIN      2               // Built-in LED for status
#define LED_GREEN_PIN       12              // Green LED (access granted)
#define LED_RED_PIN         14              // Red LED (access denied)

// ── Buzzer (for alert) ──────────────────────────────────────────────────────
#define BUZZER_PIN          25              // Buzzer GPIO pin
#define BUZZER_ALERT_COUNT  3               // Number of beeps for alert

// ── Health Report ────────────────────────────────────────────────────────────
#define HEALTH_INTERVAL     5000            // Health report interval (ms)

// ── NVS Storage ──────────────────────────────────────────────────────────────
#define NVS_NAMESPACE       "door_cards"    // NVS namespace for card storage
#define NVS_MAX_CARDS       200             // Maximum cards to store

// ── Card UID Format ──────────────────────────────────────────────────────────
// RFID card UID is stored as hex string (e.g., "A1B2C3D4")
// Maximum UID length: 10 characters (4 bytes = 8 hex + null)
#define CARD_UID_MAX_LEN    16

#endif // CONFIG_H
