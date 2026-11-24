#include <ESP8266WiFi.h>
#include <ESP8266WebServer.h>
#include <ESP8266mDNS.h>
#include <ESP8266HTTPClient.h>
#include <ESP8266httpUpdate.h>
#include <WiFiClientSecureBearSSL.h>
#include <WiFiUdp.h>
#include <EEPROM.h>

// ============================================================================
//  Focos C7 Studio - Controlador Wi-Fi 8 Canales para ESP8266 (Arduino IDE)
//  Compatible con modulo ESP8266MOD (AI-Thinker ESP-12E / ESP-12F, 4 MB Flash)
//  En Arduino IDE funciona con: "NodeMCU 1.0 (ESP-12E Module)" o
//  "Generic ESP8266 Module" (Flash Size: 4MB FS:2MB OTA:~1019KB)
// ============================================================================

#define FIRMWARE_VERSION "1.0.1"

// Mapeo GPIO por si se compila con "Generic ESP8266 Module" en Arduino IDE
#ifndef D0
#define D0 16
#define D1 5
#define D2 4
#define D3 0
#define D4 2
#define D5 14
#define D6 12
#define D7 13
#endif

// Red Wi-Fi por defecto (opcional: tambien puedes configurarla desde la web)
const char* DEFAULT_WIFI_SSID = "TU_WIFI";
const char* DEFAULT_WIFI_PASS = "TU_CLAVE";

// Red propia de respaldo (si falla el Wi-Fi o al iniciar por primera vez)
const char* AP_SSID   = "FocosC7";
const char* AP_PASS   = "focos1234";      // minimo 8 caracteres
const char* MDNS_NAME = "focosc7";        // http://focosc7.local

// Estudio Web (GitHub Pages) y endpoints Cloud OTA
const char* STUDIO_URL       = "https://alexgremory255.github.io/focos-c7/";
const char* OTA_FIRMWARE_URL = "https://alexgremory255.github.io/focos-c7/firmware.bin";
const char* OTA_FALLBACK_URL = "https://raw.githubusercontent.com/AlexGremory255/focos-c7/main/firmware.bin";

// Pines fisicos (D0..D7) - logica activa en bajo con alta impedancia al apagar
const uint8_t PINES[8] = {D0, D1, D2, D3, D4, D5, D6, D7};
const uint8_t N = 8;
const uint8_t MAX_PATRON = 13;
const unsigned long DDP_TIMEOUT = 2000;   // ms sin paquetes DDP = vuelve a patrones
const uint16_t VELOCIDADES[] = {70, 100, 140, 190, 260, 340};

// Distribucion EEPROM (128 bytes)
const uint8_t EEPROM_MAGIC_V2  = 0xA7;
const uint8_t EEPROM_MAGIC_V1  = 0xA5;
const uint8_t WIFI_MAGIC_SET   = 0x5A;
const uint8_t WIFI_MAGIC_RESET = 0x5E;

ESP8266WebServer server(80);
WiFiUDP udp;

bool estadoLogico[8] = {false};
bool estadoFisico[8] = {false};
uint8_t patron = 1;
uint16_t velocidad = 150;
bool aleatorio = false;
uint16_t aleMin = 30, aleMax = 60;        // segundos
bool invertirOrden = false;

char wifiSsid[33] = "";
char wifiPass[65] = "";
uint8_t wifiEstadoCfg = 0;                // 0=default, WIFI_MAGIC_SET, WIFI_MAGIC_RESET

unsigned long ultimo = 0, ultimoDDP = 0, proxCambio = 0;
uint32_t paso = 0;
bool apActivo = false;
bool mdnsActivo = false;

bool pendienteGuardar = false;
unsigned long tCambio = 0;
bool otaPendiente = false;
bool reinicioPendiente = false;
unsigned long tReinicio = 0;

// ---------- Helpers ----------
bool tieneWifiValido() {
  if (wifiEstadoCfg == WIFI_MAGIC_RESET) return false;
  if (wifiSsid[0] == '\0') return false;
  if (strcmp(wifiSsid, "TU_WIFI") == 0) return false;
  return true;
}

String escaparJson(const String& s) {
  String out;
  out.reserve(s.length() + 8);
  for (unsigned int i = 0; i < s.length(); i++) {
    char c = s.charAt(i);
    if (c == '"')       out += "\\\"";
    else if (c == '\\') out += "\\\\";
    else if (c == '\n') out += "\\n";
    else if (c == '\r') out += "\\r";
    else if (c == '\t') out += "\\t";
    else if ((uint8_t)c >= 0x20) out += c;
  }
  return out;
}

uint8_t obtenerMascara() {
  uint8_t m = 0;
  for (uint8_t i = 0; i < N; i++) {
    if (estadoLogico[i]) m |= (1 << i);
  }
  return m;
}

// ---------- Memoria (EEPROM) ----------
void guardar(bool incluirWifi = false) {
  EEPROM.write(0, EEPROM_MAGIC_V2);
  EEPROM.write(1, patron);
  EEPROM.put(2, velocidad);
  EEPROM.write(4, aleatorio ? 1 : 0);
  EEPROM.put(5, aleMin);
  EEPROM.put(7, aleMax);
  EEPROM.write(9, invertirOrden ? 1 : 0);
  if (incluirWifi) {
    EEPROM.write(10, wifiEstadoCfg);
    for (int i = 0; i < 33; i++) EEPROM.write(11 + i, (uint8_t)wifiSsid[i]);
    for (int i = 0; i < 65; i++) EEPROM.write(44 + i, (uint8_t)wifiPass[i]);
  }
  EEPROM.commit();
}

void cargar() {
  strlcpy(wifiSsid, DEFAULT_WIFI_SSID, sizeof(wifiSsid));
  strlcpy(wifiPass, DEFAULT_WIFI_PASS, sizeof(wifiPass));
  wifiEstadoCfg = 0;

  uint8_t magic = EEPROM.read(0);
  if (magic != EEPROM_MAGIC_V1 && magic != EEPROM_MAGIC_V2) return;

  uint8_t p = EEPROM.read(1);
  if (p <= MAX_PATRON) patron = p;

  uint16_t v = 150;
  EEPROM.get(2, v);
  if (v >= 30 && v <= 1000) velocidad = v;

  aleatorio = (EEPROM.read(4) == 1);

  uint16_t mn = 30, mx = 60;
  EEPROM.get(5, mn);
  EEPROM.get(7, mx);
  if (mn >= 5 && mx >= mn && mx <= 3600) {
    aleMin = mn;
    aleMax = mx;
  }

  if (magic == EEPROM_MAGIC_V2) {
    invertirOrden = (EEPROM.read(9) == 1);
    uint8_t wMagic = EEPROM.read(10);
    if (wMagic == WIFI_MAGIC_SET) {
      wifiEstadoCfg = WIFI_MAGIC_SET;
      for (int i = 0; i < 32; i++) wifiSsid[i] = (char)EEPROM.read(11 + i);
      wifiSsid[32] = '\0';
      for (int i = 0; i < 64; i++) wifiPass[i] = (char)EEPROM.read(44 + i);
      wifiPass[64] = '\0';
    } else if (wMagic == WIFI_MAGIC_RESET) {
      wifiEstadoCfg = WIFI_MAGIC_RESET;
      wifiSsid[0] = '\0';
      wifiPass[0] = '\0';
    }
  }
}

void marcarGuardar() {
  pendienteGuardar = true;
  tCambio = millis();
}

void guardarSiToca() {
  if (pendienteGuardar && (millis() - tCambio > 3000)) {
    pendienteGuardar = false;
    guardar(false);
  }
}

// ---------- Salidas fisicas ----------
void canalFisico(uint8_t idxFisico, bool on) {
  if (idxFisico >= N) return;
  if (estadoFisico[idxFisico] == on) return;
  estadoFisico[idxFisico] = on;
  if (on) {
    pinMode(PINES[idxFisico], OUTPUT);
    digitalWrite(PINES[idxFisico], LOW);
  } else {
    pinMode(PINES[idxFisico], INPUT);     // flotante = apagado seguro para reles 5V
  }
}

void canal(uint8_t i, bool on) {
  if (i >= N) return;
  estadoLogico[i] = on;
  uint8_t fis = invertirOrden ? (N - 1 - i) : i;
  canalFisico(fis, on);
}

void aplicarOrden() {
  for (uint8_t i = 0; i < N; i++) {
    uint8_t fis = invertirOrden ? (N - 1 - i) : i;
    canalFisico(fis, estadoLogico[i]);
  }
}

void todos(bool on) {
  for (uint8_t i = 0; i < N; i++) canal(i, on);
}

void setPatron(int n) {
  if (n < 0 || n > MAX_PATRON) return;
  patron = (uint8_t)n;
  paso = 0;
  if (n == 0) todos(false);
}

// ---------- Modo aleatorio ----------
void programarCambio() {
  proxCambio = millis() + (unsigned long)random((long)aleMin * 1000L, (long)aleMax * 1000L + 1);
}

void cambioAleatorio() {
  uint8_t nuevo;
  do {
    nuevo = (uint8_t)random(1, MAX_PATRON + 1);
  } while (nuevo == patron);
  velocidad = VELOCIDADES[random(sizeof(VELOCIDADES) / sizeof(VELOCIDADES[0]))];
  setPatron(nuevo);
  programarCambio();
}

// ---------- Patrones (0..13) ----------
void avanzar() {
  switch (patron) {
    case 0:  // Apagar
      todos(false);
      break;

    case 1:  // Persecucion
      for (uint8_t i = 0; i < N; i++) canal(i, i == (uint8_t)(paso % 8));
      paso++;
      break;

    case 2: {  // Ping-pong
      int p = (int)(paso % 14);
      int pos = (p < 8) ? p : (14 - p);
      for (uint8_t i = 0; i < N; i++) canal(i, (int)i == pos);
      paso++;
      break;
    }

    case 3:  // Alternado
      for (uint8_t i = 0; i < N; i++) canal(i, ((i + paso) % 2) == 0);
      paso++;
      break;

    case 4: {  // Llenar y vaciar
      int k = (int)(paso % 16);
      for (uint8_t i = 0; i < N; i++) canal(i, (k < 8) ? ((int)i <= k) : ((int)i > k - 8));
      paso++;
      break;
    }

    case 5: {  // Chispas
      bool alguno = false;
      for (uint8_t i = 0; i < N; i++) {
        bool on = (random(3) == 0);
        if (on) alguno = true;
        canal(i, on);
      }
      if (!alguno) canal((uint8_t)random(8), true);
      paso++;
      break;
    }

    case 6:  // Parpadeo general
      todos((paso % 2) == 0);
      paso++;
      break;

    case 7:  // Todo encendido
      todos(true);
      break;

    case 8: {  // Agrupado por color (Rojos -> Verdes -> Azules -> Amarillos)
      uint8_t g = (uint8_t)(paso % 4);
      for (uint8_t i = 0; i < N; i++) canal(i, (i % 4) == g);
      paso++;
      break;
    }

    case 9: {  // Cortina (Centro <-> Orillas)
      int k = (int)(paso % 6);
      int d = (k <= 3) ? k : (6 - k);
      for (uint8_t i = 0; i < N; i++) canal(i, (int)i == (3 - d) || (int)i == (4 + d));
      paso++;
      break;
    }

    case 10: {  // Cometa triple (Tren continuo de 3 focos)
      int head = (int)(paso % 8);
      for (uint8_t i = 0; i < N; i++) {
        int dist = (head + 8 - (int)i) % 8;
        canal(i, dist < 3);
      }
      paso++;
      break;
    }

    case 11: {  // Hueco viajero (Todos encendidos menos 1 sombra que rebota)
      int p = (int)(paso % 14);
      int pos = (p < 8) ? p : (14 - p);
      for (uint8_t i = 0; i < N; i++) canal(i, (int)i != pos);
      paso++;
      break;
    }

    case 12: {  // Choque dual (Extremos chocan al centro y rebotan en onda)
      int s = (int)(paso % 6);
      for (uint8_t i = 0; i < N; i++) {
        bool on = false;
        if      (s == 0) on = (i == 0 || i == 7);
        else if (s == 1) on = (i == 1 || i == 6);
        else if (s == 2) on = (i == 2 || i == 5);
        else if (s == 3) on = (i == 3 || i == 4);
        else if (s == 4) on = (i >= 2 && i <= 5);
        else             on = (i >= 1 && i <= 6);
        canal(i, on);
      }
      paso++;
      break;
    }

    case 13: {  // Cascada espejo (Llena de puntas al centro y vacia al inverso)
      int s = (int)(paso % 8);
      for (uint8_t i = 0; i < N; i++) {
        bool on = false;
        if      (s == 0 || s == 6) on = (i == 0 || i == 7);
        else if (s == 1 || s == 5) on = (i <= 1 || i >= 6);
        else if (s == 2 || s == 4) on = (i <= 2 || i >= 5);
        else if (s == 3)           on = true;
        else                       on = false;
        canal(i, on);
      }
      paso++;
      break;
    }
  }
}

void refrescarSiToca() {
  if (aleatorio && (long)(millis() - proxCambio) >= 0) {
    cambioAleatorio();
    avanzar();
    ultimo = millis();
  }
  if (millis() - ultimo >= velocidad) {
    ultimo = millis();
    avanzar();
  }
}

// ---------- Wi-Fi y mDNS ----------
void iniciarMDNS() {
  if (mdnsActivo || WiFi.status() != WL_CONNECTED) return;
  if (MDNS.begin(MDNS_NAME)) {
    MDNS.addService("http", "tcp", 80);
    mdnsActivo = true;
    Serial.printf("[mDNS] Activo: http://%s.local\n", MDNS_NAME);
  }
}

void iniciarAP(bool mantenerSTA) {
  WiFi.mode(mantenerSTA ? WIFI_AP_STA : WIFI_AP);
  WiFi.softAP(AP_SSID, AP_PASS);
  apActivo = true;
  Serial.printf("[Wi-Fi] AP respaldo '%s' IP: %s\n", AP_SSID, WiFi.softAPIP().toString().c_str());
}

void vigilarWiFi() {
  static unsigned long t = 0;
  if (millis() - t < 15000) return;
  t = millis();

  if (!tieneWifiValido()) {
    if (!apActivo) iniciarAP(false);
    return;
  }

  if (WiFi.status() == WL_CONNECTED) {
    iniciarMDNS();
    if (apActivo && WiFi.softAPgetStationNum() == 0) {
      WiFi.softAPdisconnect(true);
      WiFi.mode(WIFI_STA);
      apActivo = false;
      Serial.println("[Wi-Fi] Conectado a red local; AP de respaldo apagado.");
    }
  } else if (!apActivo) {
    iniciarAP(true);
  }
}

// ---------- DDP (xLights) ----------
bool ddpActivo() {
  return ultimoDDP != 0 && (millis() - ultimoDDP < DDP_TIMEOUT);
}

void recibirDDP() {
  int sz = udp.parsePacket();
  if (sz <= 0) return;
  uint8_t buf[540];
  int n = udp.read(buf, sizeof(buf));
  if (n < 11) return;
  int ini = (buf[0] & 0x10) ? 14 : 10;   // con o sin timecode
  uint32_t offset = ((uint32_t)buf[4] << 24) | ((uint32_t)buf[5] << 16) |
                    ((uint32_t)buf[6] << 8) | buf[7];
  for (int k = ini; k < n; k++) {
    uint32_t ch = offset + (uint32_t)(k - ini);
    if (ch < N) canal((uint8_t)ch, buf[k] > 127);
  }
  ultimoDDP = millis();
}

// ---------- Cloud OTA (BearSSL + GitHub) ----------
void destellarTodos(int veces, int msOn, int msOff) {
  for (int v = 0; v < veces; v++) {
    todos(true);  delay(msOn);
    todos(false); delay(msOff);
  }
}

void ejecutarCloudOTA() {
  otaPendiente = false;
  Serial.println("[OTA] Iniciando actualizacion Cloud OTA...");

  udp.stop();
  if (mdnsActivo) { MDNS.end(); mdnsActivo = false; }
  WiFi.setSleepMode(WIFI_NONE_SLEEP);
  delay(80);

  todos(false);

  ESPhttpUpdate.setFollowRedirects(HTTPC_STRICT_FOLLOW_REDIRECTS);
  ESPhttpUpdate.rebootOnUpdate(false);
  ESPhttpUpdate.onProgress([](int cur, int total) {
    if (total <= 0) return;
    int encendidos = (int)(((long)cur * (long)N) / (long)total);
    if (encendidos < 1) encendidos = 1;
    if (encendidos > N) encendidos = N;
    for (uint8_t i = 0; i < N; i++) canal(i, (int)i < encendidos);
  });

  const char* urls[2]  = { OTA_FIRMWARE_URL, OTA_FALLBACK_URL };
  const char* hosts[2] = { "alexgremory255.github.io", "raw.githubusercontent.com" };
  bool exito = false;

  for (int intento = 0; intento < 2 && !exito; intento++) {
    BearSSL::WiFiClientSecure client;
    client.setInsecure();
    if (client.probeMaxFragmentLength(hosts[intento], 443, 1024)) {
      client.setBufferSizes(1024, 512);
    } else {
      client.setBufferSizes(16384, 512);
    }

    String fullUrl = String(urls[intento]) + "?t=" + String(millis());
    Serial.printf("[OTA] Intento %d -> %s (heap=%u)\n", intento + 1, hosts[intento], ESP.getFreeHeap());
    t_httpUpdate_return ret = ESPhttpUpdate.update(client, fullUrl);

    if (ret == HTTP_UPDATE_OK) {
      exito = true;
    } else if (ret == HTTP_UPDATE_NO_UPDATES) {
      Serial.println("[OTA] Sin actualizaciones reportadas.");
    } else {
      Serial.printf("[OTA] Error (%d): %s\n",
                    ESPhttpUpdate.getLastError(),
                    ESPhttpUpdate.getLastErrorString().c_str());
    }
  }

  if (exito) {
    Serial.println("[OTA] Actualizacion exitosa! Reiniciando...");
    destellarTodos(2, 160, 120);
    ESP.restart();
  } else {
    Serial.println("[OTA] Fallo la actualizacion; restaurando operacion normal.");
    todos(false);
    for (int k = 0; k < 2; k++) {
      canal(0, true); canal(7, true); delay(160);
      todos(false); delay(140);
    }
    udp.begin(4048);
    iniciarMDNS();
    setPatron(patron);
    avanzar();
    ultimo = millis();
  }
}

// ---------- Servidor Web, CORS y Puente HTTP ----------
void enviarCabecerasCORS() {
  server.sendHeader("Access-Control-Allow-Origin", "*");
  server.sendHeader("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
  server.sendHeader("Access-Control-Allow-Headers", "Content-Type, Access-Control-Request-Private-Network");
  server.sendHeader("Access-Control-Allow-Private-Network", "true");
  server.sendHeader("Connection", "close");
}

String construirJsonConfig() {
  String ipAct = (WiFi.status() == WL_CONNECTED)
                   ? WiFi.localIP().toString()
                   : WiFi.softAPIP().toString();
  String ssidAct = (WiFi.status() == WL_CONNECTED)
                     ? WiFi.SSID()
                     : String(tieneWifiValido() ? wifiSsid : "");
  String json = "{";
  json += "\"device\":\"focos-c7\"";
  json += ",\"fw_ver\":\"" FIRMWARE_VERSION "\"";
  json += ",\"ip\":\"" + ipAct + "\"";
  json += ",\"ssid\":\"" + escaparJson(ssidAct) + "\"";
  json += ",\"rssi\":" + String(WiFi.status() == WL_CONNECTED ? WiFi.RSSI() : 0);
  json += ",\"ap\":" + String(apActivo ? 1 : 0);
  json += ",\"ddp\":" + String(ddpActivo() ? 1 : 0);
  json += ",\"patron\":" + String(patron);
  json += ",\"velocidad\":" + String(velocidad);
  json += ",\"aleatorio\":" + String(aleatorio ? 1 : 0);
  json += ",\"ale_min\":" + String(aleMin);
  json += ",\"ale_max\":" + String(aleMax);
  json += ",\"invertir\":" + String(invertirOrden ? 1 : 0);
  json += ",\"mask\":" + String(obtenerMascara());
  json += ",\"heap\":" + String(ESP.getFreeHeap());
  json += ",\"flash_kb\":" + String(ESP.getFlashChipRealSize() / 1024);
  json += ",\"ota_kb\":" + String(ESP.getFreeSketchSpace() / 1024);
  json += ",\"ota_ok\":" + String((ESP.checkFlashConfig(false) && ESP.getFreeSketchSpace() > 480000) ? 1 : 0);
  json += "}";
  return json;
}

void enviarRespuestaBridge(bool saved) {
  String cfg = construirJsonConfig();
  String html = F("<!DOCTYPE html><html><head><meta charset='utf-8'><title>Sincronizando Focos C7</title>"
                  "<style>body{background:#06080f;color:#4ade80;font-family:system-ui,sans-serif;display:flex;"
                  "align-items:center;justify-content:center;height:100vh;margin:0;font-weight:600}</style>"
                  "</head><body><div>&#10003; Focos C7 sincronizados</div><script>var cfg=");
  html += cfg;
  html += F(";try{if(window.opener){window.opener.postMessage({type:'focos_c7_bridge',ok:true,saved:");
  html += (saved ? F("true") : F("false"));
  html += F(",data:cfg},'*');}}catch(e){}setTimeout(function(){window.close();},70);</script></body></html>");
  enviarCabecerasCORS();
  server.send(200, "text/html; charset=utf-8", html);
}

void manejarOptions() {
  enviarCabecerasCORS();
  server.send(204, "text/plain", "");
}

void redirigirLocal() {
  server.sendHeader("Location", "/");
  server.sendHeader("Connection", "close");
  server.send(303, "text/plain", "");
}

void manejarConfigGet() {
  if (server.hasArg("patron") || server.hasArg("velocidad") || server.hasArg("aleatorio") ||
      server.hasArg("ale_min") || server.hasArg("ale_max") || server.hasArg("invertir") ||
      server.hasArg("save_wifi") || server.hasArg("reset_wifi") ||
      server.hasArg("start_ota") || server.hasArg("reboot")) {
    manejarConfigPost();
    return;
  }
  enviarCabecerasCORS();
  server.send(200, "application/json", construirJsonConfig());
}

void manejarConfigPost() {
  bool isBridge = server.hasArg("bridge") || (server.uri() == "/bridge");

  if (server.hasArg("start_ota")) {
    if (isBridge) enviarRespuestaBridge(true);
    else { enviarCabecerasCORS(); server.send(200, "application/json", "{\"ok\":true,\"ota\":true}"); }
    otaPendiente = true;
    return;
  }

  if (server.hasArg("reboot")) {
    if (isBridge) enviarRespuestaBridge(true);
    else { enviarCabecerasCORS(); server.send(200, "application/json", "{\"ok\":true,\"reboot\":true}"); }
    reinicioPendiente = true;
    tReinicio = millis();
    return;
  }

  if (server.hasArg("reset_wifi")) {
    wifiEstadoCfg = WIFI_MAGIC_RESET;
    wifiSsid[0] = '\0';
    wifiPass[0] = '\0';
    guardar(true);
    if (isBridge) enviarRespuestaBridge(true);
    else { enviarCabecerasCORS(); server.send(200, "application/json", "{\"ok\":true,\"reset_wifi\":true}"); }
    reinicioPendiente = true;
    tReinicio = millis();
    return;
  }

  if (server.hasArg("save_wifi") && server.hasArg("wifi_ssid")) {
    String nuevoSsid = server.arg("wifi_ssid");
    nuevoSsid.trim();
    if (nuevoSsid.length() > 0) {
      String nuevaPass = server.arg("wifi_pass");
      strlcpy(wifiSsid, nuevoSsid.c_str(), sizeof(wifiSsid));
      strlcpy(wifiPass, nuevaPass.c_str(), sizeof(wifiPass));
      wifiEstadoCfg = WIFI_MAGIC_SET;
      guardar(true);
      if (isBridge) enviarRespuestaBridge(true);
      else { enviarCabecerasCORS(); server.send(200, "application/json", "{\"ok\":true,\"wifi\":true}"); }
      reinicioPendiente = true;
      tReinicio = millis();
      return;
    }
  }

  bool cambioPatron = false;
  if (server.hasArg("aleatorio")) {
    bool nuevoAle = (server.arg("aleatorio").toInt() == 1);
    if (nuevoAle != aleatorio) {
      aleatorio = nuevoAle;
      if (aleatorio) { cambioAleatorio(); cambioPatron = true; }
    }
  }
  if (server.hasArg("patron")) {
    int p = server.arg("patron").toInt();
    if (p >= 0 && p <= MAX_PATRON) {
      if (!server.hasArg("aleatorio")) aleatorio = false;
      setPatron(p);
      cambioPatron = true;
    }
  }
  if (server.hasArg("velocidad")) {
    velocidad = (uint16_t)constrain(server.arg("velocidad").toInt(), 30, 1000);
    if (!server.hasArg("aleatorio")) aleatorio = false;
  }
  if (server.hasArg("ale_min") || server.hasArg("ale_max")) {
    int mn = server.hasArg("ale_min") ? constrain(server.arg("ale_min").toInt(), 5, 3600) : aleMin;
    int mx = server.hasArg("ale_max") ? constrain(server.arg("ale_max").toInt(), 5, 3600) : aleMax;
    if (mx < mn) mx = mn;
    aleMin = (uint16_t)mn;
    aleMax = (uint16_t)mx;
    if (aleatorio) programarCambio();
  }
  if (server.hasArg("invertir")) {
    bool inv = (server.arg("invertir").toInt() == 1);
    if (inv != invertirOrden) {
      invertirOrden = inv;
      aplicarOrden();
    }
  }

  if (cambioPatron && !ddpActivo()) {
    avanzar();
    ultimo = millis();
  }

  marcarGuardar();

  if (isBridge) enviarRespuestaBridge(true);
  else {
    enviarCabecerasCORS();
    server.send(200, "application/json", construirJsonConfig());
  }
}

void manejarBridge() {
  if (server.hasArg("patron") || server.hasArg("velocidad") || server.hasArg("aleatorio") ||
      server.hasArg("ale_min") || server.hasArg("ale_max") || server.hasArg("invertir") ||
      server.hasArg("save_wifi") || server.hasArg("reset_wifi") ||
      server.hasArg("start_ota") || server.hasArg("reboot")) {
    manejarConfigPost();
    return;
  }
  enviarRespuestaBridge(false);
}

void manejarScanWifi() {
  int total = WiFi.scanNetworks();
  String lista = "[";
  bool primero = true;

  for (int i = 0; i < total; i++) {
    String s = WiFi.SSID(i);
    if (s.length() == 0) continue;
    int rssi = WiFi.RSSI(i);

    bool repetidaMenor = false;
    for (int j = 0; j < total; j++) {
      if (j == i) continue;
      if (WiFi.SSID(j) == s) {
        int rj = WiFi.RSSI(j);
        if (rj > rssi || (rj == rssi && j < i)) {
          repetidaMenor = true;
          break;
        }
      }
    }
    if (repetidaMenor) continue;

    if (!primero) lista += ",";
    primero = false;
    bool abierta = (WiFi.encryptionType(i) == ENC_TYPE_NONE);
    lista += "{\"ssid\":\"" + escaparJson(s) + "\",\"rssi\":" + String(rssi) + ",\"open\":" + String(abierta ? 1 : 0) + "}";
  }
  lista += "]";
  WiFi.scanDelete();

  if (server.hasArg("bridge")) {
    String html = F("<!DOCTYPE html><html><head><meta charset='utf-8'></head><body><script>var nets=");
    html += lista;
    html += F(";try{if(window.opener){window.opener.postMessage({type:'focos_c7_wifi_scan',networks:nets},'*');}}catch(e){}setTimeout(function(){window.close();},70);</script></body></html>");
    enviarCabecerasCORS();
    server.send(200, "text/html; charset=utf-8", html);
    return;
  }

  enviarCabecerasCORS();
  server.send(200, "application/json", lista);
}

// ---------- Pagina local ligera de respaldo ----------
void paginaInicioLocal() {
  static const char* const NOMBRES[14] = {
    "0. Apagar", "1. Persecucion", "2. Ping-pong", "3. Alternado",
    "4. Llenar/vaciar", "5. Chispas", "6. Parpadeo", "7. Encender todo",
    "8. Por color", "9. Cortina", "10. Cometa triple", "11. Hueco viajero",
    "12. Choque dual", "13. Cascada espejo"
  };

  String ipAct = (WiFi.status() == WL_CONNECTED) ? WiFi.localIP().toString() : WiFi.softAPIP().toString();
  String h;
  h.reserve(2600);
  h += F("<!DOCTYPE html><html lang='es'><head><meta charset='utf-8'>"
         "<meta name='viewport' content='width=device-width,initial-scale=1'>"
         "<title>Focos C7 Local</title>"
         "<style>"
         "body{margin:0;padding:16px;background:#070b14;color:#f1f5f9;font-family:system-ui,sans-serif;max-width:520px;margin-inline:auto}"
         ".card{background:#111827;border:1px solid #1f2937;border-radius:16px;padding:14px;margin-bottom:12px}"
         ".btn{display:block;width:100%;padding:12px;margin:6px 0;border-radius:12px;border:1px solid #334155;"
         "background:#1e293b;color:#f8fafc;font-size:15px;font-weight:600;text-align:center;text-decoration:none;cursor:pointer;box-sizing:border-box}"
         ".pri{background:linear-gradient(135deg,#2563eb,#7c3aed);border-color:#60a5fa}"
         ".act{background:#0f766e;border-color:#2dd4bf}"
         ".grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}"
         "input{width:100%;padding:10px;margin:4px 0 8px;border-radius:10px;border:1px solid #334155;background:#0b1220;color:#fff;box-sizing:border-box}"
         "small{color:#94a3b8}"
         "</style></head><body>"
         "<div class='card'><h2 style='margin:0 0 6px'>Focos C7 · Panel Local</h2><small>IP: ");
  h += ipAct;
  h += F(" · Firmware v" FIRMWARE_VERSION " · ");
  h += (WiFi.status() == WL_CONNECTED) ? ("Wi-Fi: " + escaparJson(WiFi.SSID())) : String("Sin Wi-Fi");
  if (apActivo) h += F(" | AP: FocosC7 (192.168.4.1)");
  h += ddpActivo() ? F(" | DDP Activo") : F("");
  h += F("</small><a class='btn pri' style='margin-top:10px' href='");
  h += STUDIO_URL;
  h += F("?ip=");
  h += ipAct;
  h += F("' target='_blank' rel='noopener'>Abrir Estudio Web Completo (GitHub Pages)</a></div>");

  h += F("<div class='card'><div style='margin-bottom:6px'><b>Control rapido</b></div>"
         "<a class='btn ");
  h += aleatorio ? F("act") : F("");
  h += F("' href='/a?on=");
  h += aleatorio ? F("0") : F("1");
  h += F("'>Aleatorio: ");
  h += aleatorio ? F("ACTIVADO") : F("apagado");
  h += F("</a><div class='grid'>"
         "<a class='btn' href='/v?ms=80'>Rapida</a>"
         "<a class='btn' href='/v?ms=150'>Media</a>"
         "<a class='btn' href='/v?ms=300'>Lenta</a>"
         "<a class='btn' href='/p?n=0'>Apagar</a></div></div>");

  h += F("<div class='card'><div style='margin-bottom:6px'><b>Patrones (0-13)</b></div><div class='grid'>");
  for (int i = 0; i <= MAX_PATRON; i++) {
    h += F("<a class='btn ");
    if (!aleatorio && patron == i) h += F("act");
    h += F("' href='/p?n=");
    h += String(i);
    h += F("'>");
    h += NOMBRES[i];
    h += F("</a>");
  }
  h += F("</div></div>");

  h += F("<div class='card'><div style='margin-bottom:6px'><b>Configurar Wi-Fi</b></div>"
         "<form action='/wifi_local' method='POST'>"
         "<input name='wifi_ssid' placeholder='Nombre de tu red Wi-Fi' required>"
         "<input name='wifi_pass' type='password' placeholder='Contrasena (vacia si es abierta)'>"
         "<button class='btn pri' type='submit'>Guardar Wi-Fi y reiniciar</button></form></div>"
         "</body></html>");

  enviarCabecerasCORS();
  server.send(200, "text/html; charset=utf-8", h);
}

// ---------- Setup & Loop ----------
void setup() {
  Serial.begin(115200);
  for (uint8_t i = 0; i < N; i++) pinMode(PINES[i], INPUT);

  EEPROM.begin(128);
  randomSeed(ESP.getCycleCount() ^ analogRead(A0));
  cargar();

  // Aplicar patron desde el primer milisegundo antes de intentar Wi-Fi
  if (aleatorio) cambioAleatorio(); else setPatron(patron);
  avanzar();
  ultimo = millis();

  if (tieneWifiValido()) {
    WiFi.mode(WIFI_STA);
    WiFi.setAutoReconnect(true);
    WiFi.setSleepMode(WIFI_NONE_SLEEP);
    WiFi.begin(wifiSsid, wifiPass);

    unsigned long t0 = millis();
    while (WiFi.status() != WL_CONNECTED && (millis() - t0 < 8000)) {
      refrescarSiToca();
      delay(40);
    }

    if (WiFi.status() == WL_CONNECTED) {
      Serial.printf("\n[Wi-Fi] Conectado! IP: %s\n", WiFi.localIP().toString().c_str());
      iniciarMDNS();
    } else {
      iniciarAP(true);
    }
  } else {
    iniciarAP(false);
  }

  udp.begin(4048);

  // API JSON + CORS + Puente HTTP para GitHub Pages
  server.on("/",           HTTP_GET,     paginaInicioLocal);
  server.on("/config",     HTTP_GET,     manejarConfigGet);
  server.on("/config",     HTTP_POST,    manejarConfigPost);
  server.on("/config",     HTTP_OPTIONS, manejarOptions);
  server.on("/bridge",     HTTP_GET,     manejarBridge);
  server.on("/bridge",     HTTP_POST,    manejarBridge);
  server.on("/scan_wifi",  HTTP_GET,     manejarScanWifi);
  server.on("/scan_wifi",  HTTP_OPTIONS, manejarOptions);

  // Rutas simples de compatibilidad para el panel local
  server.on("/p", []() {
    aleatorio = false;
    setPatron(server.arg("n").toInt());
    avanzar(); ultimo = millis();
    marcarGuardar(); redirigirLocal();
  });
  server.on("/v", []() {
    aleatorio = false;
    velocidad = (uint16_t)constrain(server.arg("ms").toInt(), 30, 1000);
    marcarGuardar(); redirigirLocal();
  });
  server.on("/a", []() {
    aleatorio = (server.arg("on") == "1");
    if (aleatorio) { cambioAleatorio(); avanzar(); ultimo = millis(); }
    marcarGuardar(); redirigirLocal();
  });
  server.on("/t", []() {
    int mn = constrain(server.arg("min").toInt(), 5, 3600);
    int mx = constrain(server.arg("max").toInt(), 5, 3600);
    if (mx < mn) mx = mn;
    aleMin = (uint16_t)mn; aleMax = (uint16_t)mx;
    if (aleatorio) programarCambio();
    marcarGuardar(); redirigirLocal();
  });
  server.on("/wifi_local", HTTP_POST, []() {
    String s = server.arg("wifi_ssid");
    s.trim();
    if (s.length() > 0) {
      strlcpy(wifiSsid, s.c_str(), sizeof(wifiSsid));
      strlcpy(wifiPass, server.arg("wifi_pass").c_str(), sizeof(wifiPass));
      wifiEstadoCfg = WIFI_MAGIC_SET;
      guardar(true);
      server.send(200, "text/html; charset=utf-8",
                  "<!DOCTYPE html><html><body style='background:#070b14;color:#4ade80;font-family:sans-serif;padding:24px'>"
                  "<h3>Wi-Fi guardado. Reiniciando Focos C7...</h3></body></html>");
      reinicioPendiente = true;
      tReinicio = millis();
    } else {
      redirigirLocal();
    }
  });

  server.begin();
}

void loop() {
  static bool eraDDP = false;

  server.handleClient();
  if (mdnsActivo) MDNS.update();
  recibirDDP();
  guardarSiToca();
  vigilarWiFi();

  if (otaPendiente) {
    ejecutarCloudOTA();
    return;
  }

  if (reinicioPendiente && (millis() - tReinicio > 350)) {
    ESP.restart();
    return;
  }

  bool ahora = ddpActivo();
  if (eraDDP && !ahora) {
    setPatron(patron);
    avanzar();
    ultimo = millis();
  }
  eraDDP = ahora;

  if (!ahora) {
    refrescarSiToca();
  }
}
