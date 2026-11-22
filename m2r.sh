#!/usr/bin/env bash
# M2R launcher: installs prerequisites, fetches m2r.py, runs it.
# bash <(curl -Ls https://raw.githubusercontent.com/ali-ar1/M2R/main/m2r.sh)
set -e

REPO_RAW="https://raw.githubusercontent.com/ali-ar1/M2R/main"
APP_DIR="/opt/M2R"

[ "$(id -u)" -eq 0 ] || { echo "[!] Run as root (sudo -i)."; exit 1; }

if command -v apt-get >/dev/null; then
    PM="apt-get install -y -qq"; UPDATE="apt-get update -qq"
    PKGS="python3 python3-bcrypt python3-xxhash default-mysql-client postgresql-client curl"
elif command -v dnf >/dev/null; then
    PM="dnf install -y -q"; UPDATE=":"
    PKGS="python3 python3-bcrypt mariadb postgresql curl"
elif command -v yum >/dev/null; then
    PM="yum install -y -q"; UPDATE=":"
    PKGS="python3 python3-bcrypt mariadb postgresql curl"
else
    PM=""
fi

need=""
command -v python3 >/dev/null || need="$need python3"
command -v curl >/dev/null || need="$need curl"
command -v mysql >/dev/null || command -v mariadb >/dev/null || need="$need mysql-client"
command -v psql >/dev/null || need="$need psql"
python3 -c "import bcrypt, xxhash" 2>/dev/null || need="$need python-libs"

if [ -n "$need" ]; then
    if [ -z "$PM" ]; then
        echo "[~] Unknown package manager, install manually:$need"
    else
        echo "[+] Installing prerequisites:$need"
        $UPDATE >/dev/null 2>&1 || true
        # one by one: a package missing from this distro must not block the others
        for p in $PKGS; do DEBIAN_FRONTEND=noninteractive $PM "$p" >/dev/null 2>&1 || echo "[~] Could not install $p (optional unless noted)"; done
    fi
fi
command -v python3 >/dev/null || { echo "[!] python3 is required."; exit 1; }

# use a local copy when run from a clone, otherwise download the latest
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
if [ -f "$HERE/m2r.py" ]; then
    SCRIPT="$HERE/m2r.py"
else
    mkdir -p "$APP_DIR"
    curl -fsSL "$REPO_RAW/m2r.py" -o "$APP_DIR/m2r.py" || { echo "[!] Download failed."; exit 1; }
    SCRIPT="$APP_DIR/m2r.py"
fi

exec python3 "$SCRIPT" "$@" </dev/tty
