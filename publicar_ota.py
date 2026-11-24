#!/usr/bin/env python3
"""
publicar_ota.py - Publicador OTA para Focos C7 Studio (ESP8266 + Arduino IDE / arduino-cli)
Uso:
  python publicar_ota.py                 -> Sube version patch, compila si arduino-cli existe (o usa binario exportado), y publica a GitHub.
  python publicar_ota.py 1.0.2           -> Fuerza la version 1.0.2 y publica.
  python publicar_ota.py 1.0.2 ruta.bin  -> Usa un .bin ya exportado desde Arduino IDE sin recompilar.
"""

import os
import re
import sys
import json
import glob
import shutil
import base64
import socket
import subprocess
import concurrent.futures
import urllib.request
import urllib.error
from datetime import datetime

REPO_OWNER = "AlexGremory255"
REPO_NAME  = "focos-c7"
BRANCH     = "main"
FQBN       = "esp8266:esp8266:nodemcuv2:eesz=4M2M"


def get_project_dir():
    return os.path.dirname(os.path.abspath(__file__))


def load_token(project_dir):
    candidates = [
        os.path.join(project_dir, ".env"),
        os.path.abspath(os.path.join(project_dir, "..", "..", "PlatformIO", "Projects", "esp32 clock", ".env")),
    ]
    for env_file in candidates:
        if os.path.exists(env_file):
            with open(env_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("GITHUB_TOKEN="):
                        tok = line.split("=", 1)[1].strip().strip('"').strip("'")
                        if tok:
                            return tok
    return os.environ.get("GITHUB_TOKEN", "")


def find_arduino_cli():
    local_app = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        shutil.which("arduino-cli"),
        os.path.join(local_app, "Programs", "Arduino IDE", "resources", "app", "lib", "backend", "resources", "arduino-cli.exe"),
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


def get_current_version(ino_path):
    with open(ino_path, "r", encoding="utf-8") as f:
        content = f.read()
    m = re.search(r'#define\s+FIRMWARE_VERSION\s+"([^"]+)"', content)
    return m.group(1) if m else "1.0.0"


def bump_patch_version(ver):
    parts = [int(x) for x in ver.split(".")]
    while len(parts) < 3:
        parts.append(0)
    parts[2] += 1
    return f"{parts[0]}.{parts[1]}.{parts[2]}"


def set_version_in_ino(ino_path, new_ver):
    with open(ino_path, "r", encoding="utf-8") as f:
        content = f.read()
    updated = re.sub(
        r'(#define\s+FIRMWARE_VERSION\s+")[^"]+(")',
        rf'\g<1>{new_ver}\g<2>',
        content,
    )
    with open(ino_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(updated)


def sync_luces_txt(project_dir):
    ino_path = os.path.join(project_dir, "focos_c7", "focos_c7.ino")
    txt_path = os.path.join(project_dir, "luces.txt")
    shutil.copyfile(ino_path, txt_path)


def find_exported_bin(project_dir):
    patterns = [
        os.path.join(project_dir, "focos_c7", "build", "**", "*.bin"),
        os.path.join(project_dir, "focos_c7", "*.bin"),
        os.path.join(project_dir, "firmware.bin"),
    ]
    found = []
    for pat in patterns:
        found.extend(glob.glob(pat, recursive=True))
    found = [p for p in found if os.path.isfile(p) and not p.endswith(".elf")]
    if not found:
        return None
    found.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return found[0]


def discover_lan_devices():
    prefixes = ["192.168.68", "192.168.1", "192.168.0"]
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        pref = ".".join(local_ip.split(".")[:3])
        if pref and pref not in prefixes:
            prefixes.insert(0, pref)
    except Exception:
        pass

    found = []

    def probe_ip(ip):
        try:
            req = urllib.request.Request(f"http://{ip}/config", headers={"User-Agent": "FocosC7-Scanner"})
            with urllib.request.urlopen(req, timeout=0.6) as r:
                data = json.loads(r.read().decode("utf-8", errors="ignore"))
                if isinstance(data, dict) and data.get("device") == "focos-c7":
                    data["ip"] = ip
                    return data
        except Exception:
            pass
        return None

    candidates = [f"{p}.{i}" for p in prefixes for i in range(1, 255)]
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=64) as ex:
            for res in ex.map(probe_ip, candidates):
                if res:
                    found.append(res)
    except Exception:
        pass
    return found


def github_get_file_sha(path, token):
    url = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/contents/{path}?ref={BRANCH}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "FocosC7-OTA-Publisher",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8")).get("sha")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def github_upload_file(path, raw_bytes, commit_msg, token):
    sha = github_get_file_sha(path, token)
    url = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/contents/{path}"
    payload = {
        "message": commit_msg,
        "content": base64.b64encode(raw_bytes).decode("ascii"),
        "branch": BRANCH,
    }
    if sha:
        payload["sha"] = sha
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="PUT", headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "User-Agent": "FocosC7-OTA-Publisher",
    })
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    project_dir = get_project_dir()
    ino_path = os.path.join(project_dir, "focos_c7", "focos_c7.ino")
    cur_ver = get_current_version(ino_path)
    new_ver = sys.argv[1] if len(sys.argv) > 1 else bump_patch_version(cur_ver)
    explicit_bin = sys.argv[2] if len(sys.argv) > 2 else None

    print(f"[1/4] Actualizando FIRMWARE_VERSION: {cur_ver} -> {new_ver}")
    set_version_in_ino(ino_path, new_ver)
    sync_luces_txt(project_dir)

    bin_path = None
    if explicit_bin and os.path.exists(explicit_bin):
        bin_path = explicit_bin
        print(f"[2/4] Usando binario proporcionado: {bin_path}")
    else:
        acli = find_arduino_cli()
        if acli:
            print(f"[2/4] Compilando con arduino-cli ({FQBN})...")
            sketch_dir = os.path.join(project_dir, "focos_c7")
            res = subprocess.run([acli, "compile", "--fqbn", FQBN, "--export-binaries", sketch_dir], cwd=project_dir)
            if res.returncode != 0:
                print("[Error] Fallo la compilacion con arduino-cli.")
                sys.exit(1)
            bin_path = find_exported_bin(project_dir)
        else:
            bin_path = find_exported_bin(project_dir)
            print(f"[2/4] arduino-cli no encontrado; usando binario exportado por Arduino IDE: {bin_path}")

    if not bin_path or not os.path.exists(bin_path):
        print("[Error] No se encontro el archivo .bin. En Arduino IDE usa Programa -> Exportar binario compilado.")
        sys.exit(1)

    dst_bin = os.path.join(project_dir, "firmware.bin")
    if os.path.abspath(bin_path) != os.path.abspath(dst_bin):
        shutil.copyfile(bin_path, dst_bin)

    stamp = datetime.now().strftime("%d/%m/%Y %H:%M")
    changelog_list = []
    cl_file = os.path.join(project_dir, "CHANGELOG.txt")
    if os.path.exists(cl_file):
        with open(cl_file, "r", encoding="utf-8") as cf:
            for line in cf:
                c = line.strip().lstrip("-*•").strip()
                if c:
                    changelog_list.append(c)
    if not changelog_list:
        changelog_list = ["Mejoras en efectos, estabilidad Wi-Fi y actualizaciones OTA"]

    lan_devices = discover_lan_devices()
    ver_obj = {
        "version": new_ver,
        "build_time": stamp,
        "notes": f"Actualizacion v{new_ver} ({stamp})",
        "changelog": changelog_list,
        "url": f"https://{REPO_OWNER.lower()}.github.io/{REPO_NAME}/firmware.bin",
        "fallback_url": f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/{BRANCH}/firmware.bin",
        "devices": lan_devices,
    }
    ver_bytes = json.dumps(ver_obj, indent=2, ensure_ascii=False).encode("utf-8")
    with open(os.path.join(project_dir, "version.json"), "wb") as vf:
        vf.write(ver_bytes)

    manifest_bytes = json.dumps({
        "name": "Focos C7 Studio - ESP8266",
        "version": new_ver,
        "new_install_prompt_erase": True,
        "builds": [{"chipFamily": "ESP8266", "parts": [{"path": "firmware.bin", "offset": 0}]}],
    }, indent=2, ensure_ascii=False).encode("utf-8")
    with open(os.path.join(project_dir, "manifest.json"), "wb") as mf:
        mf.write(manifest_bytes)

    token = load_token(project_dir)
    if not token:
        print("[3/4] No se encontro GITHUB_TOKEN en .env; archivos locales actualizados sin subir a GitHub.")
        return

    print(f"[3/4] Subiendo firmware.bin, version.json, manifest.json y codigo a GitHub ({REPO_OWNER}/{REPO_NAME})...")
    with open(dst_bin, "rb") as f:
        github_upload_file("firmware.bin", f.read(), f"Publicar firmware v{new_ver}", token)
    github_upload_file("version.json", ver_bytes, f"Publicar version.json v{new_ver}", token)
    github_upload_file("manifest.json", manifest_bytes, f"Actualizar manifest v{new_ver}", token)

    for rel in ("index.html", "luces.txt", "focos_c7/focos_c7.ino", "CHANGELOG.txt"):
        p = os.path.join(project_dir, rel.replace("/", os.sep))
        if os.path.exists(p):
            with open(p, "rb") as f:
                github_upload_file(rel, f.read(), f"Actualizar {rel} (v{new_ver})", token)

    print(f"[4/4] LISTO! Version v{new_ver} publicada para OTA en {REPO_OWNER}/{REPO_NAME}.")


if __name__ == "__main__":
    main()
