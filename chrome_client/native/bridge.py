"""Private Line Chrome native messaging bridge (Python 3 standard library).

Accepts only messages from the Chrome Native Messaging protocol. The installer
restricts allowed_origins to one extension ID. Never logs or returns VLESS
credentials. Xray is bound to 127.0.0.1 only.
"""
from __future__ import annotations

import base64
import ipaddress
import json
import os
import pathlib
import re
import signal
import socket
import ssl
import struct
import subprocess
import sys
import time
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

HOST_NAME = "ru.privateline.chrome"
SOCKS_PORT = 19590
MAX_MESSAGE = 32768


def home() -> pathlib.Path:
    if os.name == "nt":
        root = os.environ.get("LOCALAPPDATA", str(pathlib.Path.home()))
        directory = pathlib.Path(root) / "PrivateLineChrome"
    else:
        directory = pathlib.Path.home() / ".config" / "private-line-chrome"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory


def atomic_save(path: pathlib.Path, data: str) -> None:
    import tempfile
    descriptor, tmp = tempfile.mkstemp(prefix=".private-line-", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def config_from_vless(raw: str) -> dict:
    if not isinstance(raw, str) or len(raw) > 8192:
        raise ValueError("Слишком длинный ключ")
    p = urlsplit(raw.strip())
    if p.scheme != "vless" or p.password or not p.username:
        raise ValueError("Требуется ссылка VLESS Reality")
    try:
        uid = str(UUID(p.username))
        address = str(ipaddress.ip_address(p.hostname or ""))
        if not ipaddress.ip_address(address).is_global:
            raise ValueError("Адрес ноды должен быть публичным IP")
        port = p.port
        if not port or not (1 <= port <= 65535):
            raise ValueError("Неверный порт ноды")
    except (ValueError, TypeError) as exc:
        raise ValueError("Неверный IP, порт или UUID VLESS") from exc

    query = parse_qs(p.query, keep_blank_values=True)
    def one(name: str, default: str = "") -> str:
        values = query.get(name)
        if values and len(values) != 1:
            raise ValueError("Повторяющийся параметр " + name)
        return values[0] if values else default

    if (one("security") != "reality" or one("flow") != "xtls-rprx-vision"
            or one("type", "tcp") not in ("tcp", "raw")
            or one("encryption", "none") != "none"
            or one("fp", "chrome") != "chrome"
            or one("headerType", "none") not in ("", "none")):
        raise ValueError("Ключ несовместим с VLESS Reality/Vision (security/flow/type/fp)")
    public_key = one("pbk")
    short_id = one("sid")
    server_name = one("sni")
    if not re.fullmatch(r"[A-Za-z0-9_-]{43,44}", public_key):
        raise ValueError("Некорректный Reality public key")
    try:
        key_bytes = base64.urlsafe_b64decode(public_key + "=" * (-len(public_key) % 4))
    except Exception as exc:
        raise ValueError("Некорректный Reality public key") from exc
    if len(key_bytes) != 32:
        raise ValueError("Некорректная длина Reality public key")
    if not re.fullmatch(r"(?:[a-fA-F0-9]{2}){1,8}", short_id):
        raise ValueError("Некорректный Reality short ID")
    if not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", server_name):
        raise ValueError("Некорректный Reality SNI")

    return {
        "log": {"loglevel": "warning"},
        "inbounds": [{
            "tag": "private-line-browser",
            "listen": "127.0.0.1",
            "port": SOCKS_PORT,
            "protocol": "socks",
            "settings": {"auth": "noauth", "udp": False}
        }],
        "outbounds": [{
            "tag": "remote", "protocol": "vless",
            "settings": {"vnext": [{
                "address": address, "port": port,
                "users": [{"id": uid, "encryption": "none", "flow": "xtls-rprx-vision"}]
            }]},
            "streamSettings": {
                "network": "tcp", "security": "reality",
                "realitySettings": {
                    "serverName": server_name, "fingerprint": "chrome",
                    "password": public_key, "shortId": short_id, "spiderX": ""
                }
            }
        }],
        "routing": {"domainStrategy": "AsIs", "rules": []}
    }


def get_binary() -> pathlib.Path:
    location = home() / "xray-path.txt"
    if location.is_file():
        value = location.read_text(encoding="utf-8").strip()
        binary = pathlib.Path(value).expanduser().resolve()
        if binary.is_file():
            return binary
    raise ValueError("Xray не установлен. Запустите установщик из комплекта Chrome.")


def process_running(pid: int) -> bool:
    if not isinstance(pid, int) or pid <= 1:
        return False
    if os.name == "nt":
        import ctypes
        kernel = ctypes.windll.kernel32
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def same_xray_executable(pid: int, xray: pathlib.Path) -> bool:
    if not process_running(pid):
        return False
    if os.name == "nt":
        import ctypes
        kernel = ctypes.windll.kernel32
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            buf = ctypes.create_unicode_buffer(32768)
            length = ctypes.c_ulong(len(buf))
            if not kernel.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(length)):
                return False
            return os.path.normcase(os.path.abspath(buf.value)) == os.path.normcase(os.path.abspath(str(xray)))
        finally:
            kernel.CloseHandle(handle)
    try:
        return pathlib.Path(f"/proc/{pid}/exe").resolve() == xray.resolve()
    except OSError:
        return False


def state() -> dict:
    try:
        value = json.loads((home() / "state.json").read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def socks_ready() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", SOCKS_PORT), timeout=1.5) as sock:
            sock.sendall(b"\x05\x01\x00")
            return sock.recv(2) == b"\x05\x00"
    except OSError:
        return False


def receive_exact(sock: socket.socket, count: int) -> bytes:
    buf = b""
    while len(buf) < count:
        data = sock.recv(count - len(buf))
        if not data:
            raise OSError("SOCKS connection unexpectedly closed")
        buf += data
    return buf


def real_https_egress() -> str:
    """Test actual HTTPS via the VLESS tunnel, not merely local SOCKS readiness."""
    host = "api.ipify.org"
    with socket.create_connection(("127.0.0.1", SOCKS_PORT), timeout=8) as raw:
        raw.settimeout(12)
        raw.sendall(b"\x05\x01\x00")
        if receive_exact(raw, 2) != b"\x05\x00":
            raise ValueError("Локальный Xray не принимает SOCKS5")
        encoded = host.encode("ascii")
        raw.sendall(b"\x05\x01\x00\x03" + bytes([len(encoded)]) + encoded + struct.pack("!H", 443))
        header = receive_exact(raw, 4)
        if header[1] != 0:
            raise ValueError("VLESS Reality не установил соединение (SOCKS код " + str(header[1]) + ")")
        if header[3] == 1:
            receive_exact(raw, 4)
        elif header[3] == 4:
            receive_exact(raw, 16)
        elif header[3] == 3:
            receive_exact(raw, receive_exact(raw, 1)[0])
        else:
            raise ValueError("Некорректный ответ SOCKS5")
        receive_exact(raw, 2)
        tls = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        with tls:
            tls.sendall(b"GET / HTTP/1.1\r\nHost: api.ipify.org\r\nConnection: close\r\n\r\n")
            response = b""
            while len(response) < 8192:
                chunk = tls.recv(2048)
                if not chunk:
                    break
                response += chunk
                if b"\r\n\r\n" in response and len(response.split(b"\r\n\r\n", 1)[1]) >= 7:
                    break
    if not response.startswith(b"HTTP/1.1 200") and not response.startswith(b"HTTP/1.0 200"):
        raise ValueError("VLESS работает, но HTTPS-проверка не прошла")
    body = response.split(b"\r\n\r\n", 1)[1].strip()
    first = body.split(b"\r\n", 1)[0].decode("ascii", "ignore").strip()
    try:
        address = ipaddress.ip_address(first)
        if not address.is_global:
            raise ValueError("Получен непубличный IP")
        return str(address)
    except ValueError as exc:
        raise ValueError("Не удалось подтвердить внешний IP через VPN") from exc


def stop_existing() -> None:
    previous = state()
    pid = previous.get("pid")
    try:
        binary = get_binary()
    except ValueError:
        binary = None
    if isinstance(pid, int) and binary is not None and same_xray_executable(pid, binary):
        if os.name == "nt":
            import ctypes
            kernel = ctypes.windll.kernel32
            handle = kernel.OpenProcess(0x1001, False, pid)
            if handle:
                try:
                    kernel.TerminateProcess(handle, 0)
                finally:
                    kernel.CloseHandle(handle)
        else:
            os.kill(pid, signal.SIGTERM)
    (home() / "state.json").unlink(missing_ok=True)


def start_saved() -> dict:
    conf = home() / "client.json"
    if not conf.is_file():
        return {"ok": False, "error": "Сначала вставьте VLESS-ключ"}
    previous = state()
    try:
        binary = get_binary()
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if isinstance(previous.get("pid"), int) and same_xray_executable(previous["pid"], binary) and socks_ready():
        try:
            ip = real_https_egress()
            return {"ok": True, "running": True, "ip": ip}
        except (OSError, ValueError, ssl.SSLError):
            stop_existing()
    else:
        stop_existing()

    if socks_ready():
        return {"ok": False, "error": f"Порт SOCKS5 {SOCKS_PORT} занят другой программой"}

    with open(home() / "xray.log", "ab") as log:
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(
            [str(binary), "run", "-config", str(conf)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log,
            cwd=str(home()), **kwargs
        )
    atomic_save(home() / "state.json", json.dumps({"pid": proc.pid}, separators=(",", ":")))
    for attempt in range(35):
        if not process_running(proc.pid):
            return {"ok": False, "error": "Локальный Xray завершился. Проверьте установку."}
        if socks_ready():
            break
        time.sleep(.2)
    else:
        stop_existing()
        return {"ok": False, "error": "Локальный SOCKS5 Xray не запустился"}

    try:
        ip = real_https_egress()
        return {"ok": True, "running": True, "ip": ip}
    except (OSError, ValueError, ssl.SSLError) as exc:
        stop_existing()
        return {"ok": False, "error": "Не удалось пройти реальную HTTPS-проверку через VPN: " + str(exc)[:180]}


def dispatch(message: dict) -> dict:
    action = message.get("action")
    if action == "status":
        current = state()
        try:
            binary = get_binary()
            running = isinstance(current.get("pid"), int) and same_xray_executable(current["pid"], binary) and socks_ready()
        except ValueError:
            running = False
        return {"ok": True, "running": running, "has_profile": (home() / "client.json").is_file()}
    if action == "disconnect":
        stop_existing()
        return {"ok": True}
    if action == "connect":
        config = config_from_vless(message.get("url", ""))
        stop_existing()
        atomic_save(home() / "client.json", json.dumps(config, separators=(",", ":")))
        return start_saved()
    if action == "connect_saved":
        return start_saved()
    return {"ok": False, "error": "Неизвестная команда"}


def native_main() -> None:
    if os.name == "nt":
        import msvcrt
        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
    stream = sys.stdin.buffer
    packet_size = stream.read(4)
    if len(packet_size) != 4:
        return
    length = struct.unpack("=I", packet_size)[0]
    if length <= 0 or length > MAX_MESSAGE:
        return
    data = stream.read(length)
    if len(data) != length:
        return
    try:
        request = json.loads(data)
        result = dispatch(request) if isinstance(request, dict) else {"ok": False, "error": "Неверный запрос"}
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        result = {"ok": False, "error": str(exc)[:240]}
    except Exception:
        result = {"ok": False, "error": "Внутренняя ошибка локального клиента"}
    output = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("=I", len(output)))
    sys.stdout.buffer.write(output)
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    native_main()
