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
    resolved="$(command -v python3 2>/dev/null || true)"
    if [[ -n "$resolved" ]] && python_is_compatible "$resolved"; then
        printf '%s\n' "$resolved"
        return 0
    fi
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
            "Install a compatible interpreter, use the version-matched hosted uninstall.sh, or run the verified installed bin/agent-skills directly." \
            >&2
        return 1
    fi
}

REQUESTED_ENGINE="${AGENT_SKILLS_UNINSTALL_ENGINE:-auto}"
case "$REQUESTED_ENGINE" in
    auto|python|rust) ;;
    *)
        printf '%s\n' "AGENT_SKILLS_UNINSTALL_ENGINE must be auto, rust, or python" >&2
        exit 2
        ;;
esac

SOURCE_CHECKOUT_ROOT=''
# Rendered signed bootstraps must use their embedded release metadata even when
# they are saved beside a source checkout.
if [[ -n "$SCRIPT_SOURCE" && -z "$AGENT_SKILLS_EMBEDDED_VERSION" ]]; then
    REPO_ROOT="$(cd -P "$(dirname "$SCRIPT_SOURCE")" && pwd -P)"
    if [[ -f "$REPO_ROOT/scripts/uninstall_local.py" ]]; then
        SOURCE_CHECKOUT_ROOT="$REPO_ROOT"
    fi
fi

if [[ -n "${CODEX_HOME:-}" ]]; then
    NATIVE_TARGET_ROOT="$CODEX_HOME"
elif [[ -n "${HOME:-}" ]]; then
    NATIVE_TARGET_ROOT="$HOME/.codex"
else
    NATIVE_TARGET_ROOT=''
fi
NATIVE_ARGUMENTS=()
NATIVE_ARGUMENTS_PRESENT=0
NATIVE_DRY_RUN=0
NATIVE_JSON=0

validate_uninstall_target_arguments() {
    local argument value target_root_seen=0
    local target_root="$NATIVE_TARGET_ROOT"
    while (($#)); do
        argument="$1"
        case "$argument" in
            --target-root)
                (($# >= 2)) || return 1
                ((target_root_seen == 0)) || return 1
                value="$2"
                [[ -n "$value" && "$value" != --* ]] || return 1
                target_root_seen=1
                target_root="$value"
                shift 2
                ;;
            --target-root=*)
                ((target_root_seen == 0)) || return 1
                value="${argument#*=}"
                [[ -n "$value" ]] || return 1
                target_root_seen=1
                target_root="$value"
                shift
                ;;
            *)
                shift
                ;;
        esac
    done
    [[ -n "$target_root" && "$target_root" != *'~'* ]]
}

parse_native_request() {
    local argument target_root_seen=0
    while (($#)); do
        argument="$1"
        case "$argument" in
            --target-root)
                (($# >= 2)) || return 1
                ((target_root_seen == 0)) || return 1
                [[ -n "$2" && "$2" != --* ]] || return 1
                target_root_seen=1
                NATIVE_TARGET_ROOT="$2"
                shift 2
                ;;
            --target-root=*)
                ((target_root_seen == 0)) || return 1
                target_root_seen=1
                NATIVE_TARGET_ROOT="${argument#*=}"
                [[ -n "$NATIVE_TARGET_ROOT" ]] || return 1
                shift
                ;;
            --platform)
                (($# >= 2)) || return 1
                [[ -n "$2" ]] || return 1
                NATIVE_ARGUMENTS+=(--platform "$2")
                NATIVE_ARGUMENTS_PRESENT=1
                shift 2
                ;;
            --platform=*)
                argument="${argument#*=}"
                [[ -n "$argument" ]] || return 1
                NATIVE_ARGUMENTS+=(--platform "$argument")
                NATIVE_ARGUMENTS_PRESENT=1
                shift
                ;;
            --dry-run)
                NATIVE_DRY_RUN=1
                shift
                ;;
            --json)
                NATIVE_JSON=1
                shift
                ;;
            *)
                return 1
                ;;
        esac
    done
    [[ -n "$NATIVE_TARGET_ROOT" && "$NATIVE_TARGET_ROOT" != *'~'* ]] || return 1
    if ((NATIVE_DRY_RUN)); then
        NATIVE_ARGUMENTS+=(--dry-run)
        NATIVE_ARGUMENTS_PRESENT=1
    fi
    if ((NATIVE_JSON)); then
        NATIVE_ARGUMENTS+=(--json)
        NATIVE_ARGUMENTS_PRESENT=1
    fi
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

cleanup_native_copy() {
    if [[ -n "${NATIVE_RUN_DIR:-}" ]]; then
        rm -rf "$NATIVE_RUN_DIR"
        NATIVE_RUN_DIR=''
    fi
}

clear_native_copy_traps() {
    cleanup_native_copy
    trap - EXIT HUP INT TERM
}

prepare_release_native() {
    local executable="$NATIVE_TARGET_ROOT/bin/agent-skills"
    local actual_size actual_sha256
    [[ -f "$executable" && ! -L "$executable" && -x "$executable" ]] || return 1
    [[ "$native_size" =~ ^[1-9][0-9]*$ ]] || return 1
    ((${#native_size} <= 9)) || return 1
    ((native_size <= 134217728)) || return 1
    NATIVE_RUN_DIR="$(mktemp -d "${TMPDIR:-/tmp}/agent-skills-uninstall-native.XXXXXX")" \
        || return 1
    trap cleanup_native_copy EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM
    NATIVE_EXECUTABLE="$NATIVE_RUN_DIR/agent-skills"
    if ! head -c "$((native_size + 1))" "$executable" > "$NATIVE_EXECUTABLE"; then
        clear_native_copy_traps
        return 1
    fi
    chmod 700 "$NATIVE_EXECUTABLE"
    actual_size="$(wc -c < "$NATIVE_EXECUTABLE" | tr -d '[:space:]')" || {
        clear_native_copy_traps
        return 1
    }
    if [[ "$actual_size" != "$native_size" ]]; then
        clear_native_copy_traps
        return 1
    fi
    actual_sha256="$(sha256_file "$NATIVE_EXECUTABLE")" || {
        clear_native_copy_traps
        return 1
    }
    if [[ "$actual_sha256" != "$native_sha256" ]]; then
        clear_native_copy_traps
        return 1
    fi
}

run_source_native_uninstall() {
    local source_root="$1"
    local temporary native_executable native_status=0
    command -v cargo >/dev/null 2>&1 || {
        printf '%s\n' "native source uninstall requires cargo" >&2
        return 2
    }
    local required
    for required in \
        Cargo.toml \
        Cargo.lock \
        rust-toolchain.toml \
        crates/agent-skills/Cargo.toml; do
        [[ -f "$source_root/$required" && ! -L "$source_root/$required" ]] || {
            printf '%s\n' "native source uninstall input is missing or unsafe: $required" >&2
            return 2
        }
    done
    temporary="$(mktemp -d "${TMPDIR:-/tmp}/agent-skills-source-uninstall.XXXXXX")"
    SOURCE_NATIVE_DIR="$temporary"
    trap 'rm -rf "${SOURCE_NATIVE_DIR:-}"' EXIT HUP INT TERM
    (
        cd "$source_root"
        cargo build \
            --locked \
            --offline \
            --manifest-path "$source_root/Cargo.toml" \
            --package agent-skills-rs \
            --bin agent-skills-rs \
            --target-dir "$temporary/target"
    )
    native_executable="$temporary/target/debug/agent-skills-rs"
    [[ -f "$native_executable" && ! -L "$native_executable" && -x "$native_executable" ]] || {
        printf '%s\n' "cargo did not produce the expected native uninstaller" >&2
        return 2
    }
    local command=("$native_executable" uninstall "$NATIVE_TARGET_ROOT")
    if ((NATIVE_ARGUMENTS_PRESENT)); then
        command+=("${NATIVE_ARGUMENTS[@]}")
    fi
    AGENT_SKILLS_UNINSTALL_ENGINE_SELECTED=rust \
        "${command[@]}" || native_status=$?
    return "$native_status"
}

if ! validate_uninstall_target_arguments "$@"; then
    printf '%s\n' "uninstall target arguments are malformed or unsafe" >&2
    exit 2
fi

NATIVE_REQUEST_ELIGIBLE=0
if parse_native_request "$@"; then
    NATIVE_REQUEST_ELIGIBLE=1
fi

if [[ -n "$SOURCE_CHECKOUT_ROOT" ]]; then
    if [[ "$REQUESTED_ENGINE" != "python" && "$NATIVE_REQUEST_ELIGIBLE" == "1" ]] \
        && command -v cargo >/dev/null 2>&1; then
        run_source_native_uninstall "$SOURCE_CHECKOUT_ROOT"
        exit $?
    fi
    if [[ "$REQUESTED_ENGINE" == "rust" ]]; then
        printf '%s\n' \
            "forced Rust source uninstall requires cargo and compatible target/platform arguments" \
            >&2
        exit 2
    fi
    resolve_selected_python
    exec "$PYTHON_BIN" "$SOURCE_CHECKOUT_ROOT/scripts/uninstall_local.py" "$@"
fi

if [[ "$REQUESTED_ENGINE" != "python" && "$NATIVE_REQUEST_ELIGIBLE" == "1" ]]; then
    HOST_TARGET="$(native_host_target || true)"
    if [[ -n "$HOST_TARGET" ]] && agent_skills_select_native_record "$HOST_TARGET"; then
        if prepare_release_native; then
            native_status=0
            native_command=("$NATIVE_EXECUTABLE" uninstall "$NATIVE_TARGET_ROOT")
            if ((NATIVE_ARGUMENTS_PRESENT)); then
                native_command+=("${NATIVE_ARGUMENTS[@]}")
            fi
            AGENT_SKILLS_UNINSTALL_ENGINE_SELECTED=rust \
            AGENT_SKILLS_RELEASE_VERSION="$AGENT_SKILLS_EMBEDDED_VERSION" \
                "${native_command[@]}" || native_status=$?
            clear_native_copy_traps
            exit "$native_status"
        fi
    fi
fi

if [[ "$REQUESTED_ENGINE" == "rust" ]]; then
    printf '%s\n' \
        "forced Rust uninstall requires a supported host, compatible arguments, and an installed executable matching this release's native artifact" \
        >&2
    exit 2
fi

resolve_selected_python
command -v curl >/dev/null 2>&1 || {
    printf '%s\n' "hosted Python compatibility uninstall requires curl" >&2
    exit 2
}
command -v unzip >/dev/null 2>&1 || {
    printf '%s\n' "hosted Python compatibility uninstall requires unzip" >&2
    exit 2
}
[[ -n "$AGENT_SKILLS_EMBEDDED_SOURCE_FILENAME" \
    && -n "$AGENT_SKILLS_EMBEDDED_SOURCE_SHA256" \
    && -n "$AGENT_SKILLS_EMBEDDED_SOURCE_SIZE" \
    && -n "$AGENT_SKILLS_EMBEDDED_SOURCE_ROOT" \
    && ( "$AGENT_SKILLS_EMBEDDED_ASSET_BASE_URL" == https://* \
        || ( "${AGENT_SKILLS_ALLOW_FILE_URL:-}" == "1" \
            && "$AGENT_SKILLS_EMBEDDED_ASSET_BASE_URL" == file://* ) ) ]] || {
    printf '%s\n' "hosted Python compatibility uninstall requires embedded v2 release metadata" >&2
    exit 2
}

download_verified_source() {
    local destination="$1" protocols actual_size actual_sha256
    protocols="=https"
    if [[ "${AGENT_SKILLS_ALLOW_FILE_URL:-}" == "1" ]]; then
        protocols="=https,file"
    fi
    curl --fail --silent --show-error --location \
        --proto "$protocols" --proto-redir "$protocols" \
        --connect-timeout 10 --max-time 120 \
        --max-filesize "$AGENT_SKILLS_EMBEDDED_SOURCE_SIZE" \
        -o "$destination" \
        "$AGENT_SKILLS_EMBEDDED_ASSET_BASE_URL$AGENT_SKILLS_EMBEDDED_SOURCE_FILENAME"
    actual_size="$(wc -c < "$destination" | tr -d '[:space:]')"
    [[ "$actual_size" == "$AGENT_SKILLS_EMBEDDED_SOURCE_SIZE" ]] || {
        printf '%s\n' "downloaded source size does not match embedded release metadata" >&2
        return 1
    }
    actual_sha256="$(sha256_file "$destination")"
    [[ "$actual_sha256" == "$AGENT_SKILLS_EMBEDDED_SOURCE_SHA256" ]] || {
        printf '%s\n' "downloaded source SHA-256 does not match embedded release metadata" >&2
        return 1
    }
}

COMPATIBILITY_DIR="$(mktemp -d "${TMPDIR:-/tmp}/agent-skills-uninstall-bootstrap.XXXXXX")"
cleanup() {
    rm -rf "$COMPATIBILITY_DIR"
}
trap cleanup EXIT HUP INT TERM
SOURCE_ARCHIVE="$COMPATIBILITY_DIR/$AGENT_SKILLS_EMBEDDED_SOURCE_FILENAME"
download_verified_source "$SOURCE_ARCHIVE"
mkdir "$COMPATIBILITY_DIR/extracted"
unzip -qq "$SOURCE_ARCHIVE" -d "$COMPATIBILITY_DIR/extracted"
EXTRACTED_ROOT="$COMPATIBILITY_DIR/extracted/$AGENT_SKILLS_EMBEDDED_SOURCE_ROOT"
[[ -d "$EXTRACTED_ROOT" && ! -L "$EXTRACTED_ROOT" \
    && -f "$EXTRACTED_ROOT/scripts/uninstall_local.py" \
    && ! -L "$EXTRACTED_ROOT/scripts/uninstall_local.py" ]] || {
    printf '%s\n' "verified source archive uninstall entrypoint is missing or unsafe" >&2
    exit 2
}
AGENT_SKILLS_UNINSTALL_ENGINE_SELECTED=python \
AGENT_SKILLS_RELEASE_VERSION="$AGENT_SKILLS_EMBEDDED_VERSION" \
    exec "$PYTHON_BIN" "$EXTRACTED_ROOT/scripts/uninstall_local.py" "$@"
