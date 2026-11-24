#include <ESP8266WiFi.h>
#include <ESP8266WebServer.h>
#include <ESP8266mDNS.h>
#include <ESP8266HTTPClient.h>
#include <ESP8266httpUpdate.h>
#include <WiFiClientSecureBearSSL.h>
#include <WiFiUdp.h>
#include <DNSServer.h>
#include <EEPROM.h>
#include <lwip/dns.h>
#include <lwip/tcp.h>

// ============================================================================
//  Focos C7 Studio - Controlador Wi-Fi 8 Canales para ESP8266 (Arduino IDE)
//  Compatible con modulo ESP8266MOD (AI-Thinker ESP-12E / ESP-12F, 4 MB Flash)
//  En Arduino IDE funciona con: "NodeMCU 1.0 (ESP-12E Module)" o
//  "Generic ESP8266 Module" (Flash Size: 4MB FS:2MB OTA:~1019KB)
// ============================================================================

#define FIRMWARE_VERSION "1.0.3"

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

// Estudio Web (GitHub Pages), Cloud Radar / Relay y endpoints Cloud OTA
const char* STUDIO_URL       = "https://alexgremory255.github.io/focos-c7/";
const char* RADAR_TOPIC      = "focosc7_alexgremory255_radar_v1";
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
DNSServer dnsServer;

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

char deviceMacClean[13] = "";
char deviceCmdTopic[32] = "";
char deviceAckTopic[32] = "";
char ultimaReqId[24]    = "";

unsigned long ultimo = 0, ultimoDDP = 0, proxCambio = 0;
uint32_t paso = 0;
bool apActivo = false;
bool dnsCaptivoActivo = false;
bool mdnsActivo = false;

bool pendienteGuardar = false;
unsigned long tCambio = 0;
bool radarPendiente = false;
unsigned long tRadar = 0;
bool ackPendiente = false;
String ackJsonPayload;
bool otaPendiente = false;
bool otaDiferida = false;
unsigned long tOtaDiferida = 0;
bool reinicioPendiente = false;
unsigned long tReinicio = 0;

// Estado motor Cloud lwIP no bloqueante (DNS + Publisher + Suscriptor /raw)
ip_addr_t ntfyAddr;
bool ntfyIpValido = false;
bool dnsBuscando = false;
unsigned long tUltimoDns = 0;
unsigned long tDnsResuelto = 0;
uint8_t cloudFallas = 0;

struct tcp_pcb* pubPcb = nullptr;
String pubReqBuf;
size_t pubOffset = 0;
unsigned long tInicioPub = 0;
unsigned long tUltimoPubErr = 0;

struct tcp_pcb* cmdPcb = nullptr;
bool cmdConectado = false;
bool cmdHeadersOk = false;
bool cmdPrimeraLineaLeida = false;
bool cmdStatusOk = false;
uint8_t cmdHdrMatch = 0;
uint8_t cmdStatusLen = 0;
char cmdStatusBuf[32] = "";
uint16_t cmdLineaLen = 0;
char cmdLineaBuf[320] = "";
bool cmdPendiente = false;
char cmdPendienteBuf[320] = "";
unsigned long tInicioCmd = 0;
unsigned long tUltimaActividadCmd = 0;
unsigned long tUltimoCierreCmd = 0;

void programarRadar();
void programarAck(const String& json);
void cerrarConexionesCloud();

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

// ---------- Wi-Fi, mDNS, Portal Cautivo y Cloud Relay No Bloqueante ----------
void inicializarTopicsCloud() {
  String raw = WiFi.macAddress();
  int k = 0;
  for (unsigned int i = 0; i < raw.length() && k < 12; i++) {
    char c = raw.charAt(i);
    if ((c >= '0' && c <= '9') || (c >= 'A' && c <= 'F') || (c >= 'a' && c <= 'f')) {
      if (c >= 'a' && c <= 'f') c = (char)(c - 'a' + 'A');
      deviceMacClean[k++] = c;
    }
  }
  deviceMacClean[k] = '\0';
  if (k > 0) {
    snprintf(deviceCmdTopic, sizeof(deviceCmdTopic), "focosc7_cmd_%s", deviceMacClean);
    snprintf(deviceAckTopic, sizeof(deviceAckTopic), "focosc7_ack_%s", deviceMacClean);
  }
}

void destellarFisicos(uint8_t fisA, uint8_t fisB, int veces, int msOn, int msOff) {
  for (uint8_t i = 0; i < N; i++) {
    estadoLogico[i] = false;
    canalFisico(i, false);
  }
  for (int v = 0; v < veces; v++) {
    canalFisico(fisA, true);
    canalFisico(fisB, true);
    delay(msOn);
    canalFisico(fisA, false);
    canalFisico(fisB, false);
    delay(msOff);
  }
}

void programarRadar() {
  radarPendiente = true;
  tRadar = millis();
}

void programarAck(const String& json) {
  ackJsonPayload = json;
  ackPendiente = true;
}

void iniciarMDNS() {
  if (mdnsActivo || WiFi.status() != WL_CONNECTED) return;
  if (MDNS.begin(MDNS_NAME)) {
    MDNS.addService("http", "tcp", 80);
    MDNS.addServiceTxt("http", "tcp", "device", "focos-c7");
    mdnsActivo = true;
    Serial.printf("[mDNS] Activo: http://%s.local\n", MDNS_NAME);
  }
}

void iniciarAP() {
  WiFi.setAutoReconnect(false);
  WiFi.disconnect();
  WiFi.mode(WIFI_AP_STA); // Mantiene radio STA lista para escanear redes sin saltar de canal
  WiFi.softAP(AP_SSID, AP_PASS);
  dnsServer.start(53, "*", WiFi.softAPIP());
  dnsCaptivoActivo = true;
  apActivo = true;
  Serial.printf("[Wi-Fi] AP respaldo '%s' IP: %s\n", AP_SSID, WiFi.softAPIP().toString().c_str());
}

void vigilarWiFi() {
  static unsigned long t = 0;
  static bool estabaConectado = false;
  static unsigned long ultimoReintentoSTA = 0;
  static bool reintentandoSTA = false;

  if (millis() - t < 5000) return;
  t = millis();

  if (!tieneWifiValido()) {
    estabaConectado = false;
    mdnsActivo = false;
    cerrarConexionesCloud();
    if (!apActivo) iniciarAP();
    return;
  }

  if (WiFi.status() == WL_CONNECTED) {
    reintentandoSTA = false;
    if (apActivo && WiFi.softAPgetStationNum() == 0) {
      if (dnsCaptivoActivo) {
        dnsServer.stop();
        dnsCaptivoActivo = false;
      }
      WiFi.softAPdisconnect(true);
      WiFi.mode(WIFI_STA);
      apActivo = false;
      mdnsActivo = false;
      Serial.println("[Wi-Fi] Conectado a red local; AP de respaldo apagado.");
    }
    iniciarMDNS();
    if (!estabaConectado) {
      estabaConectado = true;
      programarRadar();
    }
  } else {
    estabaConectado = false;
    mdnsActivo = false;
    cerrarConexionesCloud();
    if (!apActivo) {
      iniciarAP();
      ultimoReintentoSTA = millis();
      reintentandoSTA = false;
    } else if (WiFi.softAPgetStationNum() > 0) {
      if (reintentandoSTA) {
        reintentandoSTA = false;
        WiFi.setAutoReconnect(false);
        WiFi.disconnect();
      }
    } else {
      if (!reintentandoSTA && (millis() - ultimoReintentoSTA > 60000)) {
        reintentandoSTA = true;
        ultimoReintentoSTA = millis();
        WiFi.setAutoReconnect(true);
        WiFi.begin(wifiSsid, wifiPass);
        Serial.println("[Wi-Fi] Reintento no bloqueante en segundo plano...");
      } else if (reintentandoSTA && (millis() - ultimoReintentoSTA > 15000)) {
        reintentandoSTA = false;
        WiFi.setAutoReconnect(false);
        WiFi.disconnect();
      }
    }
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
  otaDiferida = false;
  Serial.println("[OTA] Iniciando actualizacion Cloud OTA...");

  cerrarConexionesCloud();
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
    destellarFisicos(0, 4, 3, 180, 150);
    delay(250);
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
  if (deviceMacClean[0] == '\0') inicializarTopicsCloud();
  String ipAct = (WiFi.status() == WL_CONNECTED)
                   ? WiFi.localIP().toString()
                   : WiFi.softAPIP().toString();
  String ssidAct = (WiFi.status() == WL_CONNECTED)
                     ? WiFi.SSID()
                     : String(tieneWifiValido() ? wifiSsid : "");
  String json = "{";
  json += "\"device\":\"focos-c7\"";
  json += ",\"fw_ver\":\"" FIRMWARE_VERSION "\"";
  json += ",\"mac\":\"" + WiFi.macAddress() + "\"";
  if (deviceCmdTopic[0] != '\0') {
    json += ",\"cmd_topic\":\"" + String(deviceCmdTopic) + "\"";
    json += ",\"ack_topic\":\"" + String(deviceAckTopic) + "\"";
  }
  if (ultimaReqId[0] != '\0') {
    json += ",\"req_id\":\"" + escaparJson(String(ultimaReqId)) + "\"";
  }
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

String escanearRedesJson(int maxRedes = 12) {
  if (WiFi.getMode() == WIFI_AP) {
    WiFi.mode(WIFI_AP_STA);
    delay(40);
  }
  int total = WiFi.scanNetworks(false, false);
  int elegibles[24];
  int cant = 0;

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
    if (!repetidaMenor && cant < 24) {
      elegibles[cant++] = i;
    }
  }

  for (int a = 0; a < cant - 1; a++) {
    for (int b = a + 1; b < cant; b++) {
      if (WiFi.RSSI(elegibles[b]) > WiFi.RSSI(elegibles[a])) {
        int tmp = elegibles[a];
        elegibles[a] = elegibles[b];
        elegibles[b] = tmp;
      }
    }
  }

  int limite = (cant < maxRedes) ? cant : maxRedes;
  String lista = "[";
  for (int k = 0; k < limite; k++) {
    int idx = elegibles[k];
    if (k > 0) lista += ",";
    bool abierta = (WiFi.encryptionType(idx) == ENC_TYPE_NONE);
    lista += "{\"ssid\":\"" + escaparJson(WiFi.SSID(idx)) + "\",\"rssi\":" + String(WiFi.RSSI(idx)) + ",\"open\":" + String(abierta ? 1 : 0) + "}";
  }
  lista += "]";
  WiFi.scanDelete();
  return lista;
}

// ---------- Motor Cloud Asincrono lwIP (0 ms bloqueo en loop) ----------
static void cbDnsNtfy(const char* name, const ip_addr_t* ipaddr, void* arg) {
  (void)name;
  (void)arg;
  dnsBuscando = false;
  if (ipaddr) {
    ntfyAddr = *ipaddr;
    ntfyIpValido = true;
    tDnsResuelto = millis();
  } else {
    ntfyIpValido = false;
  }
}

void asegurarDnsNtfy() {
  if (dnsBuscando) return;
  if (ntfyIpValido && (millis() - tDnsResuelto < 300000UL)) return;
  if (tUltimoDns != 0 && (millis() - tUltimoDns < 10000UL)) return;
  tUltimoDns = millis();
  dnsBuscando = true;
  err_t res = dns_gethostbyname("ntfy.sh", &ntfyAddr, cbDnsNtfy, nullptr);
  if (res == ERR_OK) {
    dnsBuscando = false;
    ntfyIpValido = true;
    tDnsResuelto = millis();
  } else if (res != ERR_INPROGRESS) {
    dnsBuscando = false;
    ntfyIpValido = false;
  }
}

static err_t cerrarPubPcb(struct tcp_pcb* tpcb, bool abortar) {
  if (tpcb) {
    tcp_arg(tpcb, nullptr);
    tcp_recv(tpcb, nullptr);
    tcp_sent(tpcb, nullptr);
    tcp_err(tpcb, nullptr);
    if (tpcb == pubPcb) pubPcb = nullptr;
    pubReqBuf = "";
    pubOffset = 0;
    if (abortar || tcp_close(tpcb) != ERR_OK) {
      tcp_abort(tpcb);
      return ERR_ABRT;
    }
  } else {
    pubPcb = nullptr;
    pubReqBuf = "";
    pubOffset = 0;
  }
  return ERR_OK;
}

static err_t enviarChunkPub(struct tcp_pcb* tpcb) {
  while (pubOffset < pubReqBuf.length()) {
    u16_t avail = tcp_sndbuf(tpcb);
    if (avail == 0) break;
    size_t rem = pubReqBuf.length() - pubOffset;
    u16_t chunk = (avail < rem) ? avail : (u16_t)rem;
    err_t err = tcp_write(tpcb, pubReqBuf.c_str() + pubOffset, chunk, TCP_WRITE_FLAG_COPY);
    if (err == ERR_OK) {
      pubOffset += chunk;
    } else if (err == ERR_MEM) {
      break;
    } else {
      return cerrarPubPcb(tpcb, true);
    }
  }
  tcp_output(tpcb);
  return ERR_OK;
}

static err_t cbPubSent(void* arg, struct tcp_pcb* tpcb, u16_t len) {
  (void)arg;
  (void)len;
  return enviarChunkPub(tpcb);
}

static err_t cbPubRecv(void* arg, struct tcp_pcb* tpcb, struct pbuf* p, err_t err) {
  (void)arg;
  if (!p) {
    return cerrarPubPcb(tpcb, false);
  }
  if (err != ERR_OK) {
    pbuf_free(p);
    return cerrarPubPcb(tpcb, true);
  }
  tcp_recved(tpcb, p->tot_len);
  pbuf_free(p);
  return ERR_OK;
}

static void cbPubErr(void* arg, err_t err) {
  (void)arg;
  (void)err;
  pubPcb = nullptr;
  pubReqBuf = "";
  pubOffset = 0;
  tUltimoPubErr = millis();
  if (++cloudFallas >= 3) {
    ntfyIpValido = false;
    cloudFallas = 0;
  }
}

static err_t cbPubConnected(void* arg, struct tcp_pcb* tpcb, err_t err) {
  (void)arg;
  if (err != ERR_OK) {
    return cerrarPubPcb(tpcb, true);
  }
  cloudFallas = 0;
  return enviarChunkPub(tpcb);
}

void iniciarPublicacionHttp(const char* topic, const String& body) {
  if (!ntfyIpValido || pubPcb != nullptr || !topic || topic[0] == '\0') return;
  pubReqBuf = "";
  pubReqBuf.reserve(body.length() + 140);
  pubReqBuf = F("POST /");
  pubReqBuf += topic;
  pubReqBuf += F(" HTTP/1.0\r\nHost: ntfy.sh\r\nContent-Type: application/json\r\nContent-Length: ");
  pubReqBuf += String(body.length());
  pubReqBuf += F("\r\nConnection: close\r\n\r\n");
  pubReqBuf += body;
  pubOffset = 0;

  struct tcp_pcb* pcb = tcp_new();
  if (!pcb) {
    pubReqBuf = "";
    tUltimoPubErr = millis();
    return;
  }
  pubPcb = pcb;
  tInicioPub = millis();
  tcp_err(pcb, cbPubErr);
  tcp_recv(pcb, cbPubRecv);
  tcp_sent(pcb, cbPubSent);
  err_t cErr = tcp_connect(pcb, &ntfyAddr, 80, cbPubConnected);
  if (cErr != ERR_OK) {
    cerrarPubPcb(pcb, true);
    tUltimoPubErr = millis();
  }
}

static err_t cerrarCmdPcb(struct tcp_pcb* tpcb, bool abortar) {
  if (tpcb) {
    tcp_arg(tpcb, nullptr);
    tcp_recv(tpcb, nullptr);
    tcp_sent(tpcb, nullptr);
    tcp_err(tpcb, nullptr);
    if (tpcb == cmdPcb) cmdPcb = nullptr;
    cmdConectado = false;
    tUltimoCierreCmd = millis();
    if (abortar || tcp_close(tpcb) != ERR_OK) {
      tcp_abort(tpcb);
      return ERR_ABRT;
    }
  } else {
    cmdPcb = nullptr;
    cmdConectado = false;
    tUltimoCierreCmd = millis();
  }
  return ERR_OK;
}

static err_t cbCmdRecv(void* arg, struct tcp_pcb* tpcb, struct pbuf* p, err_t err) {
  (void)arg;
  if (!p) {
    return cerrarCmdPcb(tpcb, false);
  }
  if (err != ERR_OK) {
    pbuf_free(p);
    return cerrarCmdPcb(tpcb, true);
  }

  tUltimaActividadCmd = millis();
  bool badHttp = false;

  for (struct pbuf* q = p; q != nullptr && !badHttp; q = q->next) {
    const char* bytes = (const char*)q->payload;
    for (u16_t k = 0; k < q->len; k++) {
      char c = bytes[k];
      if (!cmdHeadersOk) {
        if (!cmdPrimeraLineaLeida) {
          if (c == '\n') {
            cmdStatusBuf[cmdStatusLen] = '\0';
            cmdPrimeraLineaLeida = true;
            cmdStatusOk = (strstr(cmdStatusBuf, " 200") != nullptr);
            if (!cmdStatusOk) {
              badHttp = true;
              break;
            }
          } else if (c != '\r' && cmdStatusLen < sizeof(cmdStatusBuf) - 1) {
            cmdStatusBuf[cmdStatusLen++] = c;
          }
        }
        if (c == '\r') {
          cmdHdrMatch = (cmdHdrMatch == 2) ? 3 : 1;
        } else if (c == '\n') {
          if (cmdHdrMatch == 1) cmdHdrMatch = 2;
          else if (cmdHdrMatch == 3) { cmdHdrMatch = 4; cmdHeadersOk = true; }
          else cmdHdrMatch = 0;
        } else {
          cmdHdrMatch = 0;
        }
      } else {
        if (c == '\r') continue;
        if (c == '\n') {
          cmdLineaBuf[cmdLineaLen] = '\0';
          if (strncmp(cmdLineaBuf, "cmd=1&", 6) == 0 && !cmdPendiente) {
            strlcpy(cmdPendienteBuf, cmdLineaBuf, sizeof(cmdPendienteBuf));
            cmdPendiente = true;
          }
          cmdLineaLen = 0;
        } else if (cmdLineaLen < sizeof(cmdLineaBuf) - 1) {
          cmdLineaBuf[cmdLineaLen++] = c;
        }
      }
    }
  }

  tcp_recved(tpcb, p->tot_len);
  pbuf_free(p);

  if (badHttp) {
    return cerrarCmdPcb(tpcb, true);
  }
  return ERR_OK;
}

static void cbCmdErr(void* arg, err_t err) {
  (void)arg;
  (void)err;
  cmdPcb = nullptr;
  cmdConectado = false;
  tUltimoCierreCmd = millis();
  if (++cloudFallas >= 3) {
    ntfyIpValido = false;
    cloudFallas = 0;
  }
}

static err_t cbCmdConnected(void* arg, struct tcp_pcb* tpcb, err_t err) {
  (void)arg;
  if (err != ERR_OK) {
    return cerrarCmdPcb(tpcb, true);
  }
  cloudFallas = 0;
  cmdConectado = true;
  cmdHeadersOk = false;
  cmdPrimeraLineaLeida = false;
  cmdStatusOk = false;
  cmdStatusLen = 0;
  cmdHdrMatch = 0;
  cmdLineaLen = 0;
  tUltimaActividadCmd = millis();

  String req;
  req.reserve(80);
  req = F("GET /");
  req += deviceCmdTopic;
  req += F("/raw HTTP/1.0\r\nHost: ntfy.sh\r\n\r\n");
  err_t w = tcp_write(tpcb, req.c_str(), req.length(), TCP_WRITE_FLAG_COPY);
  if (w != ERR_OK) {
    return cerrarCmdPcb(tpcb, true);
  }
  tcp_output(tpcb);
  return ERR_OK;
}

void iniciarSuscripcionCmd() {
  if (!ntfyIpValido || cmdPcb != nullptr || deviceCmdTopic[0] == '\0') return;
  struct tcp_pcb* pcb = tcp_new();
  if (!pcb) {
    tUltimoCierreCmd = millis();
    return;
  }
  cmdPcb = pcb;
  cmdConectado = false;
  tInicioCmd = millis();
  tcp_err(pcb, cbCmdErr);
  tcp_recv(pcb, cbCmdRecv);
  err_t cErr = tcp_connect(pcb, &ntfyAddr, 80, cbCmdConnected);
  if (cErr != ERR_OK) {
    cerrarCmdPcb(pcb, true);
  }
}

void cerrarConexionesCloud() {
  if (cmdPcb) cerrarCmdPcb(cmdPcb, true);
  if (pubPcb) cerrarPubPcb(pubPcb, true);
  cmdPendiente = false;
  ackPendiente = false;
  ackJsonPayload = "";
}

String urlDecodeSeg(const char* src, int len) {
  String out;
  out.reserve(len);
  for (int i = 0; i < len; i++) {
    char c = src[i];
    if (c == '+') {
      out += ' ';
    } else if (c == '%' && i + 2 < len) {
      auto hexVal = [](char h) -> int {
        if (h >= '0' && h <= '9') return h - '0';
        if (h >= 'a' && h <= 'f') return h - 'a' + 10;
        if (h >= 'A' && h <= 'F') return h - 'A' + 10;
        return -1;
      };
      int hi = hexVal(src[i + 1]);
      int lo = hexVal(src[i + 2]);
      if (hi >= 0 && lo >= 0) {
        out += (char)((hi << 4) | lo);
        i += 2;
      } else {
        out += c;
      }
    } else {
      out += c;
    }
  }
  return out;
}

void procesarComandoRemoto(const char* query) {
  if (!query || query[0] == '\0') return;

  bool hasPing = false, hasScanWifi = false, hasStartOta = false, hasReboot = false, hasResetWifi = false;
  bool hasAle = false, hasPat = false, hasVel = false, hasMin = false, hasMax = false, hasInv = false;
  int valAle = 0, valPat = 0, valVel = 150, valMin = 30, valMax = 60, valInv = 0;
  String reqId = "";

  const char* p = query;
  while (*p) {
    const char* amp = strchr(p, '&');
    int segLen = amp ? (int)(amp - p) : (int)strlen(p);
    const char* eq = (const char*)memchr(p, '=', segLen);
    if (eq) {
      int kLen = (int)(eq - p);
      int vLen = segLen - kLen - 1;
      String k = urlDecodeSeg(p, kLen);
      String v = urlDecodeSeg(eq + 1, vLen);
      if      (k == "req_id")     reqId = v;
      else if (k == "ping")       hasPing = true;
      else if (k == "scan_wifi")  hasScanWifi = true;
      else if (k == "start_ota")  hasStartOta = true;
      else if (k == "reboot")     hasReboot = true;
      else if (k == "reset_wifi") hasResetWifi = true;
      else if (k == "aleatorio")  { hasAle = true; valAle = v.toInt(); }
      else if (k == "patron")     { hasPat = true; valPat = v.toInt(); }
      else if (k == "velocidad")  { hasVel = true; valVel = v.toInt(); }
      else if (k == "ale_min")    { hasMin = true; valMin = v.toInt(); }
      else if (k == "ale_max")    { hasMax = true; valMax = v.toInt(); }
      else if (k == "invertir")   { hasInv = true; valInv = v.toInt(); }
    }
    if (!amp) break;
    p = amp + 1;
  }

  if (reqId.length() > 0 && strcmp(reqId.c_str(), ultimaReqId) == 0) {
    return; // Comando duplicado ya atendido
  }
  if (reqId.length() > 0) {
    strlcpy(ultimaReqId, reqId.c_str(), sizeof(ultimaReqId));
  }

  String idEsc = escaparJson(String(ultimaReqId));

  if (hasStartOta) {
    programarAck("{\"device\":\"focos-c7\",\"ok\":true,\"ota\":true,\"req_id\":\"" + idEsc + "\"}");
    otaDiferida = true;
    tOtaDiferida = millis();
    return;
  }

  if (hasReboot) {
    programarAck("{\"device\":\"focos-c7\",\"ok\":true,\"reboot\":true,\"req_id\":\"" + idEsc + "\"}");
    reinicioPendiente = true;
    tReinicio = millis();
    return;
  }

  if (hasResetWifi) {
    wifiEstadoCfg = WIFI_MAGIC_RESET;
    wifiSsid[0] = '\0';
    wifiPass[0] = '\0';
    guardar(true);
    programarAck("{\"device\":\"focos-c7\",\"ok\":true,\"reset_wifi\":true,\"req_id\":\"" + idEsc + "\"}");
    reinicioPendiente = true;
    tReinicio = millis();
    return;
  }

  if (hasScanWifi) {
    String nets = escanearRedesJson(12);
    programarAck("{\"device\":\"focos-c7\",\"type\":\"wifi_scan\",\"req_id\":\"" + idEsc + "\",\"networks\":" + nets + "}");
    return;
  }

  if (!hasPing) {
    bool cambioPatron = false;
    bool huboCambio = false;
    if (hasAle) {
      bool nuevoAle = (valAle == 1);
      if (nuevoAle != aleatorio) {
        aleatorio = nuevoAle;
        if (aleatorio) { cambioAleatorio(); cambioPatron = true; }
      }
      huboCambio = true;
    }
    if (hasPat && valPat >= 0 && valPat <= MAX_PATRON) {
      if (!hasAle) aleatorio = false;
      setPatron(valPat);
      cambioPatron = true;
      huboCambio = true;
    }
    if (hasVel) {
      velocidad = (uint16_t)constrain(valVel, 30, 1000);
      if (!hasAle) aleatorio = false;
      huboCambio = true;
    }
    if (hasMin || hasMax) {
      int mn = hasMin ? constrain(valMin, 5, 3600) : aleMin;
      int mx = hasMax ? constrain(valMax, 5, 3600) : aleMax;
      if (mx < mn) mx = mn;
      aleMin = (uint16_t)mn;
      aleMax = (uint16_t)mx;
      if (aleatorio) programarCambio();
      huboCambio = true;
    }
    if (hasInv) {
      bool inv = (valInv == 1);
      if (inv != invertirOrden) {
        invertirOrden = inv;
        aplicarOrden();
      }
      huboCambio = true;
    }
    if (cambioPatron && !ddpActivo()) {
      avanzar();
      ultimo = millis();
    }
    if (huboCambio) marcarGuardar();
  }

  programarAck(construirJsonConfig());
  programarRadar();
}

void gestionarCloud() {
  if (WiFi.status() != WL_CONNECTED || ddpActivo() || otaPendiente) {
    cerrarConexionesCloud();
    return;
  }

  if (deviceCmdTopic[0] == '\0') inicializarTopicsCloud();

  if (cmdPendiente) {
    char copia[320];
    strlcpy(copia, cmdPendienteBuf, sizeof(copia));
    cmdPendiente = false;
    procesarComandoRemoto(copia);
  }

  unsigned long ahora = millis();

  if (pubPcb && (ahora - tInicioPub > 5000UL)) {
    cerrarPubPcb(pubPcb, true);
    tUltimoPubErr = ahora;
  }

  if (cmdPcb) {
    if (!cmdConectado && (ahora - tInicioCmd > 8000UL)) {
      cerrarCmdPcb(cmdPcb, true);
    } else if (cmdConectado && (ahora - tUltimaActividadCmd > 95000UL)) {
      cerrarCmdPcb(cmdPcb, true);
    }
  }

  if (otaDiferida) {
    bool listoAck = (!ackPendiente && pubPcb == nullptr);
    if ((listoAck && (ahora - tOtaDiferida > 180UL)) || (ahora - tOtaDiferida > 700UL)) {
      otaDiferida = false;
      otaPendiente = true;
      return;
    }
  }

  asegurarDnsNtfy();
  if (!ntfyIpValido) return;

  if (!otaDiferida && !reinicioPendiente && cmdPcb == nullptr &&
      (tUltimoCierreCmd == 0 || (ahora - tUltimoCierreCmd > 6000UL))) {
    iniciarSuscripcionCmd();
  }

  if (pubPcb == nullptr && (tUltimoPubErr == 0 || (ahora - tUltimoPubErr > 1200UL))) {
    if (ackPendiente) {
      ackPendiente = false;
      String body = ackJsonPayload;
      ackJsonPayload = "";
      iniciarPublicacionHttp(deviceAckTopic, body);
    } else if (radarPendiente && !otaDiferida && !reinicioPendiente && (ahora - tRadar > 1800UL)) {
      radarPendiente = false;
      iniciarPublicacionHttp(RADAR_TOPIC, construirJsonConfig());
    }
  }
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
  programarRadar();

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
  String lista = escanearRedesJson(12);

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

// ---------- Pagina local ligera con escaner Wi-Fi integrado ----------
String escaparHtml(const String& s) {
  String out;
  out.reserve(s.length() + 8);
  for (unsigned int i = 0; i < s.length(); i++) {
    char c = s.charAt(i);
    if      (c == '&')  out += F("&amp;");
    else if (c == '<')  out += F("&lt;");
    else if (c == '>')  out += F("&gt;");
    else if (c == '"')  out += F("&quot;");
    else if (c == '\'') out += F("&#39;");
    else                out += c;
  }
  return out;
}

String bloqueConfigWifiHtml() {
  String w;
  w.reserve(900);
  w += F("<div class='card'><div style='margin-bottom:6px'><b>Configurar Wi-Fi (2.4 GHz)</b></div>"
         "<button type='button' class='btn' id='btn_scan' onclick='escanear()'>Buscar redes Wi-Fi cercanas</button>"
         "<div id='redes' style='margin:6px 0'></div>"
         "<form action='/wifi_local' method='POST'>"
         "<input id='w_ssid' name='wifi_ssid' placeholder='Nombre de tu red Wi-Fi' maxlength='32' required>"
         "<input id='w_pass' name='wifi_pass' type='password' placeholder='Contrasena (vacia si es abierta)' maxlength='64'>"
         "<label style='display:flex;align-items:center;gap:8px;font-size:13px;color:#94a3b8;margin:2px 0 8px'>"
         "<input type='checkbox' style='width:auto;margin:0' onchange=\"document.getElementById('w_pass').type=this.checked?'text':'password'\"> Mostrar contrasena</label>"
         "<button class='btn pri' type='submit'>Guardar Wi-Fi y reiniciar</button></form></div>");
  return w;
}

void paginaInicioLocal() {
  static const char* const NOMBRES[14] = {
    "0. Apagar", "1. Persecucion", "2. Ping-pong", "3. Alternado",
    "4. Llenar/vaciar", "5. Chispas", "6. Parpadeo", "7. Encender todo",
    "8. Por color", "9. Cortina", "10. Cometa triple", "11. Hueco viajero",
    "12. Choque dual", "13. Cascada espejo"
  };

  bool conectado = (WiFi.status() == WL_CONNECTED);
  String ipAct = conectado ? WiFi.localIP().toString() : WiFi.softAPIP().toString();
  String h;
  h.reserve(4096);
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
         "<div class='card'><h2 style='margin:0 0 6px'>Focos C7 &middot; Panel Local</h2><small>IP: ");
  h += ipAct;
  h += F(" &middot; Firmware v" FIRMWARE_VERSION " &middot; ");
  h += conectado ? ("Wi-Fi: " + escaparHtml(WiFi.SSID())) : String("Sin Wi-Fi (Modo AP)");
  if (apActivo) h += F(" | AP: FocosC7 (192.168.4.1)");
  if (ddpActivo()) h += F(" | DDP Activo");
  h += F("</small><a class='btn pri' style='margin-top:10px' href='");
  h += STUDIO_URL;
  h += F("?ip=");
  h += ipAct;
  h += F("' target='_blank' rel='noopener'>Abrir Estudio Web Completo (GitHub Pages)</a></div>");

  if (!conectado) {
    h += bloqueConfigWifiHtml();
  }

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

  if (conectado) {
    h += bloqueConfigWifiHtml();
  }

  h += F("<script>"
         "function escanear(){"
         "var b=document.getElementById('btn_scan'),box=document.getElementById('redes');"
         "b.disabled=true;b.textContent='Buscando redes...';"
         "fetch('/scan_wifi?t='+Date.now()).then(function(r){return r.json();}).then(function(arr){"
         "b.disabled=false;b.textContent='Buscar redes Wi-Fi cercanas';box.innerHTML='';"
         "if(!arr||!arr.length){box.innerHTML='<small>No se encontraron redes 2.4 GHz.</small>';return;}"
         "arr.sort(function(a,c){return c.rssi-a.rssi;});"
         "arr.forEach(function(n){"
         "var btn=document.createElement('button');btn.type='button';btn.className='btn';"
         "btn.style.cssText='text-align:left;display:flex;justify-content:space-between;align-items:center;padding:10px 12px;font-size:14px';"
         "var s1=document.createElement('span');s1.textContent=n.ssid;"
         "var s2=document.createElement('small');s2.textContent=(n.open?'Abierta':'Segura')+' · '+n.rssi+' dBm';"
         "btn.appendChild(s1);btn.appendChild(s2);"
         "btn.onclick=function(){document.getElementById('w_ssid').value=n.ssid;document.getElementById('w_pass').focus();};"
         "box.appendChild(btn);"
         "});"
         "}).catch(function(){b.disabled=false;b.textContent='Buscar redes Wi-Fi cercanas';box.innerHTML='<small>Error al escanear redes.</small>';});"
         "}"
         "</script></body></html>");

  enviarCabecerasCORS();
  server.send(200, "text/html; charset=utf-8", h);
}

// ---------- Setup & Loop ----------
void setup() {
  for (uint8_t i = 0; i < N; i++) {
    pinMode(PINES[i], INPUT);
    estadoFisico[i] = false;
    estadoLogico[i] = false;
  }

  Serial.begin(115200);

  WiFi.persistent(false);
  WiFi.hostname("Focos-C7");
  inicializarTopicsCloud();

  EEPROM.begin(128);
  randomSeed(ESP.getCycleCount() ^ analogRead(A0));
  cargar();

  // Mantener la serie apagada durante la conexion para distinguir claramente el resultado Wi-Fi
  if (tieneWifiValido()) {
    WiFi.mode(WIFI_STA);
    WiFi.setAutoReconnect(true);
    WiFi.setSleepMode(WIFI_NONE_SLEEP);
    WiFi.begin(wifiSsid, wifiPass);

    unsigned long t0 = millis();
    while (WiFi.status() != WL_CONNECTED && (millis() - t0 < 15000)) {
      delay(50);
    }

    if (WiFi.status() == WL_CONNECTED) {
      Serial.printf("\n[Wi-Fi] Conectado! IP: %s\n", WiFi.localIP().toString().c_str());
      destellarFisicos(1, 5, 3, 240, 200);   // 3 destellos verdes fisicos = Wi-Fi OK
      delay(450);
      iniciarMDNS();
      programarRadar();
    } else {
      Serial.println("\n[Wi-Fi] No conecto en 15s; iniciando AP FocosC7.");
      destellarFisicos(0, 4, 3, 240, 200);   // 3 destellos rojos fisicos = fallo Wi-Fi
      delay(450);
      iniciarAP();
    }
  } else {
    destellarFisicos(0, 4, 2, 240, 200);     // 2 destellos rojos fisicos = sin Wi-Fi configurado
    delay(450);
    iniciarAP();
  }

  // Iniciar la secuencia luminosa solo despues de anunciar el estado Wi-Fi
  if (aleatorio) cambioAleatorio(); else setPatron(patron);
  avanzar();
  ultimo = millis();

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
    marcarGuardar(); programarRadar(); redirigirLocal();
  });
  server.on("/v", []() {
    aleatorio = false;
    velocidad = (uint16_t)constrain(server.arg("ms").toInt(), 30, 1000);
    marcarGuardar(); programarRadar(); redirigirLocal();
  });
  server.on("/a", []() {
    aleatorio = (server.arg("on") == "1");
    if (aleatorio) { cambioAleatorio(); avanzar(); ultimo = millis(); }
    marcarGuardar(); programarRadar(); redirigirLocal();
  });
  server.on("/t", []() {
    int mn = constrain(server.arg("min").toInt(), 5, 3600);
    int mx = constrain(server.arg("max").toInt(), 5, 3600);
    if (mx < mn) mx = mn;
    aleMin = (uint16_t)mn; aleMax = (uint16_t)mx;
    if (aleatorio) programarCambio();
    marcarGuardar(); programarRadar(); redirigirLocal();
  });
  server.on("/wifi_local", HTTP_POST, []() {
    String s = server.arg("wifi_ssid");
    s.trim();
    if (s.length() > 0) {
      strlcpy(wifiSsid, s.c_str(), sizeof(wifiSsid));
      strlcpy(wifiPass, server.arg("wifi_pass").c_str(), sizeof(wifiPass));
      wifiEstadoCfg = WIFI_MAGIC_SET;
      guardar(true);
      String msg = F("<!DOCTYPE html><html><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
                     "<body style='background:#070b14;color:#e2e8f0;font-family:system-ui,sans-serif;padding:24px;line-height:1.5'>"
                     "<h3 style='color:#4ade80;margin-top:0'>Wi-Fi guardado</h3>"
                     "<p>Reiniciando para conectar a <b>");
      msg += escaparHtml(s);
      msg += F("</b>...</p>"
               "<p style='color:#94a3b8;font-size:14px'>Si conecta bien ver&aacute;s <b>3 destellos verdes</b> en la serie y aparecer&aacute; autom&aacute;ticamente en GitHub Pages.<br>"
               "Si falla la clave ver&aacute;s <b>3 destellos rojos</b> y volver&aacute; a abrirse la red <b>FocosC7</b>.</p>"
               "</body></html>");
      server.send(200, "text/html; charset=utf-8", msg);
      reinicioPendiente = true;
      tReinicio = millis();
    } else {
      redirigirLocal();
    }
  });
  server.onNotFound([]() {
    if (apActivo && WiFi.status() != WL_CONNECTED) {
      server.sendHeader("Location", String("http://") + WiFi.softAPIP().toString() + "/", true);
      server.send(302, "text/plain", "");
    } else {
      server.send(404, "text/plain", "Not found");
    }
  });

  server.begin();
}

void loop() {
  static bool eraDDP = false;

  if (dnsCaptivoActivo) dnsServer.processNextRequest();
  server.handleClient();
  if (mdnsActivo) MDNS.update();
  recibirDDP();
  guardarSiToca();
  vigilarWiFi();
  gestionarCloud();

  if (otaPendiente) {
    ejecutarCloudOTA();
    return;
  }

  if (reinicioPendiente && (millis() - tReinicio > 750)) {
    cerrarConexionesCloud();
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
