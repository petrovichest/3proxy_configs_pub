#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
if [[ $EUID != 0 ]]; then
    exec sudo bash "$0" "$@"
fi
export DEBIAN_FRONTEND=noninteractive
packages=(python3-venv python3-pip git build-essential iproute2 logrotate ca-certificates)
missing=()
for package in "${packages[@]}"; do
    if ! dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -q 'install ok installed'; then
        missing+=("$package")
    fi
done
if (( ${#missing[@]} )); then
    apt-get update -q
    apt-get install -y "${missing[@]}"
fi
if [[ ! -x venv/bin/python ]]; then
    python3 -m venv venv
fi
requirements_hash=$(sha256sum requirements.txt | cut -d ' ' -f 1)
if [[ ! -f venv/requirements.sha256 || $(cat venv/requirements.sha256) != "$requirements_hash" ]]; then
    venv/bin/python -m pip install -r requirements.txt
    printf '%s\n' "$requirements_hash" > venv/requirements.sha256
fi
bash install_3proxy.sh
