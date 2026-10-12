#!/usr/bin/env bash
# Prepare one reproducible Marzban/Xray runtime for Private Line.
#
# Marzban v0.8.4 commits a user to DB and then schedules Xray mutation through
# FastAPI BackgroundTasks. Upstream also swallows xray-api ConnectionError in
# threaded helpers. That allows HTTP 200 + a valid UUID while the live Xray
# process does not know the user. Private Line makes critical POST/PUT user
# mutations synchronous and retry-bounded: HTTP success means HandlerService
# accepted the mutation; otherwise the control plane retries/fails closed.
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
USER_ROUTER_OVERRIDE="$OVERRIDE_DIR/user.py"
COMPOSE_OVERRIDE="$MARZBAN_DIR/docker-compose.override.yml"
PATCH_MARKER="PRIVATE_LINE_XRAY_MUTATION_SYNC_V2"

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

echo "[runtime 1/5] Ensure pinned Xray $XRAY_VERSION"
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
    echo "FAIL: pinned Xray installation verification failed" >&2
    exit 2
}

echo "[runtime 2/5] Patch Marzban Xray mutation helpers: synchronous + bounded retry"
OPS_SOURCE="$(mktemp)"
OPS_PATCHED="$(mktemp)"
docker run --rm --entrypoint cat "$MARZBAN_IMAGE" /code/app/xray/operations.py > "$OPS_SOURCE"
[[ -s "$OPS_SOURCE" ]] || { echo "FAIL: cannot read upstream operations.py" >&2; exit 2; }

SOURCE="$OPS_SOURCE" DEST="$OPS_PATCHED" MARKER="$PATCH_MARKER" python3 - <<'PY'
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
    nxt = text.find("\n\n@threaded_function", start + 1)
    if nxt < 0:
        nxt = text.find("\n\ndef ", start + 1)
    if nxt < 0:
        raise RuntimeError(f"cannot locate end of {name}")
    return text[:start] + replacement.rstrip() + text[nxt:]

add = f'''# {marker}
def _add_user_to_inbound(api: XRayAPI, inbound_tag: str, account: Account):
    delays = (0.10, 0.35, 0.80)
    for attempt, delay in enumerate(delays, start=1):
        try:
            api.add_inbound_user(tag=inbound_tag, user=account, timeout=3)
            return
        except xray.exc.EmailExistsError:
            return
        except xray.exc.ConnectionError:
            if attempt == len(delays):
                raise
            sleep(delay)
'''

remove = '''def _remove_user_from_inbound(api: XRayAPI, inbound_tag: str, email: str):
    delays = (0.10, 0.35, 0.80)
    for attempt, delay in enumerate(delays, start=1):
        try:
            api.remove_inbound_user(tag=inbound_tag, email=email, timeout=3)
            return
        except xray.exc.EmailNotFoundError:
            return
        except xray.exc.ConnectionError:
            if attempt == len(delays):
                raise
            sleep(delay)
'''

alter = '''def _alter_inbound_user(api: XRayAPI, inbound_tag: str, account: Account):
    delays = (0.10, 0.35, 0.80)
    for attempt, delay in enumerate(delays, start=1):
        try:
            api.remove_inbound_user(tag=inbound_tag, email=account.email, timeout=3)
            break
        except xray.exc.EmailNotFoundError:
            break
        except xray.exc.ConnectionError:
            if attempt == len(delays):
                raise
            sleep(delay)

    for attempt, delay in enumerate(delays, start=1):
        try:
            api.add_inbound_user(tag=inbound_tag, user=account, timeout=3)
            return
        except xray.exc.EmailExistsError:
            return
        except xray.exc.ConnectionError:
            if attempt == len(delays):
                raise
            sleep(delay)
'''

src = replace_function(src, "_add_user_to_inbound", add)
src = replace_function(src, "_remove_user_from_inbound", remove)
src = replace_function(src, "_alter_inbound_user", alter)

# Upstream PR #1642 added RAW to the Vision allow-list. Keep the pinned 0.8.4
# source compatible even though Private Line deliberately uses TCP.
src = src.replace("not in ('tcp', 'kcp')", "not in ('tcp', 'raw', 'kcp')")
src = src.replace("in ('tcp', 'kcp')", "in ('tcp', 'raw', 'kcp')")

ast.parse(src, filename="operations.py")
Path(os.environ["DEST"]).write_text(src)
PY

python3 -m py_compile "$OPS_PATCHED"
install -m 600 "$OPS_PATCHED" "$OPERATIONS_OVERRIDE"

echo "[runtime 3/5] Patch critical Marzban user POST/PUT to wait for live Xray"
USER_SOURCE="$(mktemp)"
USER_PATCHED="$(mktemp)"
docker run --rm --entrypoint cat "$MARZBAN_IMAGE" /code/app/routers/user.py > "$USER_SOURCE"
[[ -s "$USER_SOURCE" ]] || { echo "FAIL: cannot read upstream user.py" >&2; exit 2; }

SOURCE="$USER_SOURCE" DEST="$USER_PATCHED" MARKER="$PATCH_MARKER" python3 - <<'PY'
import ast
import os
from pathlib import Path

src = Path(os.environ["SOURCE"]).read_text()
marker = os.environ["MARKER"]

post_old = "    bg.add_task(xray.operations.add_user, dbuser=dbuser)\n"
post_new = (
    f"    # {marker}: do not return HTTP success before live Xray accepts user\n"
    "    xray.operations.add_user(dbuser=dbuser)\n"
)
if post_old not in src:
    raise RuntimeError("upstream POST /user mutation hook changed")
src = src.replace(post_old, post_new, 1)

modify_old = '''    if user.status in [UserStatus.active, UserStatus.on_hold]:
        bg.add_task(xray.operations.update_user, dbuser=dbuser)
    else:
        bg.add_task(xray.operations.remove_user, dbuser=dbuser)
'''
modify_new = '''    # PRIVATE_LINE_XRAY_MUTATION_SYNC_V2:
    # status/proxy/inbound mutation is successful only after live Xray accepts it.
    if user.status in [UserStatus.active, UserStatus.on_hold]:
        xray.operations.update_user(dbuser=dbuser)
    else:
        xray.operations.remove_user(dbuser=dbuser)
'''
if modify_old not in src:
    raise RuntimeError("upstream PUT /user mutation hook changed")
src = src.replace(modify_old, modify_new, 1)

ast.parse(src, filename="user.py")
Path(os.environ["DEST"]).write_text(src)
PY

python3 -m py_compile "$USER_PATCHED"
install -m 600 "$USER_PATCHED" "$USER_ROUTER_OVERRIDE"

rm -f "$OPS_SOURCE" "$OPS_PATCHED" "$USER_SOURCE" "$USER_PATCHED"

echo "[runtime 4/5] Install read-only source overrides"
cat > "$COMPOSE_OVERRIDE.tmp" <<EOF
services:
  marzban:
    volumes:
      - $OPERATIONS_OVERRIDE:/code/app/xray/operations.py:ro
      - $USER_ROUTER_OVERRIDE:/code/app/routers/user.py:ro
EOF
chmod 600 "$COMPOSE_OVERRIDE.tmp"
if [[ ! -f "$COMPOSE_OVERRIDE" ]] || ! cmp -s "$COMPOSE_OVERRIDE.tmp" "$COMPOSE_OVERRIDE"; then
    mv "$COMPOSE_OVERRIDE.tmp" "$COMPOSE_OVERRIDE"
else
    rm -f "$COMPOSE_OVERRIDE.tmp"
fi

echo "[runtime 5/5] Verify exact runtime artifacts"
docker compose -f "$MARZBAN_DIR/docker-compose.yml" -f "$COMPOSE_OVERRIDE" config -q
grep -q "$PATCH_MARKER" "$OPERATIONS_OVERRIDE"
grep -q "$PATCH_MARKER" "$USER_ROUTER_OVERRIDE"
"$CORE" version 2>/dev/null | head -n1 | grep -q "^Xray $XRAY_VERSION"

echo "PRIVATE LINE RUNTIME PASS: Marzban v0.8.4 + Xray $XRAY_VERSION + synchronous verified user mutations"
