"""Bounded, credential-free results from a temporary VLESS client in admin.

The pasted link is never logged, saved, placed on a command line or returned
to the browser. This measures the main server's path, not a user's ISP path.
"""
from __future__ import annotations

import ipaddress
import json
import os
import socket
import ssl
import struct
import subprocess
import tempfile
import time
from urllib.parse import quote, urlsplit

from app.vless_e2e_probe_config import build_config_from_url


def _read_exact(stream: socket.socket, count: int) -> bytes:
    data = b""
    while len(data) < count:
        chunk = stream.recv(count - len(data))
        if not chunk:
            raise OSError("connection closed")
        data += chunk
    return data


def _socks(port: int, address: str, target_port: int) -> socket.socket:
    sock = socket.create_connection(("127.0.0.1", port), timeout=7)
    try:
        sock.settimeout(8)
        sock.sendall(b"\x05\x01\x00")
        if _read_exact(sock, 2) != b"\x05\x00":
            raise OSError("SOCKS handshake")
        ip = ipaddress.ip_address(address)
        atyp = 1 if ip.version == 4 else 4
        sock.sendall(b"\x05\x01\x00" + bytes([atyp]) + ip.packed + struct.pack("!H", target_port))
        reply = _read_exact(sock, 4)
        if reply[1] != 0:
            raise OSError("outbound refused")
        length = {1: 4, 4: 16}.get(reply[3])
        if reply[3] == 3:
            length = _read_exact(sock, 1)[0]
        if length is None:
            raise OSError("SOCKS response")
        _read_exact(sock, length + 2)
        return sock
    except Exception:
        sock.close()
        raise


def _site(url: str) -> tuple[str, str, str]:
    if not 1 <= len(url) <= 1200 or any(ord(c) < 33 for c in url):
        raise ValueError("Укажите один полный HTTPS-адрес без пробелов")
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.port not in (None, 443)
                or parsed.username or parsed.password or parsed.fragment):
            raise ValueError("Разрешены только HTTPS-сайты на порту 443 без учётных данных")
        host = parsed.hostname.encode("idna").decode("ascii").lower()
        if not host or len(host) > 253 or not all(c.isascii() and (c.isalnum() or c in '.-') for c in host):
            raise ValueError("Некорректное имя сайта")
        path = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
        if parsed.query:
            path += "?" + quote(parsed.query, safe="%=&?/:@!$'()*+,;+-._~")
        if len(path) > 1024:
            raise ValueError("Слишком длинный адрес сайта")
    except (UnicodeError, ValueError) as exc:
        raise ValueError("Некорректный адрес HTTPS-сайта") from exc
    return host, path, parsed.geturl()


def _public_site_ips(host: str) -> list[str]:
    answers = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    ips = list(dict.fromkeys(item[4][0] for item in answers))
    if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
        raise ValueError("Адрес сайта не является публичным")
    # Pin one resolved address for the actual request; no DNS rebind/redirect.
    return ips


def _dot(port: int) -> None:
    with _socks(port, "8.8.8.8", 853) as sock:
        with ssl.create_default_context().wrap_socket(sock, server_hostname="dns.google") as tls:
            query = b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x07example\x03com\x00\x00\x01\x00\x01"
            tls.sendall(struct.pack("!H", len(query)) + query)
            length = struct.unpack("!H", _read_exact(tls, 2))[0]
            answer = _read_exact(tls, min(length, 2048))
            if len(answer) < 12 or answer[:2] != b"\x12\x34" or answer[3] & 15:
                raise OSError("DNS response invalid")


def _https(port: int, ip: str, host: str, path: str) -> int:
    with _socks(port, ip, 443) as sock:
        with ssl.create_default_context().wrap_socket(sock, server_hostname=host) as tls:
            tls.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n"
                         "Accept: application/dns-json, */*\r\n"
                         "User-Agent: PrivateLine-Diagnostic/1\r\n\r\n").encode("ascii"))
            first = b""
            while b"\r\n" not in first and len(first) < 256:
                first += _read_exact(tls, 1)
            head = first.split(b"\r\n", 1)[0]
            if not head.startswith(b"HTTP/") or len(head.split(b" ")) < 2:
                raise OSError("HTTP response invalid")
            return int(head.split(b" ")[1])


def run_diagnostic(vless_url: str, site_url: str) -> list[dict[str, object]]:
    """Always bounded, never returning input/URLs/raw Xray logs or exceptions."""
    result: list[dict[str, object]] = []
    def stage(name: str, ok: bool, detail: str, elapsed: float = 0) -> None:
        result.append({"stage": name, "ok": ok, "detail": detail, "seconds": round(elapsed, 1)})

    try:
        host, path, _ = _site(site_url)
        # The caller already matched this exact URI to an active DB key.
        # A free port is reserved only briefly, then bound by Xray.
        with socket.socket() as temp:
            temp.bind(("127.0.0.1", 0))
            port = temp.getsockname()[1]
        config = build_config_from_url(vless_url, port)
        config["log"] = {"loglevel": "warning"}
        node = config["outbounds"][0]["settings"]["vnext"][0]
        stage("Профиль", True, "Формат Reality/Vision корректен")
    except Exception:
        stage("Профиль", False, "Некорректный профиль или адрес сайта")
        return result

    try:
        started = time.monotonic()
        with socket.create_connection((node["address"], node["port"]), timeout=6):
            pass
        stage("Нода TCP/443", True, "Доступна с основного сервера", time.monotonic() - started)
    except OSError:
        stage("Нода TCP/443", False, "Нет TCP-соединения с основного сервера")
        return result

    try:
        started = time.monotonic()
        ips = _public_site_ips(host)
        stage("DNS сайта", True, "Получен публичный IP; адрес закреплён для проверки", time.monotonic() - started)
    except (OSError, ValueError):
        ips = []
        stage("DNS сайта", False, "Не удалось получить безопасный публичный адрес")

    process = None
    try:
        with tempfile.TemporaryDirectory(prefix="private-line-diag-") as folder:
            os.chmod(folder, 0o700)
            path_config = os.path.join(folder, "xray.json")
            descriptor = os.open(path_config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                json.dump(config, stream)
            process = subprocess.Popen(["/usr/local/bin/xray", "run", "-c", path_config],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                       cwd=folder, close_fds=True)
            started = time.monotonic()
            while time.monotonic() - started < 7:
                if process.poll() is not None:
                    break
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                        break
                except OSError:
                    time.sleep(0.15)
            else:
                stage("Временный клиент", False, "Локальный Xray не запустился")
                return result
            if process.poll() is not None:
                stage("Временный клиент", False, "Xray завершился до начала теста")
                return result
            stage("Временный клиент", True, "Запущен на время проверки")
            started = time.monotonic()
            try:
                _dot(port)
                stage("DNS 8.8.8.8:853", True, "Ответ DNS через оплаченный туннель", time.monotonic() - started)
            except (OSError, ssl.SSLError, ValueError):
                stage("DNS 8.8.8.8:853", False, "Тайм-аут или отказ DNS через туннель", time.monotonic() - started)
            started = time.monotonic()
            try:
                status = _https(port, "1.1.1.1", "1.1.1.1", "/dns-query?name=example.com&type=A")
                stage("DNS 1.1.1.1:443", status == 200, f"DNS через HTTPS: HTTP {status}", time.monotonic() - started)
            except (OSError, ssl.SSLError, ValueError):
                stage("DNS 1.1.1.1:443", False, "DNS через HTTPS недоступен", time.monotonic() - started)
            if ips:
                started = time.monotonic()
                try:
                    status = _https(port, ips[0], host, path)
                    stage("Указанный сайт", True, f"Сайт ответил: HTTP {status}", time.monotonic() - started)
                except (OSError, ssl.SSLError, ValueError, UnicodeError):
                    stage("Указанный сайт", False, "Нет HTTPS-ответа через туннель", time.monotonic() - started)
    except (OSError, ValueError):
        stage("Временный клиент", False, "Не удалось запустить встроенный Xray")
    finally:
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate(timeout=2)
            else:
                process.communicate(timeout=2)
    return result


def explain(result: list[dict[str, object]], client_log: str = "") -> list[str]:
    states = {str(item["stage"]): bool(item["ok"]) for item in result}
    observations: list[str] = []
    lower_log = client_log.lower()
    if "8.8.8.8:853" in lower_log and "dns: exchange failed" in lower_log:
        observations.append("Журнал Windows-клиента: DNS через 8.8.8.8:853 не отвечает; тайм-ауты разрешения имён объясняют, почему сайты не открываются.")
    if "rule-set list error" in lower_log:
        observations.append("Клиент также не загружает список правил. Это может быть следствием недоступного DNS; проверяйте после его восстановления.")
    if "failed to find an available destination" in lower_log or "reality" in lower_log and "handshake" in lower_log:
        observations.append("В журнале есть ошибка подключения VLESS/Reality. Нужны клиентская сеть и версия приложения для проверки рукопожатия.")
    if "certificate" in lower_log and ("expired" in lower_log or "not yet valid" in lower_log):
        observations.append("Клиент сообщает об ошибке времени действия TLS-сертификата. Проверьте дату и время устройства.")
    if "context canceled" in lower_log and "stopping profile" in lower_log:
        observations.append("Ошибки context canceled после остановки профиля являются следствием отключения; причина отказа обычно выше в журнале.")
    if states.get("Нода TCP/443") is False:
        observations.append("Основной сервер не достигает ноды: проверяйте порт 443 и сетевой маршрут.")
    elif states.get("DNS 8.8.8.8:853") is False and states.get("DNS 1.1.1.1:443") is True:
        observations.append("Через туннель порт DNS/853 недоступен, а DNS через HTTPS/443 работает. В Windows-клиенте укажите Remote DNS https://1.1.1.1/dns-query.")
    elif states.get("DNS 8.8.8.8:853") is True and "dns: exchange failed" in lower_log:
        observations.append("DNS/853 через серверный туннель доступен. Ошибка Windows-клиента требует проверки его сетевого маршрута, настроек или блокировки на стороне сети пользователя.")
    if states.get("Указанный сайт") is True:
        observations.append("Сайт отвечает через ключ с основного сервера. Соединение из вашей сети/приложения этой проверкой не подтверждается.")
    elif states.get("Указанный сайт") is False:
        observations.append("Указанный сайт не ответил через туннель с основного сервера: проверьте DNS, TLS и доступность сайта.")
    return observations or ["Проверка не завершена; смотрите этапы выше."]
