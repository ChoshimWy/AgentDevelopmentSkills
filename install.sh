#!/usr/bin/env bash
set -euo pipefail

SCRIPT_SOURCE="${BASH_SOURCE[0]:-}"

python_is_compatible() {
    "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1
}

resolve_python() {
    local candidate directory resolved
    for candidate in python3; do
        resolved="$(command -v "$candidate" 2>/dev/null || true)"
        if [[ -n "$resolved" ]] && python_is_compatible "$resolved"; then
            printf '%s\n' "$resolved"
            return 0
        fi
    done

    local path_directories=()
    IFS=':' read -r -a path_directories <<< "${PATH:-}"
    for directory in "${path_directories[@]}"; do
        [[ -n "$directory" ]] || continue
        for candidate in "$directory"/python3.*; do
            [[ -e "$candidate" ]] || continue
            [[ "${candidate##*/}" =~ ^python3\.[0-9]+$ ]] || continue
            if [[ -x "$candidate" ]] && python_is_compatible "$candidate"; then
                printf '%s\n' "$candidate"
                return 0
            fi
        done
    done

    local common_candidates="${AGENT_SKILLS_COMMON_PYTHON_CANDIDATES:-/opt/homebrew/bin/python3:/usr/local/bin/python3:$HOME/.local/bin/python3:$HOME/.pyenv/shims/python3}"
    local common_paths=()
    IFS=':' read -r -a common_paths <<< "$common_candidates"
    for candidate in "${common_paths[@]}"; do
        if [[ -x "$candidate" ]] && python_is_compatible "$candidate"; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

if [[ -n "${AGENT_SKILLS_PYTHON:-}" ]]; then
    if [[ "$AGENT_SKILLS_PYTHON" == */* ]]; then
        PYTHON_BIN="$AGENT_SKILLS_PYTHON"
    else
        PYTHON_BIN="$(command -v "$AGENT_SKILLS_PYTHON" 2>/dev/null || true)"
    fi
    if [[ -z "$PYTHON_BIN" || ! -x "$PYTHON_BIN" ]] || ! python_is_compatible "$PYTHON_BIN"; then
        printf '%s\n' \
            "AGENT_SKILLS_PYTHON must point to an executable Python 3.11 or newer: $AGENT_SKILLS_PYTHON" \
            >&2
        exit 1
    fi
elif ! PYTHON_BIN="$(resolve_python)"; then
    printf '%s\n' \
        "AgentDevelopmentSkills could not find Python 3.11 or newer." \
        "Checked python3, versioned python3.x commands on PATH, Homebrew, ~/.local/bin, and pyenv shims." \
        "Install a compatible interpreter or set AGENT_SKILLS_PYTHON to its absolute path." \
        "The installer will not silently modify the system Python or run an unverified runtime installer." \
        >&2
    exit 1
fi

# Source-checkout fast path. The hosted/piped script has no repository sibling.
if [[ -n "$SCRIPT_SOURCE" ]]; then
    REPO_ROOT="$(cd -P "$(dirname "$SCRIPT_SOURCE")" && pwd -P)"
    if [[ -f "$REPO_ROOT/scripts/install_local.py" ]]; then
        exec "$PYTHON_BIN" "$REPO_ROOT/scripts/install_local.py" "$@"
    fi
fi

RELEASE_BASE_URL="${AGENT_SKILLS_RELEASE_BASE_URL:-https://choshimwy.github.io/AgentDevelopmentSkills}"
RELEASE_BASE_URL="${RELEASE_BASE_URL%/}"
MANIFEST_URL="${AGENT_SKILLS_RELEASE_MANIFEST_URL:-$RELEASE_BASE_URL/release-manifest.json}"

BOOTSTRAP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/agent-skills-bootstrap.XXXXXX")"
cleanup() {
    rm -rf "$BOOTSTRAP_DIR"
}
trap cleanup EXIT HUP INT TERM

download_limited() {
    "$PYTHON_BIN" - "$1" "$2" "$3" <<'PY'
import os
from pathlib import Path
import sys
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

source, destination, maximum_text = sys.argv[1:]
maximum = int(maximum_text)
allow_file = os.environ.get("AGENT_SKILLS_ALLOW_FILE_URL") == "1"
allowed = {"https"} | ({"file"} if allow_file else set())

class HttpsOnlyRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        if urlparse(new_url).scheme not in allowed:
            raise RuntimeError("download redirect must preserve HTTPS")
        return super().redirect_request(request, file_pointer, code, message, headers, new_url)

if urlparse(source).scheme not in allowed:
    raise SystemExit("download URL must use HTTPS")
target = Path(destination)
try:
    request = Request(source, headers={"User-Agent": "agent-development-skills-bootstrap/1.0"})
    with build_opener(HttpsOnlyRedirect()).open(request, timeout=30) as response, target.open("xb") as output:
        if urlparse(response.geturl()).scheme not in allowed:
            raise RuntimeError("download redirect must preserve HTTPS")
        declared = response.headers.get("Content-Length")
        if declared is not None and int(declared) > maximum:
            raise RuntimeError("download exceeds the configured size limit")
        total = 0
        while True:
            chunk = response.read(min(65536, maximum - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > maximum:
                raise RuntimeError("download exceeds the configured size limit")
            output.write(chunk)
except Exception as error:
    target.unlink(missing_ok=True)
    raise SystemExit(str(error))
PY
}

MANIFEST_PATH="$BOOTSTRAP_DIR/release-manifest.json"
BOOTSTRAP_PATH="$BOOTSTRAP_DIR/bootstrap_install.py"
download_limited "$MANIFEST_URL" "$MANIFEST_PATH" 1048576

ASSET_BASE_URL="$("$PYTHON_BIN" - "$MANIFEST_PATH" <<'PY'
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlparse

raw = Path(sys.argv[1]).read_bytes()
if len(raw) > 1024 * 1024:
    raise SystemExit("release manifest exceeds the 1 MiB size limit")
try:
    value = json.loads(raw.decode("utf-8"))
except (UnicodeDecodeError, json.JSONDecodeError) as error:
    raise SystemExit(f"release manifest is invalid: {error}")
canonical = (json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
if canonical != raw:
    raise SystemExit("release manifest must use canonical JSON encoding")
base = value.get("asset_base_url")
if not isinstance(base, str) or not base.endswith("/"):
    raise SystemExit("release manifest asset_base_url is invalid")
scheme = urlparse(base).scheme
if scheme != "https" and not (scheme == "file" and os.environ.get("AGENT_SKILLS_ALLOW_FILE_URL") == "1"):
    raise SystemExit("release manifest asset_base_url must use HTTPS")
print(base)
PY
)"

download_limited "${ASSET_BASE_URL}bootstrap_install.py" "$BOOTSTRAP_PATH" 1048576

"$PYTHON_BIN" - "$MANIFEST_PATH" "$BOOTSTRAP_PATH" <<'PY'
import hashlib
import json
from pathlib import Path
import re
import sys

manifest = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
matches = [item for item in manifest.get("bootstrap_assets", []) if isinstance(item, dict) and item.get("filename") == "bootstrap_install.py"]
if len(matches) != 1 or set(matches[0]) != {"filename", "sha256", "size"}:
    raise SystemExit("release manifest must declare exactly one bootstrap_install.py asset")
expected = matches[0]
if type(expected["size"]) is not int or not 0 < expected["size"] <= 1024 * 1024:
    raise SystemExit("release manifest bootstrap size is invalid")
if not isinstance(expected["sha256"], str) or re.fullmatch(r"[0-9a-f]{64}", expected["sha256"]) is None:
    raise SystemExit("release manifest bootstrap sha256 is invalid")
data = Path(sys.argv[2]).read_bytes()
if len(data) != expected["size"] or hashlib.sha256(data).hexdigest() != expected["sha256"]:
    raise SystemExit("downloaded bootstrap does not match release manifest")
PY

"$PYTHON_BIN" "$BOOTSTRAP_PATH" \
    --manifest-file "$MANIFEST_PATH" \
    --artifact-base-url "$ASSET_BASE_URL" \
    "$@"
