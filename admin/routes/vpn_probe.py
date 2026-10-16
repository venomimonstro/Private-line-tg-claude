"""One-time, admin-issued Windows probe with structured client-side telemetry."""
from __future__ import annotations

import hashlib
import hmac
import json
import asyncio
import re
import secrets
import logging
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from admin.dependencies import require_super_admin
from app.config import settings
from app.database import async_session_maker, get_async_session
from app.enums import SubscriptionStatus, VpnKeyStatus
from app.models.app_setting import AppSetting
from app.models.subscription import Subscription
from app.models.user import User
from app.models.vpn_key import VpnKey
from app.models.vpn_server import VpnServer
from app.services.vpn_key import _server_protocol, get_client_access_url
from app.services.vpn_link import canonical_client_link
from app.vpn.admin_diagnostic import _site
from app.vpn.direct_xray import DirectXrayClient
from app.vpn.probe_package import verified_xray_archive

router = APIRouter()
logger = logging.getLogger(__name__)
templates = Jinja2Templates(directory="admin/templates")
PREFIX = "vpnprobe_"
RESULT_PREFIX = "vpnprobe_result_"
CMD_LAUNCHER = (
    '@echo off\r\n'
    'setlocal\r\n'
    'set "PL_PROBE_FILE=%~f0"\r\n'
    'powershell.exe -NoProfile -ExecutionPolicy Bypass -Command '
    '"$text=[IO.File]::ReadAllText($env:PL_PROBE_FILE,[Text.Encoding]::UTF8);'
    "$marker=':__PROBE_POWERSHELL__';"
    '$i=$text.LastIndexOf($marker);'
    'if($i -lt 0){throw \'Probe payload missing\'};'
    '$code=$text.Substring($i+$marker.Length);Invoke-Expression $code"\r\n'
    'if errorlevel 1 pause\r\n'
    'exit /b %errorlevel%\r\n'
    ':__PROBE_POWERSHELL__\r\n'
)
STAGES = frozenset(("profile", "control_tls", "direct_site", "node_tcp", "binary",
                    "xray_start", "socks", "vpn_site", "vpn_dns_853", "xray_log", "client_runtime"))
CODES = frozenset(("OK", "INVALID_KEY", "INVALID_SITE", "BAD_REALITY_PARAMS", "TLS_FAIL", "HTTP_FAIL",
                   "DNS_FAIL", "TCP_TIMEOUT", "TCP_REFUSED", "TCP_FAIL", "DOWNLOAD_FAIL",
                   "DIGEST_FAIL", "BINARY_FAIL", "XRAY_EXIT", "SOCKS_TIMEOUT", "SOCKS_FAIL",
                   "HTTPS_TIMEOUT", "HTTPS_TLS_FAIL", "HTTPS_FAIL", "DNS853_TIMEOUT",
                   "DNS853_FAIL", "REALITY_FAIL", "AUTH_FAIL", "DIAL_FAIL", "NO_ERROR_LOG", "SKIPPED",
                   "CLIENT_ERROR"))


class TraceStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stage: str = Field(max_length=24)
    code: str = Field(max_length=24)
    elapsed_ms: int = Field(ge=0, le=120000)
    http_status: int | None = Field(default=None, ge=100, le=599)


class ProbeReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    test_id: str = Field(pattern=r"^[a-f0-9]{24}$")
    profile_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    windows: str = Field(pattern=r"^[0-9.]{1,24}$")
    steps: list[TraceStep] = Field(min_length=1, max_length=24)


def safe_steps(report: ProbeReport) -> list[dict]:
    if any(s.stage not in STAGES or s.code not in CODES for s in report.steps):
        raise ValueError("Unsupported stage or code")
    return [s.model_dump() for s in report.steps]


def explain(steps: list[dict], node_trace: dict, key_match: bool) -> str:
    failures = {row["stage"]: row["code"] for row in steps if row["code"] not in ("OK", "SKIPPED")}
    if not key_match:
        return "Ссылка не совпадает с активным ключом в базе. Сравните ключ Telegram с текущей подпиской."
    if node_trace.get("status") == "observed" and not node_trace.get("key_on_node", False):
        return "Ключ есть в базе, но отсутствует или отличается на VPN-ноде. Требуется синхронизация ноды."
    if "control_tls" in failures:
        return "Компьютер не может проверить HTTPS основного сервера. Проверьте сертификат и доступность админки из этой сети."
    if "node_tcp" in failures and node_trace.get("listener"):
        return "Нода слушает порт, но соединение с компьютера не прошло: исследуйте маршрут или блокировку провайдера."
    if "node_tcp" in failures:
        return "Соединение с нодой по TCP не прошло. Проверьте службу, порт и сетевой путь."
    if "binary" in failures:
        return "Не удалось получить или проверить временный Xray. VPN-тест ещё не выполнялся."
    if "client_runtime" in failures:
        return "Проверка прервалась на компьютере. Посмотрите последний успешный этап в отчёте и локальную копию результата."
    if "xray_start" in failures or "socks" in failures:
        return "Временный Xray не запустился или не открыл локальный SOCKS. Проверьте журнал защиты Windows."
    if "vpn_site" not in failures and "vpn_dns_853" in failures:
        return "VPN-туннель передаёт HTTPS, но DNS-over-TLS на 8.8.8.8:853 не отвечает. Это объясняет отсутствие интернета у клиента с таким DNS."
    if "vpn_site" in failures:
        if failures.get("xray_log") == "REALITY_FAIL":
            return "Xray на компьютере сообщает об ошибке Reality handshake. Сравните SNI, public key, short ID и фильтрацию трафика по пути."
        if failures.get("xray_log") == "AUTH_FAIL":
            return "Xray сообщает об ошибке авторизации VLESS. Сверьте UUID с ключом на ноде."
        return "TCP до ноды есть, но HTTPS через VLESS не проходит: проверьте Reality handshake, авторизацию UUID и выход ноды."
    if "direct_site" in failures:
        return "Прямой HTTPS с компьютера не прошёл, хотя VPN-этапы доступны. Проверьте локальный DNS и сеть."
    return "Проверка VLESS с этого компьютера прошла. Если приложение по-прежнему не работает, исследуйте его DNS, системный прокси и правила маршрутизации."


def _csrf(request: Request) -> str:
    session = request.cookies.get("admin_session", "")
    return hmac.new(settings.SECRET_KEY.encode(), (session + ":vpn-probe").encode(), hashlib.sha256).hexdigest()


@router.get("/vpn-probe", response_class=HTMLResponse)
async def probe_page(request: Request, db: AsyncSession = Depends(get_async_session),
                     admin: User = Depends(require_super_admin)):
    rows = (await db.execute(select(AppSetting).where(AppSetting.key.like(RESULT_PREFIX + "%"))
                             .order_by(AppSetting.updated_at.desc()).limit(30))).scalars().all()
    results = []
    for row in rows:
        try:
            results.append({"id": row.key.removeprefix(RESULT_PREFIX),
                            "at": row.updated_at, "data": json.loads(row.value or "{}")})
        except (ValueError, TypeError):
            continue
    issued_rows = (await db.execute(select(AppSetting).where(
        AppSetting.key.like(PREFIX + "%"), ~AppSetting.key.like(RESULT_PREFIX + "%"))
        .order_by(AppSetting.updated_at.desc()).limit(30))).scalars().all()
    pending = []
    for row in issued_rows:
        test_id = row.key.removeprefix(PREFIX)
        if not re.fullmatch(r"[a-f0-9]{24}", test_id):
            continue
        try:
            metadata = json.loads(row.value or "{}")
            pending.append({"id": test_id, "at": row.updated_at, "site": metadata.get("site", ""),
                            "expired": datetime.utcnow() >= datetime.fromisoformat(metadata["expires"])})
        except (ValueError, TypeError, KeyError):
            continue
    return templates.TemplateResponse("vpn_probe.html", {
        "request": request, "page_title": "Проверка VPN с компьютера", "csrf": _csrf(request),
        "results": results, "pending": pending,
    }, headers={"Cache-Control": "no-store, private", "Referrer-Policy": "no-referrer"})


@router.post("/vpn-probe/download")
async def download_probe(request: Request, site: str = Form("https://www.google.com/"),
                         csrf: str = Form(""), db: AsyncSession = Depends(get_async_session),
                         admin: User = Depends(require_super_admin)):
    if not hmac.compare_digest(csrf, _csrf(request)):
        raise HTTPException(status_code=403, detail="Invalid form token")
    try:
        _site(site)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Specify a public HTTPS website") from exc
    base = settings.CONTROL_PLANE_PUBLIC_URL.strip().rstrip("/")
    if not base.startswith("https://"):
        raise HTTPException(status_code=503, detail="Public HTTPS endpoint is not configured")
    test_id = secrets.token_hex(12)
    token = secrets.token_urlsafe(32)
    record = {"token_sha256": hashlib.sha256(token.encode()).hexdigest(),
              "expires": (datetime.utcnow() + timedelta(hours=1)).isoformat(),
              "site": site, "admin_id": admin.id}
    await db.execute(delete(AppSetting).where(AppSetting.key.like(PREFIX + "%"),
                      AppSetting.updated_at < datetime.utcnow() - timedelta(days=30)))
    db.add(AppSetting(key=PREFIX + test_id, value=json.dumps(record, separators=(",", ":")),
                      description="One-time Windows VPN probe upload"))
    await db.commit()
    source = Path("windows_probe/Probe-PrivateLine.ps1").read_text(encoding="utf-8-sig")
    for name, value in (("BASE", base), ("TOKEN", token), ("TEST_ID", test_id), ("SITE", site)):
        source = source.replace("__" + name + "__", value.replace("'", "''"))
    # A single .cmd launches on double click, regardless of the Windows .ps1
    # file association. The PowerShell payload stays in the same downloaded file.
    return Response((CMD_LAUNCHER + source).encode("utf-8"), media_type="application/octet-stream",
                    headers={"Content-Disposition": 'attachment; filename="Probe-PrivateLine.cmd"',
                             "Cache-Control": "no-store, private", "Referrer-Policy": "no-referrer"})


async def _enrich_node_trace(test_id: str, key_id: int, server_id: int, profile_sha256: str) -> None:
    """Best-effort SSH details; the client report has already been committed."""
    trace = {"status": "ssh_unavailable"}
    client = None
    try:
        async with async_session_maker() as db:
            server = await db.get(VpnServer, server_id)
            key = await db.get(VpnKey, key_id)
            if not server or not key:
                return
            client = DirectXrayClient(server)
            live = await asyncio.wait_for(client.status(), timeout=12)
            trace = {"status": "observed", "service_active": bool(live.get("service_active")),
                     "listener": bool(live.get("listener"))}
            remote = await asyncio.wait_for(client.get_key(key.outline_key_id), timeout=12)
            remote_uri = canonical_client_link(remote.access_url).split("#", 1)[0]
            remote_hash = hashlib.sha256(remote_uri.encode()).hexdigest()
            trace["key_on_node"] = hmac.compare_digest(remote_hash, profile_sha256)
    except Exception as exc:
        logger.warning("VPN probe node inspection unavailable test_id=%s error_type=%s",
                       test_id, type(exc).__name__)
    finally:
        if client:
            await client.aclose()
    async with async_session_maker() as db:
        row = await db.get(AppSetting, RESULT_PREFIX + test_id)
        if row:
            result = json.loads(row.value or "{}")
            result["node_trace"] = trace
            result["analysis"] = explain(result["steps"], trace, result["key_match"])
            row.value = json.dumps(result, separators=(",", ":"))
            row.updated_at = datetime.utcnow()
            await db.commit()


@router.post("/api/vpn-probe/report")
async def receive_probe(request: Request, report: ProbeReport, background_tasks: BackgroundTasks,
                        db: AsyncSession = Depends(get_async_session)):
    supplied = request.headers.get("authorization", "")
    if not supplied.startswith("Bearer ") or len(supplied) > 100:
        raise HTTPException(status_code=403, detail="Invalid probe token")
    token_hash = hashlib.sha256(supplied[7:].encode()).hexdigest()
    row = (await db.execute(select(AppSetting).where(AppSetting.key == PREFIX + report.test_id)
                            .with_for_update())).scalar_one_or_none()
    try:
        record = json.loads(row.value or "{}") if row else {}
        valid = (hmac.compare_digest(token_hash, record.get("token_sha256", ""))
                 and datetime.utcnow() < datetime.fromisoformat(record["expires"]))
    except (ValueError, TypeError, KeyError):
        valid = False
    if not valid:
        raise HTTPException(status_code=403, detail="Unknown, expired or used probe token")
    try:
        steps = safe_steps(report)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Match the exact Telegram URI without storing it. Do not accept a client
    # claimed key/server ID as proof of ownership.
    matches = []
    keys = (await db.execute(select(VpnKey, VpnServer)
        .join(Subscription, Subscription.id == VpnKey.subscription_id)
        .join(VpnServer, VpnServer.id == VpnKey.server_id)
        .where(VpnKey.status == VpnKeyStatus.active,
               Subscription.status.in_([SubscriptionStatus.active, SubscriptionStatus.expires_soon]),
               Subscription.expires_at > datetime.utcnow()))).all()
    for key, server in keys:
        try:
            raw = get_client_access_url(key).strip().split("#", 1)[0]
            if hmac.compare_digest(hashlib.sha256(raw.encode()).hexdigest(), report.profile_sha256):
                matches.append((key, server))
        except (ValueError, TypeError):
            continue
    node_trace = {"status": "pending" if len(matches) == 1 else "no_matching_key"}
    result = {"site": record["site"], "windows": report.windows, "steps": steps,
              "key_id": matches[0][0].id if len(matches) == 1 else None,
              "server_id": matches[0][1].id if len(matches) == 1 else None,
              "node_trace": node_trace,
              "key_match": len(matches) == 1}
    result["analysis"] = explain(steps, node_trace, len(matches) == 1)
    db.add(AppSetting(key=RESULT_PREFIX + report.test_id,
                      value=json.dumps(result, separators=(",", ":")),
                      description="Structured Windows to VPN node probe"))
    await db.delete(row)
    await db.commit()
    if len(matches) == 1 and _server_protocol(matches[0][1]) == "direct_xray":
        background_tasks.add_task(_enrich_node_trace, report.test_id, matches[0][0].id,
                                  matches[0][1].id, report.profile_sha256)
    logger.info("VPN client report stored test_id=%s steps=%d matched=%s", report.test_id,
                len(steps), len(matches) == 1)
    return {"ok": True, "test_id": report.test_id}


@router.get("/api/vpn-probe/xray")
async def get_probe_xray(request: Request, test_id: str, digest_only: bool = False,
                         db: AsyncSession = Depends(get_async_session)):
    if not re.fullmatch(r"[a-f0-9]{24}", test_id):
        raise HTTPException(status_code=404)
    row = await db.get(AppSetting, PREFIX + test_id)
    supplied = request.headers.get("authorization", "")
    try:
        record = json.loads(row.value or "{}") if row else {}
        valid = (supplied.startswith("Bearer ") and len(supplied) <= 100
                 and hmac.compare_digest(hashlib.sha256(supplied[7:].encode()).hexdigest(),
                                         record.get("token_sha256", ""))
                 and datetime.utcnow() < datetime.fromisoformat(record["expires"]))
    except (ValueError, TypeError, KeyError):
        valid = False
    if not valid:
        raise HTTPException(status_code=403, detail="Invalid probe token")
    await db.rollback()
    try:
        archive, digest = await asyncio.to_thread(verified_xray_archive)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Verified diagnostic Xray unavailable") from exc
    if digest_only:
        return PlainTextResponse(digest, headers={"Cache-Control": "no-store, private"})
    return FileResponse(archive, media_type="application/zip", filename="Xray-windows-64.zip",
                        headers={"X-Xray-SHA256": digest, "Cache-Control": "no-store, private"})
