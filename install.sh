#!/usr/bin/env bash
set -euo pipefail

SCRIPT_SOURCE="${BASH_SOURCE[0]:-}"

# BEGIN agent-skills embedded release metadata
AGENT_SKILLS_EMBEDDED_VERSION=''
AGENT_SKILLS_EMBEDDED_ASSET_BASE_URL=''
AGENT_SKILLS_EMBEDDED_SOURCE_FILENAME=''
AGENT_SKILLS_EMBEDDED_SOURCE_SHA256=''
AGENT_SKILLS_EMBEDDED_SOURCE_SIZE=''
AGENT_SKILLS_EMBEDDED_SOURCE_ROOT=''
agent_skills_select_native_record() {
    return 1
}
# END agent-skills embedded release metadata

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

resolve_selected_python() {
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
            return 1
        fi
    elif ! PYTHON_BIN="$(resolve_python)"; then
        printf '%s\n' \
            "AgentDevelopmentSkills could not find Python 3.11 or newer." \
            "Checked python3, versioned python3.x commands on PATH, Homebrew, ~/.local/bin, and pyenv shims." \
            "Install a compatible interpreter or set AGENT_SKILLS_PYTHON to its absolute path." \
            "The installer will not silently modify the system Python or run an unverified runtime installer." \
            >&2
        return 1
    fi
}

REQUESTED_ENGINE="${AGENT_SKILLS_INSTALL_ENGINE:-auto}"
case "$REQUESTED_ENGINE" in
    auto|python|rust) ;;
    *)
        printf '%s\n' "AGENT_SKILLS_INSTALL_ENGINE must be auto, rust, or python" >&2
        exit 2
        ;;
esac

# Source-checkout compatibility path. The hosted/piped script has no repository sibling.
if [[ -n "$SCRIPT_SOURCE" ]]; then
    REPO_ROOT="$(cd -P "$(dirname "$SCRIPT_SOURCE")" && pwd -P)"
    if [[ -f "$REPO_ROOT/scripts/install_local.py" ]]; then
        if [[ "$REQUESTED_ENGINE" == "rust" ]]; then
            printf '%s\n' \
                "forced Rust install requires a hosted v2 release and an explicit fresh platform selection" \
                >&2
            exit 2
        fi
        resolve_selected_python
        exec "$PYTHON_BIN" "$REPO_ROOT/scripts/install_local.py" "$@"
    fi
fi

if [[ -n "${CODEX_HOME:-}" ]]; then
    NATIVE_TARGET_ROOT="$CODEX_HOME"
elif [[ -n "${HOME:-}" ]]; then
    NATIVE_TARGET_ROOT="$HOME/.codex"
else
    NATIVE_TARGET_ROOT=''
fi
NATIVE_PLATFORMS=()
NATIVE_PLATFORM_KEYS='|'
NATIVE_JSON=0

parse_native_request() {
    local argument value
    while (($#)); do
        argument="$1"
        case "$argument" in
            --json)
                NATIVE_JSON=1
                shift
                ;;
            --target-root|--platform)
                (($# >= 2)) || return 1
                value="$2"
                if [[ "$argument" == "--target-root" ]]; then
                    NATIVE_TARGET_ROOT="$value"
                else
                    case "$value" in
                        apple|desktop) ;;
                        *) return 1 ;;
                    esac
                    [[ "$NATIVE_PLATFORM_KEYS" != *"|$value|"* ]] || return 1
                    NATIVE_PLATFORMS+=("$value")
                    NATIVE_PLATFORM_KEYS+="$value|"
                fi
                shift 2
                ;;
            --target-root=*)
                NATIVE_TARGET_ROOT="${argument#*=}"
                shift
                ;;
            --platform=*)
                value="${argument#*=}"
                case "$value" in
                    apple|desktop) ;;
                    *) return 1 ;;
                esac
                [[ "$NATIVE_PLATFORM_KEYS" != *"|$value|"* ]] || return 1
                NATIVE_PLATFORMS+=("$value")
                NATIVE_PLATFORM_KEYS+="$value|"
                shift
                ;;
            *)
                return 1
                ;;
        esac
    done
    [[ "$NATIVE_PLATFORM_KEYS" != "|" ]] || return 1
    [[ -n "$NATIVE_TARGET_ROOT" && "$NATIVE_TARGET_ROOT" != *'~'* ]] || return 1
    [[ ! -L "$NATIVE_TARGET_ROOT" ]] || return 1
    if [[ -e "$NATIVE_TARGET_ROOT" && ! -d "$NATIVE_TARGET_ROOT" ]]; then
        return 1
    fi
    for existing in AGENTS.md skills .agent-skills; do
        [[ ! -e "$NATIVE_TARGET_ROOT/$existing" && ! -L "$NATIVE_TARGET_ROOT/$existing" ]] || return 1
    done
}

gnu_libc_is_compatible() {
    local libc_name libc_version major minor
    command -v getconf >/dev/null 2>&1 || return 1
    read -r libc_name libc_version < <(getconf GNU_LIBC_VERSION 2>/dev/null) || return 1
    [[ "$libc_name" == "glibc" && "$libc_version" =~ ^([0-9]+)\.([0-9]+)$ ]] || return 1
    major="${BASH_REMATCH[1]}"
    minor="${BASH_REMATCH[2]}"
    ((major > 2 || (major == 2 && minor >= 39)))
}

native_host_target() {
    local host_os host_arch
    case "$(uname -s 2>/dev/null || true)" in
        Darwin) host_os="apple-darwin" ;;
        Linux)
            gnu_libc_is_compatible || return 1
            host_os="unknown-linux-gnu"
            ;;
        *) return 1 ;;
    esac
    case "$(uname -m 2>/dev/null || true)" in
        arm64|aarch64) host_arch="aarch64" ;;
        x86_64|amd64) host_arch="x86_64" ;;
        *) return 1 ;;
    esac
    printf '%s-%s\n' "$host_arch" "$host_os"
}

sha256_file() {
    if command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | awk '{print $1}'
    elif command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    elif command -v openssl >/dev/null 2>&1; then
        openssl dgst -sha256 "$1" | awk '{print $NF}'
    else
        printf '%s\n' "no SHA-256 implementation is available" >&2
        return 1
    fi
}

download_native_asset() {
    local url="$1" destination="$2" expected_size="$3" expected_sha256="$4"
    local protocols actual_size actual_sha256
    protocols="=https"
    if [[ "${AGENT_SKILLS_ALLOW_FILE_URL:-}" == "1" ]]; then
        protocols="=https,file"
    fi
    curl --fail --silent --show-error --location \
        --proto "$protocols" --proto-redir "$protocols" \
        --connect-timeout 10 --max-time 120 --max-filesize "$expected_size" \
        -o "$destination" "$url"
    actual_size="$(wc -c < "$destination" | tr -d '[:space:]')"
    [[ "$actual_size" == "$expected_size" ]] || {
        printf '%s\n' "downloaded asset size does not match the embedded release metadata" >&2
        return 1
    }
    actual_sha256="$(sha256_file "$destination")"
    [[ "$actual_sha256" == "$expected_sha256" ]] || {
        printf '%s\n' "downloaded asset SHA-256 does not match the embedded release metadata" >&2
        return 1
    }
}

run_native_install() {
    local target="$1"
    local native_filename native_sha256 native_size
    local temporary source_archive native_executable extracted_root
    command -v curl >/dev/null 2>&1 || {
        printf '%s\n' "native bootstrap requires curl" >&2
        return 2
    }
    command -v unzip >/dev/null 2>&1 || {
        printf '%s\n' "native bootstrap requires unzip" >&2
        return 2
    }
    [[ "$AGENT_SKILLS_EMBEDDED_ASSET_BASE_URL" == https://* \
        || ( "${AGENT_SKILLS_ALLOW_FILE_URL:-}" == "1" \
            && "$AGENT_SKILLS_EMBEDDED_ASSET_BASE_URL" == file://* ) ]] || {
        printf '%s\n' "embedded release asset base URL is unavailable or insecure" >&2
        return 2
    }
    agent_skills_select_native_record "$target" || {
        printf '%s\n' "the embedded release has no native artifact for $target" >&2
        return 2
    }
    temporary="$(mktemp -d "${TMPDIR:-/tmp}/agent-skills-native-bootstrap.XXXXXX")"
    NATIVE_BOOTSTRAP_DIR="$temporary"
    trap 'rm -rf "${NATIVE_BOOTSTRAP_DIR:-}"' EXIT HUP INT TERM
    source_archive="$temporary/$AGENT_SKILLS_EMBEDDED_SOURCE_FILENAME"
    native_executable="$temporary/$native_filename"
    download_native_asset \
        "$AGENT_SKILLS_EMBEDDED_ASSET_BASE_URL$AGENT_SKILLS_EMBEDDED_SOURCE_FILENAME" \
        "$source_archive" \
        "$AGENT_SKILLS_EMBEDDED_SOURCE_SIZE" \
        "$AGENT_SKILLS_EMBEDDED_SOURCE_SHA256"
    download_native_asset \
        "$AGENT_SKILLS_EMBEDDED_ASSET_BASE_URL$native_filename" \
        "$native_executable" \
        "$native_size" \
        "$native_sha256"
    chmod 700 "$native_executable"
    mkdir "$temporary/extracted"
    unzip -qq "$source_archive" -d "$temporary/extracted"
    extracted_root="$temporary/extracted/$AGENT_SKILLS_EMBEDDED_SOURCE_ROOT"
    [[ -d "$extracted_root" && ! -L "$extracted_root" ]] || {
        printf '%s\n' "verified source archive root is missing or unsafe" >&2
        return 2
    }
    local command=(
        "$native_executable"
        install
        --source-root "$extracted_root"
        --target-root "$NATIVE_TARGET_ROOT"
    )
    local platform_id
    for platform_id in "${NATIVE_PLATFORMS[@]}"; do
        command+=(--platform "$platform_id")
    done
    if [[ " ${NATIVE_PLATFORMS[*]} " == *" apple "* ]]; then
        command+=(--session-launcher "$native_executable")
    fi
    if ((NATIVE_JSON)); then
        command+=(--json)
    fi
    AGENT_SKILLS_INSTALL_ENGINE_SELECTED=rust \
    AGENT_SKILLS_RELEASE_SHA256="$AGENT_SKILLS_EMBEDDED_SOURCE_SHA256" \
    AGENT_SKILLS_RELEASE_VERSION="$AGENT_SKILLS_EMBEDDED_VERSION" \
        "${command[@]}"
}

NATIVE_REQUEST_ELIGIBLE=0
if parse_native_request "$@"; then
    NATIVE_REQUEST_ELIGIBLE=1
fi

if [[ "$REQUESTED_ENGINE" != "python" && "$NATIVE_REQUEST_ELIGIBLE" == "1" ]]; then
    HOST_TARGET="$(native_host_target || true)"
    if [[ -n "$HOST_TARGET" ]] && agent_skills_select_native_record "$HOST_TARGET"; then
        run_native_install "$HOST_TARGET"
        exit $?
    fi
fi

if [[ "$REQUESTED_ENGINE" == "rust" ]]; then
    printf '%s\n' \
        "forced Rust install requires an embedded v2 native release, an explicit fresh --platform apple/desktop selection, and no compatibility-only arguments" \
        >&2
    exit 2
fi

resolve_selected_python

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
