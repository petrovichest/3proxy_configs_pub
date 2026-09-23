#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
version=0.9.5
if [[ -x 3proxy_binaries/3proxy && -f 3proxy_binaries/version && $(cat 3proxy_binaries/version) == "$version" ]]; then
    echo "3proxy $version already installed"
    exit 0
fi
build_dir=$(mktemp -d)
trap 'rm -rf "$build_dir"' EXIT
git clone --quiet --depth 1 --branch "$version" https://github.com/3proxy/3proxy.git "$build_dir/source"
make -C "$build_dir/source" -f Makefile.Linux
mkdir -p 3proxy_binaries
install -m 755 "$build_dir/source/bin/3proxy" 3proxy_binaries/3proxy.new
mv 3proxy_binaries/3proxy.new 3proxy_binaries/3proxy
printf '%s\n' "$version" > 3proxy_binaries/version
