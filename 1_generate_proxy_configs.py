#!/usr/bin/env python3
"""Generate persistent IPv4-port to IPv6 mappings, optionally starting systemd."""
import argparse
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import subprocess
import tempfile

from setup_proxy_logging import add_logging_to_config, logging_block

BASE_OUTPUT_DIR = Path(__file__).resolve().parent / 'generated_proxy_configs'
STATE_FILE = BASE_OUTPUT_DIR / 'proxy_states.json'
DEFAULT_START_PORT = 10000
DEFAULT_END_PORT = 65000
SERVICE_LIMITS = {'LimitNOFILE': 131072, 'LimitNPROC': 32768, 'TasksMax': 8192}


def atomic_write(path, content, mode=0o600):
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def get_state():
    if not STATE_FILE.exists():
        return {}
    state = json.loads(STATE_FILE.read_text())
    if not isinstance(state, dict):
        raise ValueError('Invalid allocation state')
    for value in state.values():
        if not isinstance(value.get('latest_port'), int) or not isinstance(value.get('ipv6_subnets'), dict):
            raise ValueError('Invalid allocation state')
        for subnet in value['ipv6_subnets'].values():
            if not isinstance(subnet.get('latest_suffix_increment'), int) or subnet['latest_suffix_increment'] < 0:
                raise ValueError('Invalid IPv6 allocation state')
    return state


def proxy_records(path):
    records = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        row = dict(field.split(':', 1) for field in line.split())
        ipaddress.IPv4Address(row['proxy_ip'])
        ipaddress.IPv6Interface(row['ipv6'])
        if not 1 <= int(row['proxy_port']) <= 65535:
            raise ValueError('Invalid proxy port')
        records.append(row)
    return records


def inspect_projects():
    projects, endpoints, addresses = {}, set(), set()
    for directory in sorted(BASE_OUTPUT_DIR.iterdir()):
        if not directory.is_dir() or directory.name.startswith('.'):
            continue
        if not (directory / 'proxy_configs').exists():
            raise ValueError(f'Incomplete project: {directory.name}')
        records = proxy_records(directory / 'proxy_configs')
        config = (directory / 'full_proxy_config').read_text()
        config_mappings = set()
        for line in config.splitlines():
            if line.startswith('proxy '):
                parts = shlex.split(line)
                config_mappings.add((next(p[2:] for p in parts if p.startswith('-i')),
                                     int(next(p[2:] for p in parts if p.startswith('-p'))),
                                     str(ipaddress.IPv6Address(next(p[2:] for p in parts if p.startswith('-e'))))))
        expected = set()
        for row in records:
            endpoint = (row['proxy_ip'], int(row['proxy_port']))
            address = str(ipaddress.IPv6Interface(row['ipv6']).ip)
            if endpoint in endpoints or address in addresses:
                raise ValueError('Duplicate endpoint or IPv6 in generated projects')
            endpoints.add(endpoint)
            addresses.add(address)
            expected.add((*endpoint, address))
        if not records or expected != config_mappings:
            raise ValueError(f'Configuration mismatch: {directory.name}')
        projects[directory.name] = records
    return projects, endpoints, addresses


def render_project(directory, project, records, interface):
    root = BASE_OUTPUT_DIR.parent
    user, password = records[0]['user'], records[0]['pass']
    config = (f'maxconn 10000\nnscache 65536\ntimeouts 1 5 30 60 180 1800 15 60\n'
              f'setgid 65535\nsetuid 65535\nflush\nauth strong\nusers {user}:CL:{password}\n'
              f'deny * * 0.0.0.0/0\ndeny * * ::ffff:0.0.0.0/96\nallow {user}\n')
    config += ''.join(f"proxy -64 -n -a -p{r['proxy_port']} -i{r['proxy_ip']} -e{r['ipv6'].split('/')[0]}\n" for r in records)
    atomic_write(directory / 'full_proxy_config', add_logging_to_config(config, project))
    atomic_write(directory / 'proxy_configs', ''.join(' '.join(f'{k}:{v}' for k,v in r.items())+'\n' for r in records))
    atomic_write(directory / 'extracted_proxy', ''.join(f"{r['proxy_ip']}:{r['proxy_port']}@{r['user']}:{r['pass']}\n" for r in records))
    final = BASE_OUTPUT_DIR / project
    unit = (f'[Unit]\nDescription=3proxy Service for {project}\nWants=network-online.target\nAfter=network-online.target\n\n'
            f'[Service]\nType=simple\nUser=root\nWorkingDirectory={final}\n'
            f'ExecStartPre={root}/venv/bin/python {root}/2_bind_ipv6_addresses.py {project} --interface {interface} --action add_all\n'
            f'ExecStart={root}/3proxy_binaries/3proxy full_proxy_config\n'
            + ''.join(f'{key}={value}\n' for key,value in SERVICE_LIMITS.items()) +
            'TimeoutStartSec=180\nRestart=on-failure\nRestartSec=3\n\n'
            '[Install]\nWantedBy=multi-user.target\n')
    atomic_write(directory / 'service.unit', unit, 0o644)
    scripts = {
        'setup_network_ipv6.sh': f'exec "{root}/venv/bin/python" "{root}/2_bind_ipv6_addresses.py" {project} --interface {interface} --action add_all',
        'bind.sh': f'exec "{root}/venv/bin/python" "{root}/2_bind_ipv6_addresses.py" {project} --interface {interface} "$@"',
        'unbind.sh': f'exec "{root}/venv/bin/python" "{root}/2_bind_ipv6_addresses.py" {project} --interface {interface} "$@" --action del',
        'proxy_checker.sh': f'exec "{root}/venv/bin/python" "{root}/4_proxy_checker.py" --project-name {project} "$@"',
        'start_systemctl.sh': (f'python3 "{root}/setup_proxy_logging.py" "{final}/full_proxy_config"\n'
            f'install -m 644 "{final}/service.unit" /etc/systemd/system/3proxy-{project}.service\n'
            f'systemctl daemon-reload\nsystemctl enable --now 3proxy-{project}.service\n'
            f'systemctl is-active --quiet 3proxy-{project}.service'),
        'stop_systemctl.sh': (f'systemctl disable --now 3proxy-{project}.service\n'
            f'rm -f /etc/systemd/system/3proxy-{project}.service\nsystemctl daemon-reload'),
        'start.sh': f'python3 "{root}/setup_proxy_logging.py" "{final}/full_proxy_config"\nexec pm2 start "{root}/3proxy_binaries/3proxy" --name {project} -- full_proxy_config',
    }
    for name, command in scripts.items():
        atomic_write(directory / name, f'#!/bin/bash\nset -euo pipefail\ncd -- "$(dirname -- "$0")"\n{command}\n', 0o700)


def generate_proxy_configs(num_proxies, project_name, ipv6_subnet, interface, external_ipv4, *, target=False, batch_size=1000, reserved_ports=(), reserved_addresses=()):
    logging_block(project_name)
    network = ipaddress.IPv6Network(ipv6_subnet, strict=True)
    ipaddress.IPv4Address(external_ipv4)
    if network.prefixlen not in (48, 64):
        raise ValueError('Only /48 and /64 are supported')
    if num_proxies <= 0 or not 1 <= batch_size <= 1000:
        raise ValueError('Positive proxy count and batch size 1..1000 required')
    if not re.fullmatch(r'[A-Za-z0-9_.:-]+', interface):
        raise ValueError('Invalid network interface')
    if any(c.isspace() for c in str(BASE_OUTPUT_DIR)):
        raise ValueError('Installation path must not contain whitespace')
    BASE_OUTPUT_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (BASE_OUTPUT_DIR / '.allocation.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = get_state()
        projects, endpoints, used_addresses = inspect_projects()
        names = ([n for n in projects if re.fullmatch(re.escape(project_name)+r'_\d+', n)] if target else [project_name] if project_name in projects else [])
        existing = sum(len(projects[n]) for n in names)
        for n in names:
            if any(r['proxy_ip'] != external_ipv4 or ipaddress.IPv6Interface(r['ipv6']).ip not in network for r in projects[n]):
                raise ValueError('Existing project belongs to another address or subnet')
        if existing > num_proxies or (existing and not target and existing != num_proxies):
            raise ValueError('Existing proxies are preserved; use a new project or larger target')
        if existing == num_proxies:
            return sorted(names)
        entry = state.setdefault(external_ipv4, {'latest_port': DEFAULT_START_PORT-1, 'ipv6_subnets': {}})
        subnet = entry['ipv6_subnets'].setdefault(str(network), {'latest_suffix_increment': 0})
        port = max(entry['latest_port']+1, max((p+1 for ip,p in endpoints if ip == external_ipv4), default=DEFAULT_START_PORT))
        suffix = subnet['latest_suffix_increment']
        excluded_ports = {p for ip,p in endpoints if ip == external_ipv4} | set(reserved_ports)
        excluded_ips = used_addresses | {str(ipaddress.IPv6Address(a)) for a in reserved_addresses} | {str(network.network_address+1), str(network.network_address+2)}
        allocation = []
        for _ in range(num_proxies-existing):
            while port in excluded_ports:
                port += 1
            if port > DEFAULT_END_PORT:
                raise ValueError('Not enough available proxy ports for the complete requested count')
            while True:
                # /48 keeps the ::66 host offset used by earlier generator versions.
                address = ipaddress.IPv6Address(int(network.network_address) + ((suffix << 64) + 0x66 if network.prefixlen == 48 else suffix+2))
                suffix += 1
                if address not in network:
                    raise ValueError('IPv6 subnet exhausted')
                if str(address) not in excluded_ips:
                    break
            allocation.append((port, str(address)))
            port += 1
        next_batch = max((int(n.rsplit('_',1)[1]) for n in names), default=0)+1
        chunk_size = batch_size if target else len(allocation)
        for offset in range(0, len(allocation), chunk_size):
            name = f'{project_name}_{next_batch}' if target else project_name
            next_batch += 1
            if (BASE_OUTPUT_DIR / name).exists():
                raise ValueError('Project already exists')
            password = secrets.token_urlsafe(24)
            records = [dict(user=name, **{'pass':password}, proxy_ip=external_ipv4, proxy_port=str(p), ipv6=f'{a}/64') for p,a in allocation[offset:offset+chunk_size]]
            with tempfile.TemporaryDirectory(prefix='.staging-', dir=BASE_OUTPUT_DIR) as temp:
                render_project(Path(temp), name, records, interface)
                # Reserve the complete allocation before publishing any project.
                entry['latest_port'] = port-1
                subnet['latest_suffix_increment'] = suffix
                atomic_write(STATE_FILE, json.dumps(state, indent=2)+'\n')
                os.rename(temp, BASE_OUTPUT_DIR / name)
            names.append(name)
            print(f'Created {name}: {len(records)} proxies', flush=True)
        return sorted(names)


def network_preflight(interface, external_ipv4):
    data = json.loads(subprocess.check_output(['ip','-j','addr','show','dev',interface], text=True))
    addresses = [a['local'] for link in data for a in link['addr_info']]
    if external_ipv4 not in addresses:
        raise ValueError('Listening IPv4 is not assigned to this interface')
    routes = json.loads(subprocess.check_output(['ip','-j','-6','route','show','default'], text=True))
    if not any(r.get('dev') == interface for r in routes):
        raise ValueError('Configure the IPv6 default route before starting proxies')
    listeners = subprocess.check_output(['ss','-H','-ltn'], text=True)
    ports = {int(l.split()[3].rsplit(':',1)[1]) for l in listeners.splitlines()}
    return ports, [a for a in addresses if ':' in a]


def configure_allocator(unit_path, arenas):
    """Opt-in glibc arena limit; a repeated application does not restart a service."""
    if arenas<0:
        raise ValueError('Malloc arena limit cannot be negative')
    current=unit_path.read_text()
    line=f'Environment=MALLOC_ARENA_MAX={arenas}\n'
    updated=re.sub(r'^Environment=MALLOC_ARENA_MAX=\d+\n','',current,flags=re.M)
    if arenas:
        updated=updated.replace('[Service]\n','[Service]\n'+line,1)
    if updated==current:
        return False
    atomic_write(unit_path,updated,0o644)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('num_proxies', type=int, nargs='?')
    parser.add_argument('project_name', nargs='?')
    parser.add_argument('--target-count', type=int)
    parser.add_argument('--project-prefix')
    parser.add_argument('--batch-size', type=int, default=1000)
    parser.add_argument('--ipv6-subnet')
    parser.add_argument('--interface')
    parser.add_argument('--external-ipv4')
    parser.add_argument('--start', action='store_true')
    parser.add_argument('--malloc-arenas',type=int,help='Opt-in glibc arena limit (0 restores default); restarts changed services')
    args = parser.parse_args()
    if args.malloc_arenas is not None and (args.malloc_arenas<0 or not args.start):
        parser.error('--malloc-arenas requires a nonnegative count and --start')
    target = args.target_count is not None
    count = args.target_count if target else args.num_proxies
    if count is None:
        count = int(input('Количество прокси: '))
    project = args.project_prefix if target else args.project_name
    project = project or input('Имя проекта: ').strip()
    subnet = args.ipv6_subnet or input('IPv6 подсеть: ').strip()
    interface = args.interface or input('Сетевой интерфейс: ').strip()
    ipv4 = args.external_ipv4 or input('Внешний IPv4: ').strip()
    if args.start:
        if os.geteuid() != 0:
            raise PermissionError('--start requires root')
        defaults = subprocess.check_output(['systemctl','show','--property=DefaultLimitNOFILE,DefaultLimitNPROC,DefaultTasksMax'],text=True)
        for line in defaults.splitlines():
            key,value = line.split('=',1)
            key = key.removeprefix('Default')
            if key in SERVICE_LIMITS:
                SERVICE_LIMITS[key] = 'infinity' if value == 'infinity' else max(SERVICE_LIMITS[key],int(value))
    ports, addresses = network_preflight(interface, ipv4) if args.start else ((), ())
    names = generate_proxy_configs(count, project, subnet, interface, ipv4, target=target, batch_size=args.batch_size, reserved_ports=ports, reserved_addresses=addresses)
    if args.start:
        if os.geteuid() != 0:
            raise PermissionError('--start requires root')
        current = int(subprocess.check_output(['sysctl','-n','kernel.threads-max'],text=True))
        sysctl_file = Path('/etc/sysctl.d/90-3proxy-capacity.conf')
        desired = max(current, 32768)
        atomic_write(sysctl_file, f'kernel.threads-max = {desired}\n', 0o644)
        subprocess.run(['sysctl','-p',str(sysctl_file)], check=True)
        for name in names:
            restart=False
            unit=f'3proxy-{name}.service'
            if args.malloc_arenas is not None and configure_allocator(BASE_OUTPUT_DIR/name/'service.unit',args.malloc_arenas):
                restart=subprocess.run(['systemctl','is-active','--quiet',unit],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0
            subprocess.run(['bash',str(BASE_OUTPUT_DIR/name/'start_systemctl.sh')], check=True)
            if restart:
                subprocess.run(['systemctl','restart',unit],check=True)
    print(json.dumps({'projects':names,'total':count}), flush=True)


if __name__ == '__main__':
    main()
