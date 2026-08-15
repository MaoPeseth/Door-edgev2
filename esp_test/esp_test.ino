/*
 * ESP32 Door Control - MQTT Test Client
 * 
 * This sketch:
 * 1. Connects to PC's WiFi hotspot (DoorEdge-Network)
 * 2. Connects to MQTT broker (192.168.137.1:1883)
 * 3. Subscribes to MQTT topics
 * 4. Prints all messages to Serial Monitor
 * 
 * Upload to: ESP32 DevKit-C or similar
 * Baud Rate: 115200
 */

#include <WiFi.h>
#include <PubSubClient.h>

// ───────────────────────────────────────────────────────────────────────────
// CONFIG
// ───────────────────────────────────────────────────────────────────────────
const char* ssid = "seth";              // PC hotspot SSID
const char* password = "123456789";                 // PC hotspot password
const char* mqtt_broker = "10.4.70.127";         // PC's hotspot IP
const int mqtt_port = 1883;
const char* mqtt_client_id = "ESP32_Door_Control";

// MQTT Topics (from Door-Edge config.py)
const char* topic_unlock = "door/cmd/unlock";
const char* topic_denied = "door/access/denied";
const char* topic_health = "door/health";

// ───────────────────────────────────────────────────────────────────────────
// GLOBALS
// ───────────────────────────────────────────────────────────────────────────
WiFiClient espClient;
PubSubClient client(espClient);
unsigned long lastHealthPublish = 0;
const long healthPublishInterval = 5000; // Publish health every 5 seconds

// ───────────────────────────────────────────────────────────────────────────
// MQTT CALLBACK - Receives messages from Door-Edge
// ───────────────────────────────────────────────────────────────────────────
void onMqttMessage(char* topic, byte* payload, unsigned int length) {
  Serial.print("\n[MQTT] Message received on topic: ");
  Serial.println(topic);
  
  Serial.print("[MQTT] Payload (");
  Serial.print(length);
  Serial.print(" bytes): ");
  
  String message = "";
  for (unsigned int i = 0; i < length; i++) {
    Serial.print((char)payload[i]);
    message += (char)payload[i];
  }
  Serial.println();
  
  // Handle unlock command
  if (strcmp(topic, topic_unlock) == 0) {
    Serial.println("✅ [ACTION] UNLOCK DOOR!");
    Serial.println("  → People can access");
    // Future: digitalWrite(LOCK_PIN, HIGH);
  }
  
  // Handle denied message
  else if (strcmp(topic, topic_denied) == 0) {
    Serial.println("❌ [ACTION] ACCESS DENIED!");
    Serial.println("  Not recognize that person");
    // Future: digitalWrite(BUZZER_PIN, HIGH);
  }
}

// ───────────────────────────────────────────────────────────────────────────
// WiFi CONNECTION
// ───────────────────────────────────────────────────────────────────────────
void connectToWiFi() {
  Serial.print("\n[WiFi] Connecting to: ");
  Serial.println(ssid);
  
  WiFi.mode(WIFI_STA);
  WiFi.begin(ssid, password);
  
  int timeout = 20; // 20 seconds max
  while (WiFi.status() != WL_CONNECTED && timeout > 0) {
    delay(500);
    Serial.print(".");
    timeout--;
  }
  
  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("\n✅ [WiFi] Connected!");
    Serial.print("   IP Address: ");
    Serial.println(WiFi.localIP());
    Serial.print("   RSSI (Signal Strength): ");
    Serial.print(WiFi.RSSI());
    Serial.println(" dBm");
  } else {
    Serial.println("\n❌ [WiFi] Connection failed!");
    Serial.println("   Check SSID/Password or router range");
  }
}

// ───────────────────────────────────────────────────────────────────────────
// MQTT CONNECTION
// ───────────────────────────────────────────────────────────────────────────
void connectToMqtt() {
  Serial.print("\n[MQTT] Connecting to broker: ");
  Serial.print(mqtt_broker);
  Serial.print(":");
  Serial.println(mqtt_port);
  
  client.setServer(mqtt_broker, mqtt_port);
  client.setCallback(onMqttMessage);
  
  if (client.connect(mqtt_client_id)) {
    Serial.println("✅ [MQTT] Connected!");
    
    // Subscribe to topics from Door-Edge
    Serial.println("[MQTT] Subscribing to topics...");
    
    if (client.subscribe(topic_unlock)) {
      Serial.print("   ✓ Subscribed to: ");
      Serial.println(topic_unlock);
    }
    
    if (client.subscribe(topic_denied)) {
      Serial.print("   ✓ Subscribed to: ");
      Serial.println(topic_denied);
    }
    
    // Publish initial health message
    client.publish(topic_health, "{\"status\":\"online\",\"device\":\"ESP32_Door\"}");
    Serial.println("   ✓ Published health status");
    
  } else {
    Serial.print("❌ [MQTT] Connection failed! Error code: ");
    Serial.println(client.state());
    Serial.println("   Make sure MQTT broker (Mosquitto) is running on PC");
  }
}

// ───────────────────────────────────────────────────────────────────────────
// SETUP
// ───────────────────────────────────────────────────────────────────────────
void setup() {
  Serial.begin(115200);
  delay(2000); // Wait for serial to initialize
  
  Serial.println("\n\n");
  Serial.println("╔════════════════════════════════════════════════════╗");
  Serial.println("║  ESP32 Door Control - MQTT Test Client            ║");
  Serial.println("║  Connecting to: Door-Edge System                  ║");
  Serial.println("╚════════════════════════════════════════════════════╝");
  
  connectToWiFi();
  connectToMqtt();
}

// ───────────────────────────────────────────────────────────────────────────
// LOOP
// ───────────────────────────────────────────────────────────────────────────
void loop() {
  // Maintain MQTT connection
  if (!client.connected()) {
    if (WiFi.status() == WL_CONNECTED) {
      connectToMqtt();
    } else {
      connectToWiFi();
    }
    delay(5000); // Wait 5 seconds before retry
  } else {
    client.loop(); // Process MQTT messages
  }
  
  // Publish health status every 5 seconds
  unsigned long currentTime = millis();
  if (currentTime - lastHealthPublish >= healthPublishInterval) {
    lastHealthPublish = currentTime;
    
    // Create JSON health message
    String healthMsg = "{\"status\":\"online\",\"uptime\":" + String(currentTime / 1000) + "}";
    client.publish(topic_health, healthMsg.c_str());
    
    Serial.print("\n[MQTT] Health published | Uptime: ");
    Serial.print(currentTime / 1000);
    Serial.println(" seconds");
  }
}