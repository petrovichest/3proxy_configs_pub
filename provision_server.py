#!/usr/bin/env python3
"""Create one proxy pool on a clean server; failures require manual recovery."""
import argparse
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import time

from server_preflight import inspect, memory_info

ROOT = Path(__file__).resolve().parent
RECORD = ROOT / 'deployment.json'
RESERVE_BYTES = 512 * 1024**2


def generator():
    spec = importlib.util.spec_from_file_location('generator', ROOT / '1_generate_proxy_configs.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def save(record):
    generator().atomic_write(RECORD, json.dumps(record, indent=2) + '\n')


def require_memory():
    memory = memory_info()
    if memory['available_bytes'] < RESERVE_BYTES:
        raise RuntimeError('Less than 512 MiB RAM remains available; requested pool is incomplete')
    return memory


def wait_ready(projects, deadline=180):
    module = generator()
    expected = set()
    for project in projects:
        expected.update((r['proxy_ip'], int(r['proxy_port']))
                        for r in module.proxy_records(module.BASE_OUTPUT_DIR / project / 'proxy_configs'))
    until = time.monotonic() + deadline
    while time.monotonic() < until:
        require_memory()
        for project in projects:
            properties = dict(line.split('=', 1) for line in subprocess.check_output(
                ['systemctl', 'show', f'3proxy-{project}.service', '--property=ActiveState,NRestarts,MainPID'],
                text=True).splitlines())
            if properties.get('ActiveState') != 'active' or not int(properties.get('MainPID', 0)):
                raise RuntimeError(f'Proxy service for {project} is not active')
            if int(properties.get('NRestarts', 0)):
                raise RuntimeError(f'Proxy service for {project} restarted during installation')
        listeners = subprocess.check_output(['ss', '-H', '-lnt'], text=True)
        listening = {(line.split()[3].rsplit(':', 1)[0], int(line.split()[3].rsplit(':', 1)[1]))
                     for line in listeners.splitlines()}
        if expected <= listening:
            return
        time.sleep(.5)
    raise TimeoutError('Not all requested listeners became ready')


def create(args):
    if args.count is None or args.count <= 0:
        raise ValueError('A positive --count is required')
    module = generator()
    network = inspect(ROOT, interface=args.interface, ipv4=args.external_ipv4, subnet=args.ipv6_subnet)
    memory = require_memory()
    budget = memory['available_bytes'] - RESERVE_BYTES
    if args.count * 2048 > budget:
        raise RuntimeError('Insufficient RAM to construct the complete requested configuration')
    module.SERVICE_LIMITS['MemoryMax'] = budget
    ports, addresses = module.network_preflight(network['interface'], network['ipv4'])
    record = {'version': 1, 'status': 'creating', 'requested_count': args.count,
              'created_count': 0, 'projects': [], 'network': network,
              'architecture': '3proxy-shared-v1', 'identity': 'host_port_username',
              'resources': {'memory_max_bytes': budget, 'available_memory_reserve_bytes': RESERVE_BYTES},
              'started_at': time.time(), 'code_revision': subprocess.check_output(
                  ['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()}
    save(record)
    try:
        projects = module.generate_shared_pool(args.count, args.project_prefix,
                    network['ipv6_subnet'], network['interface'], network['ipv4'],
                    reserved_ports=ports, reserved_addresses=addresses)
        record['projects'] = projects
        record['created_count'] = sum(len(module.proxy_records(module.BASE_OUTPUT_DIR / p / 'proxy_configs'))
                                      for p in projects)
        save(record)
        if record['created_count'] != args.count:
            raise RuntimeError('Generated count differs from the requested count')
        for project in projects:
            require_memory()
            subprocess.run(['bash', str(module.BASE_OUTPUT_DIR / project / 'start_systemctl.sh')], check=True)
        wait_ready(projects)
        record['status'] = 'started_unverified'
        record['memory_after_start'] = require_memory()
        record['started_proxies'] = args.count
        save(record)
        print(json.dumps({'status': record['status'], 'requested': args.count, 'started': args.count,
                          'projects': projects, 'network': network}), flush=True)
    except BaseException as exc:
        # Quiesce only the newly created services; retain addresses, files and credentials.
        for project in record['projects']:
            subprocess.run(['systemctl', 'disable', '--now', f'3proxy-{project}.service'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        record['status'] = 'failed'
        record['error'] = type(exc).__name__ + ': ' + str(exc)
        # Also retain projects published just before a generation failure.
        record['projects'] = sorted(p.name for p in module.BASE_OUTPUT_DIR.iterdir()
                                    if p.is_dir() and not p.name.startswith('.')) if module.BASE_OUTPUT_DIR.exists() else []
        record['created_count'] = sum(len(module.proxy_records(module.BASE_OUTPUT_DIR / p / 'proxy_configs'))
                                      for p in record['projects'])
        save(record)
        raise


def finalize(verification):
    record = json.loads(RECORD.read_text())
    if record['status'] != 'started_unverified':
        raise ValueError('Only a newly started, unverified pool can be finalized')
    if verification == 'passed':
        try:
            if record['requested_count'] != record['created_count']:
                raise RuntimeError('Cannot finalize an incomplete pool')
            wait_ready(record['projects'])
        except Exception as exc:
            record['status'] = 'verification_failed'
            record['error'] = type(exc).__name__ + ': ' + str(exc)
            save(record)
            raise
    record['status'] = 'complete' if verification == 'passed' else 'verification_failed'
    record['external_verification'] = verification
    record['finished_at'] = time.time()
    save(record)
    print(json.dumps({'status': record['status'], 'requested': record['requested_count'],
                      'created': record['created_count']}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('create', 'finalize'))
    parser.add_argument('--count', type=int)
    parser.add_argument('--project-prefix', default='capacity')
    parser.add_argument('--interface')
    parser.add_argument('--external-ipv4')
    parser.add_argument('--ipv6-subnet')
    parser.add_argument('--verification', choices=('passed', 'failed'))
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise PermissionError('Provisioning requires root')
    if args.action == 'finalize' and args.verification is None:
        parser.error('--verification is required for finalize')
    os.umask(0o077)
    with (ROOT / '.provision.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another provisioning process is running') from None
        create(args) if args.action == 'create' else finalize(args.verification)


if __name__ == '__main__':
    main()
