#!/usr/bin/env python3
"""Grow a verified shared pool, preserving every existing credential and IPv6."""
import argparse
import fcntl
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
import time

import provision_server as provision
from migrate_server import digest

ROOT = Path(__file__).resolve().parent
RECORD = ROOT / 'expansion.json'
UNITS = Path('/etc/systemd/system')
FILES = ('full_proxy_config', 'proxy_configs', 'extracted_proxy', 'pool.json')


def save(record):
    provision.generator().atomic_write(RECORD, json.dumps(record, indent=2) + '\n')


def load():
    record = json.loads(RECORD.read_text())
    if record.get('kind') != 'shared_expansion' or record.get('version') != 1:
        raise ValueError('Unsupported expansion record')
    return record


def auth_config(rows):
    return (''.join(f"users {r['user']}:CL:{r['pass']}\n" for r in rows)
            + 'deny * * 0.0.0.0/0\ndeny * * ::ffff:0.0.0.0/96\n'
            + ''.join(f"allow {r['user']}\nparent 1000 extip {r['ipv6'].split('/')[0]} 0\n" for r in rows)
            + f"deny *\nproxy -6 -n -a -p{rows[0]['proxy_port']} -i{rows[0]['proxy_ip']}\n")


def read_pool(directory):
    module = provision.generator()
    rows = module.proxy_records(directory / 'proxy_configs')
    pool = json.loads((directory / 'pool.json').read_text())
    if (pool.get('architecture') != '3proxy-shared-v1'
            or pool.get('identity') != 'host_port_username' or pool.get('count') != len(rows) or not rows):
        raise ValueError('Unsupported or incomplete shared pool')
    network = ipaddress.IPv6Network(pool['ipv6_subnet'])
    users, addresses = set(), set()
    for row in rows:
        address = ipaddress.IPv6Interface(row['ipv6']).ip
        if (row['proxy_ip'] != pool['listen_ipv4'] or int(row['proxy_port']) != pool['listen_port']
                or address not in network or address in addresses or row['user'] in users
                or not re.fullmatch(r'[A-Za-z0-9_-]+', row['user'])
                or not re.fullmatch(r'[A-Za-z0-9_-]+', row['pass'])):
            raise ValueError('Invalid shared identity or address')
        users.add(row['user'])
        addresses.add(address)
    config = (directory / 'full_proxy_config').read_text()
    marker = 'auth strong\n'
    if config.count(marker) != 1 or config.split(marker)[1] != auth_config(rows):
        raise ValueError('Shared authorization configuration differs from the pool')
    extracted = ''.join(f"{r['proxy_ip']}:{r['proxy_port']}@{r['user']}:{r['pass']}\n" for r in rows)
    if (directory / 'extracted_proxy').read_text() != extracted:
        raise ValueError('Exported credentials differ from the pool')
    return rows, pool, config.split(marker)[0] + marker


def allocate(rows, pool, target, reserved):
    if target < len(rows):
        raise ValueError('Expansion never shrinks a pool')
    network = ipaddress.IPv6Network(pool['ipv6_subnet'])
    if network.prefixlen not in (48, 64) or (network.prefixlen == 48 and target > 65536):
        raise ValueError('Unsupported subnet or target count')
    used = {ipaddress.IPv6Address(a) for a in reserved}
    used.update(ipaddress.IPv6Interface(r['ipv6']).ip for r in rows)
    used.update((network.network_address, network.network_address + 1, network.network_address + 2))
    # New identities use unused /64s within a /48; legacy mappings stay untouched.
    prefixes = {int(a) >> 64 for a in used if a in network}
    users = {r['user'] for r in rows}
    result = list(rows)
    suffix = number = 0
    while len(result) < target:
        address = network.network_address + ((suffix << 64) + 0x66 if network.prefixlen == 48 else suffix + 3)
        suffix += 1
        if address not in network:
            raise ValueError('No unused addresses remain in the allocation')
        if address in used or (network.prefixlen == 48 and int(address) >> 64 in prefixes):
            continue
        while True:
            number += 1
            user = f'expanded_{number:05d}'
            if user not in users:
                break
        result.append(dict(user=user, **{'pass': secrets.token_urlsafe(24)},
                           proxy_ip=pool['listen_ipv4'], proxy_port=str(pool['listen_port']), ipv6=f'{address}/64'))
        users.add(user)
        used.add(address)
        prefixes.add(int(address) >> 64)
    return result


def check_hashes(directory, hashes):
    for name, value in hashes.items():
        if not (directory / name).is_file() or digest(directory / name) != value:
            raise ValueError(f'Expansion file changed: {name}')


def validate(record, *, mixed=False, before=False):
    directory = ROOT / 'generated_proxy_configs' / record['project']
    state = ROOT / record['state_directory']
    check_hashes(state / 'before', record['before_hashes'])
    check_hashes(state / 'after', record['after_hashes'])
    check_hashes(ROOT, record['unchanged_hashes'])
    if digest(UNITS / f"3proxy-{record['project']}.service") != record['unit_hash']:
        raise ValueError('Installed service changed during expansion')
    for name in FILES:
        expected = {record['before_hashes' if before else 'after_hashes'][name]}
        if mixed:
            expected.add(record['before_hashes'][name])
        if digest(directory / name) not in expected:
            raise ValueError(f'Live pool changed outside this expansion: {name}')
    return directory, state


def prepare(target):
    previous = load() if RECORD.exists() else None
    if previous and previous['status'] != 'complete':
        if previous['target_count'] != target:
            raise ValueError('Finish or recover the pending expansion first')
        validate(previous, mixed=True)
        return previous
    deployment = json.loads((ROOT / 'deployment.json').read_text())
    if deployment.get('status') != 'complete' or len(deployment.get('projects', [])) != 1:
        raise ValueError('Expansion requires a complete, verified single shared pool')
    project = deployment['projects'][0]
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', project):
        raise ValueError('Invalid project name')
    directory = ROOT / 'generated_proxy_configs' / project
    if previous:
        validate(previous)
    else:
        check_hashes(ROOT, deployment.get('artifact_hashes', {}))
    rows, pool, prefix = read_pool(directory)
    if target == len(rows):
        provision.wait_ready([project])
        return previous or {'status': 'complete', 'target_count': target, 'project': project}
    memory = provision.require_memory()
    resources = deployment.get('resources', {})
    reserve = resources.get('available_memory_reserve_bytes', provision.RESERVE_BYTES)
    if (target - len(rows)) * 2048 > memory['available_bytes'] - reserve:
        raise ValueError('Insufficient memory for the additional configuration')
    if target * 2048 > resources.get('memory_max_bytes', memory['available_bytes'] - reserve):
        raise ValueError('Expanded configuration exceeds the service memory budget')
    _, reserved = provision.generator().network_preflight(pool['interface'], pool['listen_ipv4'])
    if not {str(ipaddress.IPv6Interface(r['ipv6']).ip) for r in rows} <= set(reserved):
        raise ValueError('Existing pool addresses are missing from the interface')
    provision.wait_ready([project])
    new_rows = allocate(rows, pool, target, reserved)
    module = provision.generator()
    history = ROOT / 'expansion_state'
    history.mkdir(mode=0o700, exist_ok=True)
    state = Path(tempfile.mkdtemp(prefix=f'{len(rows)}-to-{target}-', dir=history))
    for sub in ('before', 'after'):
        (state / sub).mkdir(mode=0o700)
    for name in FILES:
        module.atomic_write(state / 'before' / name, (directory / name).read_text())
    if previous:
        module.atomic_write(state / 'previous_expansion.json', json.dumps(previous, indent=2) + '\n')
    module.atomic_write(state / 'after' / 'full_proxy_config', prefix + auth_config(new_rows))
    module.atomic_write(state / 'after' / 'proxy_configs', ''.join(' '.join(f'{k}:{v}' for k, v in r.items()) + '\n' for r in new_rows))
    module.atomic_write(state / 'after' / 'extracted_proxy', ''.join(f"{r['proxy_ip']}:{r['proxy_port']}@{r['user']}:{r['pass']}\n" for r in new_rows))
    module.atomic_write(state / 'after' / 'pool.json', json.dumps(dict(pool, count=target), indent=2) + '\n')
    read_pool(state / 'after')
    unit = UNITS / f'3proxy-{project}.service'
    if unit.read_bytes() != (directory / 'service.unit').read_bytes():
        raise ValueError('Installed service differs from the generated unit')
    unchanged = {str(p.relative_to(ROOT)): digest(p) for p in directory.iterdir() if p.is_file() and p.name not in FILES}
    unchanged.update({'deployment.json': digest(ROOT / 'deployment.json'),
                      '3proxy_binaries/3proxy': digest(ROOT / '3proxy_binaries/3proxy')})
    record = {'kind': 'shared_expansion', 'version': 1, 'status': 'prepared', 'project': project,
              'before_count': len(rows), 'target_count': target, 'interface': pool['interface'],
              'state_directory': str(state.relative_to(ROOT)), 'started_at': time.time(),
              'before_hashes': {n: digest(state / 'before' / n) for n in FILES},
              'after_hashes': {n: digest(state / 'after' / n) for n in FILES},
              'unchanged_hashes': unchanged, 'unit_hash': digest(unit),
              'code_revision': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()}
    save(record)
    return record


def publish(record, side):
    directory, state = validate(record, mixed=True)
    unit = UNITS / f"3proxy-{record['project']}.service"
    if digest(unit) != record['unit_hash']:
        raise ValueError('Installed service changed during expansion')
    for name in FILES:
        provision.generator().atomic_write(directory / name, (state / side / name).read_text())


def restart(record):
    subprocess.run(['systemctl', 'restart', f"3proxy-{record['project']}.service"], check=True)
    provision.wait_ready([record['project']])


def bind(record, state):
    spec = importlib.util.spec_from_file_location('binder', ROOT / '2_bind_ipv6_addresses.py')
    binder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(binder)
    binder.bind_addresses(binder.extract_ipv6_addresses(state / 'after' / 'proxy_configs'), record['interface'], 'add')


def rollback():
    record = load()
    if record['status'] == 'complete':
        raise ValueError('Verified pool may be in use; remove new identities from consumers before manual rollback')
    validate(record, mixed=True)
    record['status'] = 'rolling_back'
    save(record)
    publish(record, 'before')
    restart(record)
    # Never remove addresses: legacy and replacement services can share them.
    record.update(status='rolled_back', rolled_back_at=time.time())
    save(record)
    return record


def apply():
    record = load()
    if record['status'] in ('started_unverified', 'verification_failed', 'complete'):
        validate(record)
        provision.wait_ready([record['project']])
        return record
    if record['status'] not in ('prepared', 'applying', 'rolled_back'):
        raise ValueError('Recover the interrupted rollback before applying')
    _, state = validate(record, mixed=True)
    provision.require_memory()
    # Bind before publishing; service startup repeats the idempotent binding after reboot.
    bind(record, state)
    record['status'] = 'applying'
    save(record)
    try:
        publish(record, 'after')
        restart(record)
    except Exception:
        rollback()
        raise
    record.update(status='started_unverified', applied_at=time.time())
    save(record)
    return record


def verify(passed, count, report_sha256):
    record = load()
    if record['status'] not in ('started_unverified', 'verification_failed', 'complete'):
        raise ValueError('Only an applied expansion can be verified')
    validate(record)
    if not passed or count != record['target_count'] or not re.fullmatch(r'[a-f0-9]{64}', report_sha256 or ''):
        if record['status'] != 'complete':
            record['status'] = 'verification_failed'
            save(record)
        raise ValueError('Every old and new identity must pass external verification')
    provision.wait_ready([record['project']])
    record.update(status='complete', verified_count=count, verification_report_sha256=report_sha256, verified_at=time.time())
    save(record)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'apply', 'verify', 'rollback'))
    parser.add_argument('--target-count', type=int)
    parser.add_argument('--verification', choices=('passed', 'failed'))
    parser.add_argument('--verified-count', type=int, default=0)
    parser.add_argument('--report-sha256')
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise PermissionError('Expansion requires root')
    if args.action == 'prepare' and (args.target_count is None or args.target_count <= 0):
        parser.error('Positive --target-count required')
    os.umask(0o077)
    with ((ROOT / '.provision.lock').open('a') as lock,
          (ROOT / 'generated_proxy_configs/.allocation.lock').open('a') as allocation_lock):
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(allocation_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.action == 'prepare':
            record = prepare(args.target_count)
        elif args.action == 'apply':
            record = apply()
        elif args.action == 'rollback':
            record = rollback()
        else:
            record = verify(args.verification == 'passed', args.verified_count, args.report_sha256)
        print(json.dumps({k: record[k] for k in ('status', 'before_count', 'target_count', 'verified_count', 'project') if k in record}))


if __name__ == '__main__':
    main()
