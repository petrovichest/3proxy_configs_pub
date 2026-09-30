#!/usr/bin/env python3
"""Install recovery for an existing pool without restarting it or changing identities."""
import argparse
import importlib.util
import os
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parent


def render(project, interface, root):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', project):
        raise ValueError('Invalid project')
    if not re.fullmatch(r'[A-Za-z0-9_.:-]+', interface):
        raise ValueError('Invalid interface')
    if any(c in str(root) for c in ' \t\n%"'):
        raise ValueError('Unsupported installation path')
    unit = f'3proxy-{project}'
    repair = f'{unit}-addresses'
    return {
        f'{unit}.service.d/50-recovery.conf':
            '[Unit]\nStartLimitIntervalSec=0\n\n'
            '[Service]\nRestart=always\nRestartSec=1s\nTimeoutStopSec=5s\n',
        f'{repair}.service':
            f'[Unit]\nDescription=Restore missing IPv6 for {project}\n'
            'After=network-online.target\n\n[Service]\nType=oneshot\n'
            f'ExecCondition=/usr/bin/systemctl is-active --quiet {unit}.service\n'
            f'ExecStart={root}/venv/bin/python {root}/2_bind_ipv6_addresses.py '
            f'{project} --interface {interface} --action add_all --quiet\n'
            'TimeoutStartSec=45s\nNice=10\n',
        f'{repair}.timer':
            f'[Unit]\nDescription=Check proxy IPv6 for {project}\n\n'
            '[Timer]\nOnBootSec=10s\nOnUnitInactiveSec=5s\nAccuracySec=1s\n\n'
            '[Install]\nWantedBy=timers.target\n',
    }


def network_file(interface):
    result = subprocess.run(['networkctl', 'status', '--no-pager', '--full', interface],
                            text=True, capture_output=True, env=dict(os.environ, LC_ALL='C'))
    match = re.search(r'^\s*Network File:\s+(/\S+\.network)\s*$', result.stdout, re.M)
    if not match:
        raise RuntimeError('Cannot identify the active networkd configuration; no changes applied')
    return Path(match[1]).name


def install(project, interface):
    if os.geteuid() != 0:
        raise PermissionError('Installation requires root')
    files = render(project, interface, ROOT)
    # Read the existing pool, never regenerate it or its credentials.
    spec = importlib.util.spec_from_file_location('binder', ROOT / '2_bind_ipv6_addresses.py')
    binder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(binder)
    binder.extract_ipv6_addresses(ROOT / 'generated_proxy_configs' / project / 'proxy_configs')
    subprocess.run(['ip', 'link', 'show', 'dev', interface], check=True, stdout=subprocess.DEVNULL)
    networkd = subprocess.run(['systemctl', 'is-active', '--quiet', 'systemd-networkd']).returncode == 0
    network = network_file(interface) if networkd else None
    destinations = {Path('/etc/systemd/system') / name: body for name, body in files.items()}
    if network:
        destinations[Path('/etc/systemd/network') / (network + '.d') / '50-proxy-addresses.conf'] = (
            '# Preserve statically bound proxy IPv6 when networkd restarts.\n'
            '[Network]\nKeepConfiguration=static\n')
    for path, body in destinations.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + '.tmp')
        temporary.write_text(body)
        temporary.chmod(0o644)
        temporary.replace(path)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    if network:
        subprocess.run(['networkctl', 'reload'], check=True)
    subprocess.run(['systemctl', 'enable', '--now', f'3proxy-{project}-addresses.timer'], check=True)
    print(f'Recovery installed for {project}; running proxy was not restarted')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('project')
    parser.add_argument('--interface', required=True)
    args = parser.parse_args()
    install(args.project, args.interface)
