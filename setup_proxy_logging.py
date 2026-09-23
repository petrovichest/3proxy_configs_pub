#!/usr/bin/env python3
"""Install bounded 3proxy connection logs for a generated project."""

import argparse
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile


LOG_DIR = Path("/var/log/3proxy")
LOGROTATE_CONFIG = Path("/etc/3proxy-logrotate.conf")
ROTATE_SERVICE = Path("/etc/systemd/system/3proxy-logrotate.service")
ROTATE_TIMER = Path("/etc/systemd/system/3proxy-logrotate.timer")
START_MARKER = "# BEGIN MANAGED 3PROXY LOGGING"
END_MARKER = "# END MANAGED 3PROXY LOGGING"
PROJECT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
UNMANAGED_LOG = re.compile(r"^\s*(?:log|logformat|rotate)\s", re.MULTILINE)


def logging_block(project_name):
    if not PROJECT_NAME.fullmatch(project_name):
        raise ValueError("Project name must contain only letters, digits, dots, underscores or hyphens")
    return (
        f"{START_MARKER}\n"
        f"log {LOG_DIR / (project_name + '.log')}\n"
        'logformat "G%Y-%m-%dT%H:%M:%S %C %p %R %E %D %I %O"\n'
        f"{END_MARKER}\n"
    )


def add_logging_to_config(config, project_name):
    block = logging_block(project_name)
    if START_MARKER in config or END_MARKER in config:
        if config.count(START_MARKER) != 1 or config.count(END_MARKER) != 1:
            raise ValueError("Incomplete or duplicated managed logging block")
        if block not in config:
            raise ValueError("Existing managed logging block differs from expected project")
        return config
    if UNMANAGED_LOG.search(config):
        raise ValueError("Config already has unmanaged logging directives")
    return block + config


LOGROTATE_CONTENT = """/var/log/3proxy/*.log {
    size 16M
    rotate 4
    compress
    copytruncate
    missingok
    notifempty
}
"""

SERVICE_CONTENT = """[Unit]
Description=Rotate 3proxy connection logs

[Service]
Type=oneshot
ExecStart=/usr/sbin/logrotate -s /var/lib/logrotate/3proxy.status /etc/3proxy-logrotate.conf
"""

TIMER_CONTENT = """[Unit]
Description=Check 3proxy log sizes every minute

[Timer]
OnCalendar=*-*-* *:*:00
AccuracySec=1s

[Install]
WantedBy=timers.target
"""


def write_if_changed(path, content, mode):
    if path.exists() and path.read_text() == content:
        path.chmod(mode)
        return False
    path.write_text(content)
    path.chmod(mode)
    return True


def install_system_logging(project_name):
    if os.geteuid() != 0:
        raise PermissionError("Run setup_proxy_logging.py as root")
    if shutil.which("logrotate") is None:
        raise RuntimeError("Install logrotate before configuring 3proxy logging")

    if LOG_DIR.is_symlink():
        raise RuntimeError(f"Refusing symlinked log directory: {LOG_DIR}")
    LOG_DIR.mkdir(mode=0o750, parents=True, exist_ok=True)
    os.chown(LOG_DIR, 0, 65535)
    LOG_DIR.chmod(0o750)
    log_path = LOG_DIR / (project_name + ".log")
    if log_path.is_symlink():
        raise RuntimeError(f"Refusing symlinked log file: {log_path}")
    log_path.touch(exist_ok=True)
    os.chown(log_path, 65535, 65535)
    log_path.chmod(0o600)

    write_if_changed(LOGROTATE_CONFIG, LOGROTATE_CONTENT, 0o644)
    units_changed = write_if_changed(ROTATE_SERVICE, SERVICE_CONTENT, 0o644)
    units_changed |= write_if_changed(ROTATE_TIMER, TIMER_CONTENT, 0o644)
    if units_changed:
        subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "--now", ROTATE_TIMER.name], check=True)


def update_config(config_path, project_name):
    current = config_path.read_text()
    updated = add_logging_to_config(current, project_name)
    if updated == current:
        return False

    metadata = config_path.stat()
    fd, temp_name = tempfile.mkstemp(prefix=".full_proxy_config.", dir=config_path.parent)
    try:
        os.fchmod(fd, stat.S_IMODE(metadata.st_mode))
        os.fchown(fd, metadata.st_uid, metadata.st_gid)
        with os.fdopen(fd, "w") as temp_file:
            temp_file.write(updated)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_name, config_path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path, help="Path to generated full_proxy_config")
    args = parser.parse_args()
    config_path = args.config.resolve(strict=True)
    project_name = config_path.parent.name
    if config_path.name != "full_proxy_config":
        parser.error("Expected a generated full_proxy_config file")

    # Check for conflicts before changing system files.
    add_logging_to_config(config_path.read_text(), project_name)
    install_system_logging(project_name)
    changed = update_config(config_path, project_name)
    print(f"3proxy logging ready for {project_name}; config {'updated' if changed else 'unchanged'}")


if __name__ == "__main__":
    main()
