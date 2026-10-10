from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from packaging.version import Version
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.enums import UpdateStatus
from app.models.system_update import SystemUpdate

_REQUIRED_ZIP_ENTRIES = {"manifest.json", "checksums.json"}
_MAX_ZIP_SIZE_MB = 500
_MAX_UNCOMPRESSED_ZIP_MB = 1024
_MIN_FREE_DISK_MB = 512



def _validate_zip_members(zf: zipfile.ZipFile) -> list[str]:
    errors: list[str] = []
    total = 0
    for info in zf.infolist():
        path = PurePosixPath(info.filename)
        if (
            path.is_absolute()
            or ".." in path.parts
            or not path.parts
            or ":" in path.parts[0]
        ):
            errors.append(f"Unsafe ZIP path: {info.filename!r}")
            continue
        mode = (info.external_attr >> 16) & 0xFFFF
        if stat.S_ISLNK(mode):
            errors.append(f"ZIP symlinks are forbidden: {info.filename!r}")
        total += max(0, info.file_size)
    if total > _MAX_UNCOMPRESSED_ZIP_MB * 1024 * 1024:
        errors.append(
            f"ZIP uncompressed size exceeds {_MAX_UNCOMPRESSED_ZIP_MB} MB"
        )
    return errors


def safe_extract_zip(zip_path: str, extract_dir: Path) -> None:
    with zipfile.ZipFile(zip_path, "r") as zf:
        errors = _validate_zip_members(zf)
        if errors:
            raise ValueError("; ".join(errors))
        zf.extractall(extract_dir)


def _verify_checksums(extract_dir: Path, checksums: dict[str, str]) -> list[str]:
    errors: list[str] = []
    for relative_path, expected_hash in checksums.items():
        rel = PurePosixPath(relative_path)
        if rel.is_absolute() or ".." in rel.parts or not rel.parts or ":" in rel.parts[0]:
            errors.append(f"Unsafe checksum path: {relative_path}")
            continue
        file_path = extract_dir / Path(*rel.parts)
        if not file_path.exists():
            errors.append(f"File missing from ZIP: {relative_path}")
            continue
        h = hashlib.sha256()
        with file_path.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        if h.hexdigest() != expected_hash:
            errors.append(f"Checksum mismatch: {relative_path}")
    return errors


async def run_preflight_checks(zip_path: str, session: AsyncSession) -> list[str]:
    """Return list of failed check descriptions. Empty list means all passed."""
    failures: list[str] = []

    # 1. No active update in progress
    active_result = await session.execute(
        select(SystemUpdate).where(
            SystemUpdate.status.in_([
                UpdateStatus.preflight_check,
                UpdateStatus.backup_created,
                UpdateStatus.installing,
                UpdateStatus.migrating,
                UpdateStatus.health_check,
            ])
        )
    )
    if active_result.scalar_one_or_none() is not None:
        failures.append("Другое обновление уже выполняется.")
        return failures

    # 2. ZIP exists and is under size limit
    zip_file = Path(zip_path)
    if not zip_file.exists():
        failures.append(f"ZIP-файл не найден: {zip_path}")
        return failures

    if not zip_path.endswith(".zip"):
        failures.append("Файл должен иметь расширение .zip")

    size_mb = zip_file.stat().st_size / (1024 * 1024)
    if size_mb > _MAX_ZIP_SIZE_MB:
        failures.append(f"ZIP-файл превышает {_MAX_ZIP_SIZE_MB} МБ (размер: {size_mb:.1f} МБ)")

    if failures:
        return failures

    # 3. Validate ZIP structure
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            zip_entries = set(zf.namelist())
            failures.extend(_validate_zip_members(zf))
    except zipfile.BadZipFile:
        failures.append("Повреждённый ZIP-архив.")
        return failures

    for required in _REQUIRED_ZIP_ENTRIES:
        if required not in zip_entries:
            failures.append(f"В ZIP отсутствует обязательный файл: {required}")

    if failures:
        return failures

    extract_dir = Path(tempfile.mkdtemp(prefix="pl_preflight_"))
    try:
        safe_extract_zip(zip_path, extract_dir)

        # 4. Validate manifest.json
        manifest_path = extract_dir / "manifest.json"
        try:
            with manifest_path.open() as f:
                manifest = json.load(f)
        except Exception as exc:
            failures.append(f"Ошибка чтения manifest.json: {exc}")
            return failures

        if manifest.get("product") != "Private Line":
            failures.append("manifest.json: поле product должно быть 'Private Line'")

        if manifest.get("requires_db_migration"):
            failures.append(
                "ZIP-updater не поддерживает production DB migrations; "
                "используйте host-level release deployment."
            )

        new_version_str = manifest.get("version")
        if not new_version_str:
            failures.append("manifest.json: отсутствует поле version")

        min_version_str = manifest.get("min_supported_version", "0.0.0")
        try:
            current_version = Version(settings.APP_VERSION)
            min_version = Version(min_version_str)
            if current_version < min_version:
                failures.append(
                    f"Текущая версия {settings.APP_VERSION} ниже минимально поддерживаемой {min_version_str}"
                )
        except Exception as exc:
            failures.append(f"Ошибка сравнения версий: {exc}")

        # 5. Validate checksums
        checksums_path = extract_dir / "checksums.json"
        try:
            with checksums_path.open() as f:
                checksums = json.load(f)
        except Exception as exc:
            failures.append(f"Ошибка чтения checksums.json: {exc}")
            return failures

        checksum_errors = _verify_checksums(extract_dir, checksums)
        failures.extend(checksum_errors)

        # 6. Free disk space
        disk = shutil.disk_usage(settings.APP_DIR)
        free_mb = disk.free / (1024 * 1024)
        needed_mb = size_mb * 2 + _MIN_FREE_DISK_MB
        if free_mb < needed_mb:
            failures.append(
                f"Недостаточно места на диске: доступно {free_mb:.0f} МБ, нужно {needed_mb:.0f} МБ"
            )

        # 7. Write permissions
        if not os.access(settings.APP_DIR, os.W_OK):
            failures.append(f"Нет прав на запись в директорию: {settings.APP_DIR}")

    finally:
        shutil.rmtree(extract_dir, ignore_errors=True)

    return failures
