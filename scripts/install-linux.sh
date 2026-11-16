#!/usr/bin/env bash
# Install a candidate release; configure and start the service separately.
set -euo pipefail
if [[ $(id -u) != 0 ]]; then
  echo "Run with sudo: sudo bash scripts/install-linux.sh" >&2
  exit 1
fi
if [[ ! -r /etc/os-release ]]; then
  echo "Cannot identify this OS. Supported: Debian 13 and Ubuntu 24.04 LTS." >&2
  exit 1
fi
. /etc/os-release
case "${ID:-}:${VERSION_ID:-}" in
  debian:13|ubuntu:24.04) ;;
  *)
    echo "Supported: Debian 13 and Ubuntu 24.04 LTS. Detected: ${PRETTY_NAME:-unknown}." >&2
    exit 1
    ;;
esac
repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
apt-get update
if [[ $ID == ubuntu ]] && ! apt-cache show python3-libtorrent >/dev/null 2>&1; then
  echo "Ubuntu's Universe repository is required for python3-libtorrent and python3-venv." >&2
  echo "Enable it with: sudo add-apt-repository universe" >&2
  echo "Then rerun this installer." >&2
  exit 1
fi
apt-get install -y python3 python3-venv python3-libtorrent ca-certificates
# Use the distro interpreter that matches the native libtorrent extension.
/usr/bin/python3 - <<'PYTHON'
import sys
import libtorrent
assert sys.version_info >= (3, 11), "Python 3.11 or newer is required"
assert libtorrent.__version__.startswith("2."), "libtorrent 2.x is required"
print("System Python", sys.version.split()[0], "libtorrent", libtorrent.__version__)
PYTHON
if ! id infocon >/dev/null 2>&1; then
  useradd --system --user-group --home-dir /var/lib/infocon-librarian --shell /usr/sbin/nologin infocon
fi
install -d -m 0755 /opt/infocon-librarian/releases
install -d -o root -g infocon -m 0750 /etc/infocon-librarian
install -d -o infocon -g infocon -m 0700 /var/lib/infocon-librarian
# An unmounted local directory must never be writable by the service user.
if ! mountpoint -q /mnt/infocon; then
  install -d -o root -g root -m 0755 /mnt/infocon
fi
release_dir="/opt/infocon-librarian/releases/$(date -u +%Y%m%dT%H%M%SZ)-$$"
build_dir=$(mktemp -d)
trap 'rm -rf -- "$build_dir"' EXIT
tar -C "$repo_dir" --exclude=.git --exclude=.venv --exclude=build --exclude=dist \
  --exclude='*.egg-info' --exclude=__pycache__ --exclude=.pytest_cache \
  --exclude=.ruff_cache -cf - . | tar -C "$build_dir" -xf -
/usr/bin/python3 -m venv --system-site-packages "$release_dir"
"$release_dir/bin/python" -m pip install --constraint "$repo_dir/deploy/linux/constraints.txt" \
  "$build_dir[server]"
# Test from outside the source directory so editable imports cannot mask missing assets.
(cd / && "$release_dir/bin/python" - <<'PY'
from importlib.resources import files
import libtorrent
import waitress
from infocon_librarian.web.app import create_app
for name in ('index.html', 'app.js', 'app.css'):
    assert files('infocon_librarian.web').joinpath('static', name).is_file(), name
assert files('infocon_librarian.web').joinpath('templates', 'login.html').is_file()
assert create_app()
print('Installed package OK; libtorrent', libtorrent.__version__)
PY
)
if systemctl is-active --quiet infocon-librarian; then
  echo "Stop infocon-librarian and back up its state before activating this candidate:" >&2
  echo "$release_dir" >&2
  exit 1
fi
if [[ -L /opt/infocon-librarian/current ]]; then
  readlink /opt/infocon-librarian/current > /etc/infocon-librarian/previous-release
fi
ln -s "$release_dir" /opt/infocon-librarian/.current-new
mv -Tf /opt/infocon-librarian/.current-new /opt/infocon-librarian/current
if [[ ! -e /etc/infocon-librarian/server.toml ]]; then
  install -o root -g infocon -m 0640 "$repo_dir/deploy/linux/server.toml.example" \
    /etc/infocon-librarian/server.toml
fi
# Preserve locally edited mount paths and service settings during upgrades.
if [[ ! -e /etc/systemd/system/infocon-librarian.service ]]; then
  install -m 0644 "$repo_dir/deploy/linux/infocon-librarian.service" \
    /etc/systemd/system/infocon-librarian.service
fi
systemctl daemon-reload
echo 'Installation complete. Follow docs/linux-vm.md to configure the NAS, password and firewall.'
echo 'The service has not been enabled or started.'
