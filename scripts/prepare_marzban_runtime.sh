#!/usr/bin/env bash
# Prepare the exact Marzban/Xray runtime used by Private Line.
#
# Marzban v0.8.4 remains the management/API layer, while Xray 26.6.27 is pinned
# explicitly for current REALITY client compatibility. The only Marzban source
# override is operations.py: upstream silently swallows Xray gRPC
# ConnectionError during user hot-add/hot-remove, so we add bounded retries and
# keep the failure visible in logs. A real E2E canary still decides admission.
set -Eeuo pipefail
umask 077

[[ "$(id -u)" -eq 0 ]] || { echo "FAIL: run as root" >&2; exit 2; }

DATA_DIR="${MARZBAN_DATA:-/var/lib/marzban}"
MARZBAN_DIR="${MARZBAN_DIR:-/opt/marzban}"
XRAY_VERSION=26.6.27
XRAY_IMAGE="ghcr.io/xtls/xray-core:26.6.27@sha256:c98160906f3fe1d9d16e950c2a7997dc0c542c71816aa92c21e510412f90e1a2"
CORE_DIR="$DATA_DIR/xray-core/$XRAY_VERSION"
CORE="$CORE_DIR/xray"
OVERRIDE_DIR="$DATA_DIR/overrides"
OPERATIONS_OVERRIDE="$OVERRIDE_DIR/operations.py"
COMPOSE_OVERRIDE="$MARZBAN_DIR/docker-compose.override.yml"
PATCH_MARKER="PRIVATE_LINE_XRAY_HOTADD_RETRY_V1"

if [[ -z "${MARZBAN_IMAGE:-}" ]]; then
    case "$(uname -m)" in
        x86_64)
            MARZBAN_IMAGE="ghcr.io/gozargah/marzban:v0.8.4@sha256:953700b0b91b35dcd5165072fc7029fc3c5bd6ed10d644f80d7cb9a84a9a27f1"
            ;;
        aarch64|arm64)
            MARZBAN_IMAGE="ghcr.io/gozargah/marzban:v0.8.4@sha256:886f11d701005a80d381d4e4d659ae376d2fff55470578f115b518687d86453e"
            ;;
        *)
            echo "FAIL: unsupported architecture $(uname -m)" >&2
            exit 2
            ;;
    esac
fi

mkdir -p "$CORE_DIR" "$OVERRIDE_DIR" "$MARZBAN_DIR"
chmod 700 "$OVERRIDE_DIR"

docker image inspect "$MARZBAN_IMAGE" >/dev/null 2>&1 || docker pull "$MARZBAN_IMAGE" >/dev/null
docker image inspect "$XRAY_IMAGE" >/dev/null 2>&1 || docker pull "$XRAY_IMAGE" >/dev/null

echo "[runtime 1/4] Ensure Xray $XRAY_VERSION"
if [[ ! -x "$CORE" ]] || ! "$CORE" version 2>/dev/null | head -n1 | grep -q "^Xray $XRAY_VERSION"; then
    tmp_container="$(docker create "$XRAY_IMAGE")"
    tmp_bin="$(mktemp)"
    cleanup_core() {
        docker rm -f "${tmp_container:-}" >/dev/null 2>&1 || true
        rm -f "${tmp_bin:-}"
    }
    trap cleanup_core EXIT
    docker cp "$tmp_container:/usr/local/bin/xray" "$tmp_bin"
    install -m 755 "$tmp_bin" "$CORE"
    cleanup_core
    trap - EXIT
fi
"$CORE" version 2>/dev/null | head -n1 | grep -q "^Xray $XRAY_VERSION" || {
    echo "FAIL: Xray $XRAY_VERSION installation verification failed" >&2
    exit 2
}

echo "[runtime 2/4] Build minimal Marzban hot-add retry override from pinned image"
tmp_source="$(mktemp)"
tmp_patched="$(mktemp)"
cleanup_patch() {
    rm -f "${tmp_source:-}" "${tmp_patched:-}"
}
trap cleanup_patch EXIT

docker run --rm --entrypoint cat "$MARZBAN_IMAGE" /code/app/xray/operations.py > "$tmp_source"
[[ -s "$tmp_source" ]] || {
    echo "FAIL: cannot read Marzban operations.py from pinned image" >&2
    exit 2
}

SOURCE="$tmp_source" DEST="$tmp_patched" MARKER="$PATCH_MARKER" python3 - <<'PY'
import ast
import os
from pathlib import Path

src = Path(os.environ["SOURCE"]).read_text()
marker = os.environ["MARKER"]

if "from time import sleep" not in src:
    src = src.replace(
        "from functools import lru_cache\n",
        "from functools import lru_cache\nfrom time import sleep\n",
        1,
    )

def replace_function(text: str, name: str, replacement: str) -> str:
    start = text.index(f"@threaded_function\ndef {name}(")
    next_marker = text.find("\n\n@threaded_function", start + 1)
    if next_marker < 0:
        next_marker = text.find("\n\ndef ", start + 1)
    if next_marker < 0:
        raise RuntimeError(f"cannot find end of {name}")
    return text[:start] + replacement.rstrip() + text[next_marker:]

add = f'''# {marker}
@threaded_function
def _add_user_to_inbound(api: XRayAPI, inbound_tag: str, account: Account):
    delays = (0.10, 0.30, 0.80, 1.50)
    for attempt, delay in enumerate(delays, start=1):
        try:
            api.add_inbound_user(tag=inbound_tag, user=account, timeout=30)
            return
        except xray.exc.EmailExistsError:
            return
        except xray.exc.ConnectionError as exc:
            if attempt == len(delays):
                logger.error(
                    "Xray hot-add failed after %d attempts for inbound %s: %s",
                    attempt, inbound_tag, type(exc).__name__,
                )
                return
            sleep(delay)
'''

remove = '''@threaded_function
def _remove_user_from_inbound(api: XRayAPI, inbound_tag: str, email: str):
    delays = (0.10, 0.30, 0.80, 1.50)
    for attempt, delay in enumerate(delays, start=1):
        try:
            api.remove_inbound_user(tag=inbound_tag, email=email, timeout=30)
            return
        except xray.exc.EmailNotFoundError:
            return
        except xray.exc.ConnectionError as exc:
            if attempt == len(delays):
                logger.error(
                    "Xray hot-remove failed after %d attempts for inbound %s: %s",
                    attempt, inbound_tag, type(exc).__name__,
                )
                return
            sleep(delay)
'''

alter = '''@threaded_function
def _alter_inbound_user(api: XRayAPI, inbound_tag: str, account: Account):
    delays = (0.10, 0.30, 0.80, 1.50)
    removed = False
    for attempt, delay in enumerate(delays, start=1):
        try:
            api.remove_inbound_user(
                tag=inbound_tag, email=account.email, timeout=30
            )
            removed = True
            break
        except xray.exc.EmailNotFoundError:
            removed = True
            break
        except xray.exc.ConnectionError as exc:
            if attempt == len(delays):
                logger.error(
                    "Xray hot-alter remove failed after %d attempts for inbound %s: %s",
                    attempt, inbound_tag, type(exc).__name__,
                )
                return
            sleep(delay)

    if not removed:
        return

    for attempt, delay in enumerate(delays, start=1):
        try:
            api.add_inbound_user(tag=inbound_tag, user=account, timeout=30)
            return
        except xray.exc.EmailExistsError:
            return
        except xray.exc.ConnectionError as exc:
            if attempt == len(delays):
                logger.error(
                    "Xray hot-alter add failed after %d attempts for inbound %s: %s",
                    attempt, inbound_tag, type(exc).__name__,
                )
                return
            sleep(delay)
'''

src = replace_function(src, "_add_user_to_inbound", add)
src = replace_function(src, "_remove_user_from_inbound", remove)
src = replace_function(src, "_alter_inbound_user", alter)

# Marzban v0.8.4 images predating upstream PR #1642 can strip Vision from RAW.
# Supporting RAW here is harmless for our TCP inbound and makes the override
# correct if the network alias changes between Xray releases.
src = src.replace(
    "not in ('tcp', 'kcp')",
    "not in ('tcp', 'raw', 'kcp')",
)
src = src.replace(
    "in ('tcp', 'kcp')",
    "in ('tcp', 'raw', 'kcp')",
)

ast.parse(src, filename="operations.py")
Path(os.environ["DEST"]).write_text(src)
PY

python3 -m py_compile "$tmp_patched"
if [[ ! -s "$OPERATIONS_OVERRIDE" ]] || ! cmp -s "$tmp_patched" "$OPERATIONS_OVERRIDE"; then
    install -m 600 "$tmp_patched" "$OPERATIONS_OVERRIDE"
fi
grep -q "$PATCH_MARKER" "$OPERATIONS_OVERRIDE" || {
    echo "FAIL: Marzban hot-add retry patch marker is missing" >&2
    exit 2
}

echo "[runtime 3/4] Ensure persistent read-only Marzban override mount"
cat > "$COMPOSE_OVERRIDE.tmp" <<EOF
services:
  marzban:
    volumes:
      - $OPERATIONS_OVERRIDE:/code/app/xray/operations.py:ro
EOF
chmod 600 "$COMPOSE_OVERRIDE.tmp"
if [[ ! -f "$COMPOSE_OVERRIDE" ]] || ! cmp -s "$COMPOSE_OVERRIDE.tmp" "$COMPOSE_OVERRIDE"; then
    mv "$COMPOSE_OVERRIDE.tmp" "$COMPOSE_OVERRIDE"
else
    rm -f "$COMPOSE_OVERRIDE.tmp"
fi

echo "[runtime 4/4] Verify artifacts"
docker compose -f "$MARZBAN_DIR/docker-compose.yml" -f "$COMPOSE_OVERRIDE" config -q
grep -q "$PATCH_MARKER" "$OPERATIONS_OVERRIDE"
"$CORE" version 2>/dev/null | head -n1 | grep -q "^Xray $XRAY_VERSION"

cleanup_patch
trap - EXIT
echo "PRIVATE LINE RUNTIME PASS: Marzban v0.8.4 + Xray $XRAY_VERSION + bounded gRPC hot-add retries"
