#!/usr/bin/env python3
"""
most.py — временный мост Termux <-> Replit Agent
Запускай в Termux: python most.py
Выдаст URL cloudflare tunnel + токен — передай агенту.
"""
import http.server
import json
import subprocess
import os
import sys
import threading
import urllib.request
import time
import re
import stat

PORT = 8765
TOKEN = "replit-bridge-2026"

ARCH_MAP = {
    "aarch64": "arm64",
    "arm64":   "arm64",
    "armv7l":  "arm",
    "armv8l":  "arm",
    "x86_64":  "amd64",
    "i686":    "386",
}

def get_cloudflared():
    """Скачивает cloudflared если нет."""
    cf = os.path.join(os.path.expanduser("~"), ".local", "bin", "cloudflared")
    if os.path.isfile(cf):
        return cf

    # попробуем pkg
    try:
        subprocess.run(["pkg", "install", "-y", "cloudflared"], check=True,
                       capture_output=True)
        r = subprocess.run(["which", "cloudflared"], capture_output=True, text=True)
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass

    # скачиваем бинарь напрямую
    import platform
    machine = platform.machine().lower()
    arch = ARCH_MAP.get(machine, "arm64")
    url = f"https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-{arch}"
    os.makedirs(os.path.dirname(cf), exist_ok=True)
    print(f"[most] Скачиваю cloudflared ({arch}) ...", flush=True)
    urllib.request.urlretrieve(url, cf)
    os.chmod(cf, os.stat(cf).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return cf


class BridgeHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # тихо

    def send_json(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def check_token(self):
        return self.headers.get("X-Bridge-Token", "") == TOKEN

    def do_GET(self):
        if self.path == "/ping":
            self.send_json(200, {"status": "ok", "bridge": "termux"})
        else:
            self.send_json(404, {"error": "not found"})

    def do_POST(self):
        if not self.check_token():
            self.send_json(403, {"error": "forbidden"})
            return

        length = int(self.headers.get("Content-Length", 0))
        try:
            data = json.loads(self.rfile.read(length))
        except Exception:
            self.send_json(400, {"error": "bad json"})
            return

        if self.path == "/exec":
            cmd     = data.get("cmd", "")
            cwd     = data.get("cwd", os.path.expanduser("~"))
            timeout = int(data.get("timeout", 60))
            if not cmd:
                self.send_json(400, {"error": "no cmd"})
                return
            try:
                r = subprocess.run(cmd, shell=True, capture_output=True,
                                   text=True, cwd=cwd, timeout=timeout)
                self.send_json(200, {
                    "stdout": r.stdout, "stderr": r.stderr,
                    "returncode": r.returncode
                })
            except subprocess.TimeoutExpired:
                self.send_json(200, {"stdout": "", "stderr": "timeout", "returncode": -1})
            except Exception as e:
                self.send_json(500, {"error": str(e)})

        elif self.path == "/write":
            path    = data.get("path", "")
            content = data.get("content", "")
            if not path:
                self.send_json(400, {"error": "no path"})
                return
            try:
                os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
                with open(path, "w") as f:
                    f.write(content)
                self.send_json(200, {"ok": True})
            except Exception as e:
                self.send_json(500, {"error": str(e)})

        elif self.path == "/read":
            path = data.get("path", "")
            if not path:
                self.send_json(400, {"error": "no path"})
                return
            try:
                with open(os.path.expanduser(path), "r") as f:
                    self.send_json(200, {"content": f.read()})
            except Exception as e:
                self.send_json(500, {"error": str(e)})

        elif self.path == "/ls":
            path = data.get("path", os.path.expanduser("~"))
            try:
                entries = os.listdir(os.path.expanduser(path))
                self.send_json(200, {"entries": entries})
            except Exception as e:
                self.send_json(500, {"error": str(e)})

        else:
            self.send_json(404, {"error": "unknown endpoint"})


def start_server():
    server = http.server.HTTPServer(("127.0.0.1", PORT), BridgeHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server


def start_tunnel(cf_bin):
    """Запускает cloudflared и возвращает публичный URL."""
    log_path = os.path.join(os.path.expanduser("~"), "cf_bridge.log")
    log_f = open(log_path, "w")

    proc = subprocess.Popen(
        [cf_bin, "tunnel", "--url", f"http://127.0.0.1:{PORT}",
         "--no-autoupdate"],
        stdout=log_f, stderr=log_f
    )

    # ищем любой https URL на trycloudflare.com
    url_pattern = re.compile(rb"https://([A-Za-z0-9][A-Za-z0-9\-]*)\.(trycloudflare\.com)")
    timeout = time.time() + 45
    url = None

    while time.time() < timeout:
        time.sleep(0.5)
        log_f.flush()
        try:
            with open(log_path, "rb") as f:
                data = f.read()
            matches = url_pattern.findall(data)
            # фильтруем api.trycloudflare.com
            for sub, domain in matches:
                sub_str = sub.decode()
                if sub_str != "api":
                    url = f"https://{sub_str}.{domain.decode()}"
                    break
            if url:
                break
            # печатаем последние строки лога для диагностики
            lines = data.decode(errors="replace").strip().splitlines()
            if lines:
                print(f"[cf] {lines[-1]}", flush=True)
        except Exception:
            pass

    log_f.close()

    if not url:
        # показываем весь лог для диагностики
        try:
            with open(log_path, "r", errors="replace") as f:
                print("[cf-log]", f.read()[-2000:], flush=True)
        except Exception:
            pass

    return proc, url


def main():
    print("=" * 55, flush=True)
    print("  most.py — Termux Bridge", flush=True)
    print("=" * 55, flush=True)

    # 1. Запускаем HTTP сервер
    start_server()
    print(f"[most] HTTP сервер запущен на 127.0.0.1:{PORT}", flush=True)

    # 2. Получаем cloudflared
    try:
        cf = get_cloudflared()
    except Exception as e:
        print(f"[most] ОШИБКА: не удалось получить cloudflared: {e}", flush=True)
        sys.exit(1)
    print(f"[most] cloudflared: {cf}", flush=True)

    # 3. Запускаем туннель
    print("[most] Создаю cloudflare tunnel ...", flush=True)
    proc, url = start_tunnel(cf)

    if not url:
        print("[most] ОШИБКА: не удалось получить URL туннеля.", flush=True)
        proc.terminate()
        sys.exit(1)

    # 4. Выводим данные для агента
    print(flush=True)
    print("=" * 55, flush=True)
    print("  ПЕРЕДАЙ ЭТО АГЕНТУ:", flush=True)
    print("=" * 55, flush=True)
    print(f"  BRIDGE_URL   = {url}", flush=True)
    print(f"  BRIDGE_TOKEN = {TOKEN}", flush=True)
    print("=" * 55, flush=True)
    print(flush=True)
    print("[most] Мост работает. Ctrl+C для остановки.", flush=True)

    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        print("\n[most] Остановлен.", flush=True)


if __name__ == "__main__":
    main()
