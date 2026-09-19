/****************************************************
 * ESP_mqtt — TEE door controller, MQTT-only (Door-Edge design)
 *
 * Derived from ESP32_1CH_RUPPV2 (production v3.0.0) but with the HTTP
 * REST route cut: this firmware is driven entirely over the MQTT broker
 * by the Door-Edge edge PC, using the contract in
 *   Door-Edge_new_machine/docs/sync_agent_design.md  § MQTT Topics.
 *
 * Reused unchanged from the production door:
 *   - the full pin table (configuration.h) — relay, buzzer, LEDs, EM4100
 *     RFID on UART1, bypass + exit switches
 *   - EM4100.h (125 kHz reader, 10-digit decimal UID)
 *   - TagManager.h (LittleFS allowlist, max 500 tags, write-then-rename)
 *
 * Interface (all MQTT, QoS 1 where it matters):
 *   ESP → edge:  door/status       retained "online"/"offline" (also LWT)
 *                door/access/log   {granted, method, name, person_id,
 *                                   card_uid, reason, timestamp}
 *   edge → ESP:  door/cmd/unlock   → openDoor()          (relay, 3 s)
 *                door/cmd/lock     → lockDoor()
 *                door/alert/warning {type:"warning"|"alert_cleared"}
 *                door/access/denied {reason:"spoof_detected"|"unknown_face"|...}
 *                                  → print + lock + buzz (spoof), deny print (others)
 *                door/sync/card_uid {type:"full_sync","add"|"delete",
 *                                   cards:[{card_uid,name,person_id}]}
 *
 * Offline-first is preserved: RFID + the LittleFS allowlist work with no
 * broker at all; allowlist updates are just missed while down (the edge
 * re-pushes full_sync on every re-sync). Card UIDs are accepted in any
 * format (10-digit decimal, 4-byte hex, ...); at scan time the reader's
 * decimal output is also matched against the hex form of the same card.
 ****************************************************/

#include <WiFi.h>
#include <ESPmDNS.h>
#include <ArduinoOTA.h>
#include <HardwareSerial.h>
#include <PubSubClient.h>
#include <Preferences.h>
#include <time.h>

#include "configuration.h"
#include "EM4100.h"
#include "TagManager.h"

WiFiClient mqttNet;
PubSubClient mqtt(mqttNet);
HardwareSerial rfidSerial(1);
RFID rfid(rfidSerial, RFID_RX_PIN);
TagManager tagManager;

unsigned long previousBlinkMillis = 0;
const long blinkInterval = 1000;
bool ledState = false;

String lastScannedTagID = "";
unsigned long lastScannedTagTime = 0;

bool relayState[NUM_RELAYS];
unsigned long doorOpenUntil = 0;
bool doorTimerActive = false;

bool lastBypassState = HIGH;
bool bypassActive = false;

unsigned long lastWiFiReconnectAttempt = 0;
bool wifiReconnecting = false;
unsigned long lastWifiBlink = 0;
bool wifiLedState = false;
bool netServicesStarted = false;
bool otaInProgress = false;

unsigned long nextMqttAttempt = 0;

// ==================== Relay ====================

void updateRelayOutput() {
  digitalWrite(RELAY_PINS[0], relayState[0] ? HIGH : LOW);
}

// Lock state is deliberately not persisted — a reboot leaves the door locked.

void lockDoor() {
  relayState[0] = false;
  updateRelayOutput();
  doorTimerActive = false;
}

// ==================== Buzzer ====================

enum BuzzMode : uint8_t { BUZZ_OFF = 0, BUZZ_SHORT, BUZZ_ALARM };
BuzzMode buzzMode = BUZZ_OFF;
uint8_t buzzPhase = 0;
uint32_t buzzT0 = 0;

void startShortBuzz()  { buzzMode = BUZZ_SHORT;  buzzPhase = 0; buzzT0 = millis(); digitalWrite(BUZZER_PIN, HIGH); }
void startAlarmBuzz()  { buzzMode = BUZZ_ALARM;  buzzPhase = 0; buzzT0 = millis(); digitalWrite(BUZZER_PIN, HIGH); }

void tickBuzzer() {
  uint32_t now = millis();
  switch (buzzMode) {
    case BUZZ_SHORT:
      if (now - buzzT0 >= 150) { digitalWrite(BUZZER_PIN, LOW); buzzMode = BUZZ_OFF; }
      break;
    case BUZZ_ALARM:
      if (now - buzzT0 >= 150) {
        buzzT0 = now;
        digitalWrite(BUZZER_PIN, !digitalRead(BUZZER_PIN));
        if (++buzzPhase >= 6) { buzzMode = BUZZ_OFF; digitalWrite(BUZZER_PIN, LOW); }
      }
      break;
    default: break;
  }
}

// ==================== Door ====================

void tickDoorTimer() {
  if (doorTimerActive && (int32_t)(millis() - doorOpenUntil) >= 0) {
    lockDoor();
    Serial.println("Door auto-locked");
  }
}

void openDoor() {
  if (bypassActive) return;
  relayState[0] = true;
  updateRelayOutput();
  doorOpenUntil = millis() + DOOR_UNLOCK_MS;
  doorTimerActive = true;
  startShortBuzz();
  Serial.println("Door opened");
}

void applyBypassState(bool pinLow) {
  if (pinLow) {
    relayState[0] = true;
    updateRelayOutput();
    doorTimerActive = false;
    if (!bypassActive) Serial.println("BYPASS ACTIVE");
    bypassActive = true;
  } else {
    lockDoor();
    if (bypassActive) Serial.println("BYPASS OFF");
    bypassActive = false;
  }
}

// ==================== JSON helpers ====================

String jsonEscape(const char* s) {
  String out;
  for (const char* p = s; *p; ++p) {
    if (*p == '"' || *p == '\\') out += '\\';
    out += *p;
  }
  return out;
}

// Flat extractor for "key":"value" fields in the command payloads.
// Tolerates whitespace after the colon ("key": "value" as well as "key":"value").
String jsonField(const String& src, const char* key) {
  String needle = String("\"") + key + "\":";
  int start = src.indexOf(needle);
  if (start < 0) return "";
  start += needle.length();
  while (start < (int)src.length() && (src[start] == ' ' || src[start] == '\t')) start++;
  if (start >= (int)src.length() || src[start] != '"') return "";
  start++;
  int end = src.indexOf('"', start);
  return (end < 0) ? "" : src.substring(start, end);
}

// Any non-empty UID fits the allowlist entry (tagID buffer is 12 bytes).
// The 10-digit-only gate was dropped deliberately: some backends store the
// card as 4-byte hex (e.g. 856E074B) instead of 10-digit decimal. Matching
// normalises both forms at scan time (see processRFID).
bool isValidUID(const String& s) {
  return s.length() > 0 && s.length() < 12;
}

// 10-digit decimal (reader output) -> 8-char uppercase hex (cloud storage),
// e.g. "2239560523" -> "856E074B". Returns "" when the input is not a
// 32-bit decimal string.
String decToHex(const String& s) {
  if (s.length() == 0 || s.length() > 10) return "";
  for (unsigned int i = 0; i < s.length(); i++)
    if (!isdigit(s[i])) return "";
  unsigned long v = strtoul(s.c_str(), nullptr, 10);
  if (v > 0xFFFFFFFFUL) return "";
  char buf[9];
  snprintf(buf, sizeof(buf), "%08lX", v);
  return String(buf);
}

// Returns the substring of "key": [...] (a JSON array), or ""
// Tolerates whitespace after the colon ("key": [ ... ]).
String jsonArrayOf(const String& body, const char* key) {
  String needle = String("\"") + key + "\":";
  int start = body.indexOf(needle);
  if (start < 0) return "";
  start += needle.length();
  while (start < (int)body.length() && (body[start] == ' ' || body[start] == '\t')) start++;
  if (start >= (int)body.length() || body[start] != '[') return "";
  start++;
  int depth = 1;
  for (int i = start; i < (int)body.length(); i++) {
    char c = body[i];
    if (c == '[') depth++;
    else if (c == ']') { depth--; if (depth == 0) return body.substring(start, i); }
  }
  return "";
}

// Number of "{"..."}" objects in an array string
int jsonElemCount(const String& arr) {
  int n = 0;
  for (int i = 0; i < (int)arr.length(); i++) if (arr[i] == '{') n++;
  return n;
}

// Element idx (0-based) of an array string as its own JSON object
String jsonElem(const String& arr, int idx) {
  int depth = 0, elem = -1, start = -1;
  for (int i = 0; i < (int)arr.length(); i++) {
    char c = arr[i];
    if (c == '{') {
      if (depth == 0) { elem++; if (elem == idx) start = i; }
      depth++;
    } else if (c == '}') {
      depth--;
      if (depth == 0 && elem == idx && start >= 0) return arr.substring(start, i + 1);
    }
  }
  return "";
}

String isoNow() {
  time_t now = time(nullptr);
  if (now <= 1000000000UL) return "";
  struct tm tmUtc;
  gmtime_r(&now, &tmUtc);
  char iso[32];
  strftime(iso, sizeof(iso), "%Y-%m-%dT%H:%M:%S+00:00", &tmUtc);
  return String(iso);
}

// ==================== MQTT ====================

void publishAccessLog(bool granted, const String& studentId,
                      const String& name, const String& cardID,
                      const char* reason) {
  if (!mqtt.connected()) return;
  String body = "{\"granted\":" + String(granted ? "true" : "false");
  body += ",\"method\":\"rfid\"";
  if (granted) {
    if (studentId.length()) body += ",\"person_id\":\"" + jsonEscape(studentId.c_str()) + "\"";
    if (name.length())      body += ",\"name\":\"" + jsonEscape(name.c_str()) + "\"";
  } else if (reason) {
    body += ",\"reason\":\"" + String(reason) + "\"";
  }
  body += ",\"card_uid\":\"" + jsonEscape(cardID.c_str()) + "\"";
  String ts = isoNow();
  if (ts.length()) body += ",\"timestamp\":\"" + ts + "\"";
  body += "}";
  mqtt.publish(MQTT_TOPIC_ACCESS_LOG, (const uint8_t*)body.c_str(), body.length(), false);
  Serial.printf("[MQTT] %s card %s\n", granted ? "GRANTED" : "DENIED", cardID.c_str());
}

void handleCardSync(const String& body) {
  String type = jsonField(body, "type");
  String card = jsonField(body, "card_uid");

  if (type == "add") {
    if (!isValidUID(card)) {
      Serial.printf("card add rejected: invalid UID (%s)\n", card.c_str());
      return;
    }
    bool ok = tagManager.addMapping(card, jsonField(body, "name"), jsonField(body, "person_id"));
    Serial.printf("MQTT add %s -> %s\n", card.c_str(), ok ? "stored" : "rejected");
    return;
  }

  if (type == "delete") {
    bool ok = tagManager.deleteMapping(card);
    Serial.printf("MQTT delete %s -> %s\n", card.c_str(), ok ? "removed" : "not held");
    return;
  }

  if (type == "full_sync") {
    String arr = jsonArrayOf(body, "cards");
    int n = jsonElemCount(arr);
    if (n == 0) {
      tagManager.clearAll();
      Serial.println("MQTT full_sync: empty roster — allowlist cleared");
      return;
    }
    // Two passes: count the valid UIDs first so the atomic bulk load
    // (which commits only on an exact count) never aborts mid-table.
    int valid = 0;
    for (int i = 0; i < n; i++)
      if (isValidUID(jsonField(jsonElem(arr, i), "card_uid"))) valid++;
    if (valid == 0) {
      Serial.println("MQTT full_sync: no valid UIDs — table kept");
      return;
    }
    if (!tagManager.beginBulkLoad(valid)) {
      Serial.println("MQTT full_sync: bulk load rejected");
      return;
    }
    bool ok = true;
    for (int i = 0; ok && i < n; i++) {
      String e = jsonElem(arr, i);
      String uid = jsonField(e, "card_uid");
      if (!isValidUID(uid)) continue;
      ok = tagManager.bulkAdd(uid, jsonField(e, "name"), jsonField(e, "person_id"), true, 0);
    }
    if (ok && tagManager.commitBulkLoad()) {
      Serial.printf("MQTT full_sync: %d tag(s)\n", tagManager.count());
    } else {
      tagManager.abortBulkLoad();
      Serial.println("MQTT full_sync aborted — existing table kept");
    }
    return;
  }

  Serial.println("MQTT card sync: unrecognised type");
}

void handleMqttCommand(char* topic, byte* payload, unsigned int length) {
  String body;
  body.reserve(length);
  for (unsigned int i = 0; i < length; i++) body += (char)payload[i];

  if (strcmp(topic, MQTT_TOPIC_UNLOCK) == 0) {
    if (bypassActive) { Serial.println("MQTT unlock ignored: bypass active"); return; }
    String name   = jsonField(body, "name");
    String method = jsonField(body, "method");
    if (name.length() > 0) {
      Serial.printf("Door unlocked via %s: %s\n",
                    method.length() > 0 ? method.c_str() : "face", name.c_str());
    } else {
      Serial.println("Door unlocked via MQTT");
    }
    openDoor();
    return;
  }

  if (strcmp(topic, MQTT_TOPIC_LOCK) == 0) {
    Serial.println("Door locked via MQTT");
    lockDoor();
    return;
  }

  if (strcmp(topic, MQTT_TOPIC_DENIED) == 0) {
    String reason = jsonField(body, "reason");
    if (reason == "spoof_detected") {
      // Anti-spoof blocked an unlock — keep the door locked, sound the alarm.
      Serial.println("SPOOFING DETECTED - door stays locked");
      if (!bypassActive) lockDoor();
      startAlarmBuzz();
    } else if (reason == "unknown_face") {
      Serial.println("ACCESS DENIED - unknown face");
      startShortBuzz();
    } else {
      Serial.printf("ACCESS DENIED - %s\n",
                    reason.length() > 0 ? reason.c_str() : "unknown reason");
    }
    return;
  }

  if (strcmp(topic, MQTT_TOPIC_ALERT) == 0) {
    String type = jsonField(body, "type");
    if (type == "warning")      { startAlarmBuzz(); Serial.println("MQTT alert: warning"); }
    else if (type == "alert_cleared") { Serial.println("MQTT alert: cleared"); }
    else Serial.println("MQTT alert: unrecognised");
    return;
  }

  if (strcmp(topic, MQTT_TOPIC_CARD_SYNC) == 0) {
    handleCardSync(body);
    return;
  }

  Serial.println("MQTT: unrecognised topic");
}

void tickMqtt() {
  if (WiFi.status() != WL_CONNECTED) return;

  if (mqtt.connected()) { mqtt.loop(); return; }
  if ((int32_t)(millis() - nextMqttAttempt) < 0) return;
  nextMqttAttempt = millis() + MQTT_RECONNECT_MS;

  // Resolve tee.local via mDNS
  IPAddress edgeIP = MDNS.queryHost(MQTT_HOST);
  if (edgeIP == IPAddress(0, 0, 0, 0)) {
    Serial.println("mDNS: cannot resolve " + String(MQTT_HOST) + " — will retry");
    return;
  }
  mqtt.setServer(edgeIP, MQTT_PORT);

  // cleanSession false is the whole point: the broker holds QoS 1 commands
  // for this client id while the door is down and delivers them on reconnect.
  bool ok = mqtt.connect(DEVICE_NAME, MQTT_USER, MQTT_PASSWORD,
                         MQTT_TOPIC_STATUS, 1, true, "offline", false);
  if (!ok) {
    Serial.printf("MQTT connect failed (state %d)\n", mqtt.state());
    return;
  }
  mqtt.publish(MQTT_TOPIC_STATUS, "online", true);
  mqtt.subscribe(MQTT_TOPIC_UNLOCK, 1);
  mqtt.subscribe(MQTT_TOPIC_LOCK, 1);
  mqtt.subscribe(MQTT_TOPIC_ALERT, 1);
  mqtt.subscribe(MQTT_TOPIC_CARD_SYNC, 1);
  mqtt.subscribe(MQTT_TOPIC_DENIED, 1);
  Serial.println("MQTT connected");
}

// Say goodbye properly rather than leaving the will to fire on keepalive
void mqttAnnounceOffline() {
  if (!mqtt.connected()) return;
  mqtt.publish(MQTT_TOPIC_STATUS, "offline", true);
  mqtt.disconnect();
}

// ==================== RFID ====================

void processRFID() {
  String cardID = rfid.readCardID();
  if (cardID.length() == 0) return;

  // The reader repeats the frame while a card is held on the antenna
  if (cardID == lastScannedTagID && millis() - lastScannedTagTime < SCAN_DEBOUNCE_MS) return;
  lastScannedTagID = cardID;
  lastScannedTagTime = millis();

  TagManager::Lookup tag = tagManager.find(cardID);
  if (!tag.found) {
    // Backends may store the same card as 4-byte hex (e.g. 856E074B) while
    // the reader emits it in decimal (2239560523) — try the hex form too.
    String hex = decToHex(cardID);
    if (hex.length()) tag = tagManager.find(hex);
  }
  bool granted = tag.found && tag.active && !tag.expired;

  Serial.printf("Card %s -> %s%s\n", cardID.c_str(),
                granted ? "GRANTED " : "DENIED ",
                tag.found ? tag.name.c_str() : "(unknown)");

  if (granted) {
    startShortBuzz();
    openDoor();
    publishAccessLog(true, tag.studentId, tag.name, cardID, nullptr);
  } else {
    startAlarmBuzz();
    if (tag.found) {
      const char* why = tag.expired ? "expired" : "deactivated";
      publishAccessLog(false, tag.studentId, tag.name, cardID, why);
    } else {
      publishAccessLog(false, "", "", cardID, "unknown_card");
    }
  }
}

// ==================== WiFi ====================

void setupArduinoOTA() {
  ArduinoOTA.setHostname(HOST);          // also brings up mDNS
  ArduinoOTA.setPassword(OTA_PASSWORD);

  ArduinoOTA.onStart([]() { prepareForFirmwareWrite(); Serial.println("ArduinoOTA start"); });
  ArduinoOTA.onEnd([]()   { Serial.println("\nArduinoOTA done, rebooting"); });
  ArduinoOTA.onProgress([](unsigned int done, unsigned int total) {
    Serial.printf("ArduinoOTA %u%%\r", total ? done * 100 / total : 0);
  });
  ArduinoOTA.onError([](ota_error_t err) {
    otaInProgress = false;
    Serial.printf("ArduinoOTA error %u\n", err);
  });
  ArduinoOTA.begin();
}

void handleWiFi() {
  if (WiFi.status() == WL_CONNECTED) {
    if (wifiReconnecting) {
      Serial.println("WiFi reconnected: " + WiFi.localIP().toString());
      wifiReconnecting = false;
    }
    digitalWrite(WIFI_PIN, HIGH);
    if (!netServicesStarted) {
      setupArduinoOTA();
      netServicesStarted = true;
    }
    return;
  }

  unsigned long now = millis();
  if (now - lastWifiBlink >= 250) {           // blink while the link is down
    lastWifiBlink = now;
    wifiLedState = !wifiLedState;
    digitalWrite(WIFI_PIN, wifiLedState ? HIGH : LOW);
  }
  if (now - lastWiFiReconnectAttempt >= 10000) {
    lastWiFiReconnectAttempt = now;
    wifiReconnecting = true;
    Serial.println("WiFi down, reconnecting...");
    // OTA and mDNS are bound to the old IP — tear them down and re-announce
    // once the link is back.
    if (netServicesStarted) { ArduinoOTA.end(); netServicesStarted = false; }
    WiFi.disconnect();
    WiFi.begin(SSID, WIFI_PASSWORD);
  }
}

void syncTime() {
  configTime(0, 0, "asia.pool.ntp.org", "pool.ntp.org", "time.nist.gov");
  setenv("TZ", "ICT-7", 1);
  tzset();

  // Short wait only — SNTP keeps retrying in the background, and tag expiry is
  // the only thing that needs the clock.
  unsigned long start = millis();
  while (time(nullptr) < 1000000000UL && millis() - start < 10000UL) delay(250);

  time_t now = time(nullptr);
  if (now > 1000000000UL) {
    Serial.println("NTP time synced");
    Preferences boot;
    boot.begin("system", false);
    boot.putULong("bootTime", (unsigned long)now);
    boot.end();
  } else {
    Serial.println("NTP sync failed (retrying in background)");
  }
}

// Put the door in a safe state before anything that ends in a reboot
void prepareForFirmwareWrite() {
  otaInProgress = true;
  buzzMode = BUZZ_OFF;
  digitalWrite(BUZZER_PIN, LOW);
  if (!bypassActive) lockDoor();
  mqttAnnounceOffline();
}

// ==================== Lifecycle ====================

void setup() {
  Serial.begin(115200);
  pinMode(BUZZER_PIN, OUTPUT); digitalWrite(BUZZER_PIN, LOW);
  pinMode(STATUS_PIN, OUTPUT); digitalWrite(STATUS_PIN, LOW);
  pinMode(WIFI_PIN, OUTPUT);   digitalWrite(WIFI_PIN, LOW);
  for (uint8_t i = 0; i < NUM_RELAYS; ++i) { pinMode(RELAY_PINS[i], OUTPUT); digitalWrite(RELAY_PINS[i], LOW); }

  pinMode(BYPASS_PIN, INPUT_PULLUP);
  lastBypassState = digitalRead(BYPASS_PIN);
  applyBypassState(lastBypassState == LOW);
  pinMode(EXIT_SWITCH_PIN, INPUT_PULLUP);

  rfid.begin(9600);
  tagManager.begin();
  Serial.printf("Allowlist: %d tag(s)\n", tagManager.count());

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.begin(SSID, WIFI_PASSWORD);
  Serial.print("Connecting to WiFi");
  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 15000UL) { delay(250); Serial.print("."); }
  Serial.println();

  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("WiFi connected! IP: " + WiFi.localIP().toString());
    digitalWrite(WIFI_PIN, HIGH);

    if (MDNS.begin(HOST)) {
      Serial.println("mDNS started: " + String(HOST) + ".local");
    }

    syncTime();
  } else {
    Serial.println("WiFi failed — RFID still works offline, retrying in loop()");
  }

  mqtt.setCallback(handleMqttCommand);
  mqtt.setBufferSize(MQTT_BUFFER_BYTES);
  mqtt.setSocketTimeout(2);       // a stalled broker must not hold up the door

  Serial.println("Setup complete!");
}

void tickStatusLed() {
  unsigned long now = millis();
  if (now - previousBlinkMillis >= blinkInterval / 2) {
    previousBlinkMillis = now;
    ledState = !ledState;
    digitalWrite(STATUS_PIN, ledState ? HIGH : LOW);
  }
}

void loop() {
  handleWiFi();
  if (netServicesStarted) ArduinoOTA.handle();

  // A firmware write ends in a reboot. Don't start a door cycle we can't
  // finish, and don't drive the relay while flash is being rewritten.
  if (otaInProgress) { yield(); return; }

  bool currentBypass = (digitalRead(BYPASS_PIN) == LOW);
  if (currentBypass != lastBypassState) { applyBypassState(currentBypass); lastBypassState = currentBypass; }

  static bool lastExitState = HIGH;
  bool exitState = digitalRead(EXIT_SWITCH_PIN);
  if (exitState == LOW && lastExitState == HIGH && !bypassActive) openDoor();
  lastExitState = exitState;

  tickBuzzer();
  processRFID();
  tickDoorTimer();
  tickMqtt();
  tickStatusLed();
  yield();
}
