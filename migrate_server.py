#!/usr/bin/env python3
"""Prepare a shared listener alongside a legacy installation, then retire it safely."""
import argparse
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import time

import provision_server as provision
from server_preflight import discover, ip_json

ROOT = Path(__file__).resolve().parent
RECORD = ROOT / 'deployment.json'
UNITS = Path('/etc/systemd/system')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(record):
    provision.generator().atomic_write(RECORD, json.dumps(record, indent=2) + '\n')


def properties(service):
    raw = subprocess.check_output(['systemctl', 'show', service,
        '--property=ActiveState,UnitFileState,MainPID,FragmentPath'], text=True)
    return dict(line.split('=', 1) for line in raw.splitlines() if '=' in line)


def legacy_inventory(root, *, require_running=True):
    root = root.resolve()
    if root == ROOT.resolve() or ROOT.resolve().is_relative_to(root / 'generated_proxy_configs'):
        raise ValueError('Migration must use a separate installation directory')
    module = provision.generator()
    module.BASE_OUTPUT_DIR = root / 'generated_proxy_configs'
    projects, endpoints, addresses = module.inspect_projects()
    if not projects:
        raise ValueError('No legacy proxy projects found')
    files, services, rows = {}, {}, []
    for name, records in projects.items():
        service = f'3proxy-{name}.service'
        state = properties(service)
        unit = UNITS / service
        if state.get('FragmentPath') != str(unit) or not unit.is_file():
            raise ValueError(f'Unexpected service definition: {service}')
        content = unit.read_text()
        if str(root) not in content or any(line.startswith(('ExecStop=', 'ExecStopPost='))
                                         for line in content.splitlines()):
            raise ValueError(f'Legacy service requires manual stop inspection: {service}')
        if require_running and (state.get('ActiveState') != 'active' or not int(state.get('MainPID', 0))):
            raise ValueError(f'Legacy service is not running: {service}')
        if state.get('UnitFileState') not in ('enabled', 'disabled'):
            raise ValueError(f'Unsupported enable state: {service}')
        services[service] = {'enabled': state['UnitFileState'] == 'enabled'}
        for p in (module.BASE_OUTPUT_DIR / name / 'proxy_configs',
                  module.BASE_OUTPUT_DIR / name / 'full_proxy_config', unit):
            files[str(p)] = digest(p)
        rows.extend(records)
    binary = root / '3proxy_binaries/3proxy'
    files[str(binary)] = digest(binary)
    return {'root': str(root), 'files': files, 'services': services}, rows, endpoints, addresses


def validate_source(record):
    for name, checksum in record['legacy']['files'].items():
        path = Path(name)
        if not path.is_file() or digest(path) != checksum:
            raise ValueError(f'Legacy installation changed: {name}')


def connections(endpoints):
    count = 0
    # Include all TCP states: a closing old tunnel must drain before retirement.
    for line in subprocess.check_output(['ss', '-H', '-nt'], text=True).splitlines():
        fields = line.split()
        if len(fields) < 5:
            continue
        host, _, port = fields[3].rpartition(':')
        if port.isdigit() and (host.strip('[]'), int(port)) in endpoints:
            count += 1
    return count


def load_record():
    record = json.loads(RECORD.read_text())
    if record.get('kind') != 'legacy_migration' or record.get('version') != 1:
        raise ValueError('This is not a supported migration record')
    mappings = record.get('mappings', [])
    if not mappings or len(mappings) != record['requested_count']:
        raise ValueError('Incomplete migration mapping')
    old_endpoints, usernames, addresses = set(), set(), set()
    for pair in mappings:
        old, new = pair['old'], pair['new']
        endpoint = (old['proxy_ip'], int(old['proxy_port']))
        address = str(ipaddress.IPv6Interface(old['ipv6']).ip)
        if (endpoint in old_endpoints or new['user'] in usernames or address in addresses
                or old['ipv6'] != new['ipv6'] or old['proxy_ip'] != new['proxy_ip']
                or int(new['proxy_port']) != record['listen_port']):
            raise ValueError('Inconsistent migration mapping')
        old_endpoints.add(endpoint)
        usernames.add(new['user'])
        addresses.add(address)
    validate_source(record)
    return record


def inspect(args):
    legacy, rows, endpoints, addresses = legacy_inventory(args.legacy_directory)
    network = discover(ip_json('addr', 'show'), ip_json('route', 'show', 'default'),
        ip_json('-6', 'route', 'show', 'default'), interface=args.interface,
        ipv4=args.external_ipv4, subnet=args.ipv6_subnet)
    if {r['proxy_ip'] for r in rows} != {network['ipv4']}:
        raise ValueError('Legacy listener addresses differ from the selected IPv4')
    current = {v['local'] for link in ip_json('-6', 'addr', 'show', 'dev', network['interface'])
               for v in link.get('addr_info', []) if v.get('scope') == 'global'
               and not {'tentative', 'dadfailed'}.intersection(v.get('flags', []))}
    if not addresses <= current:
        raise ValueError('Some legacy IPv6 addresses are not assigned and ready')
    occupied, _ = provision.generator().network_preflight(network['interface'], network['ipv4'])
    if args.listen_port in occupied:
        raise ValueError('Migration listen port is already occupied')
    unit = UNITS / f'3proxy-{args.project_prefix}.service'
    if unit.exists():
        raise ValueError('The migration service already exists without a migration record')
    memory = provision.require_memory()
    return legacy, rows, network, memory


def validate_artifacts(record):
    for name, checksum in record.get('artifact_hashes', {}).items():
        path = ROOT / name
        if not path.is_file() or digest(path) != checksum:
            raise ValueError(f'Migration artifact changed: {name}')


def prepare(args):
    module = provision.generator()
    if RECORD.exists():
        record = load_record()
        if (record['legacy']['root'] != str(args.legacy_directory.resolve())
                or record['projects'] != [args.project_prefix]
                or record['listen_port'] != args.listen_port):
            raise ValueError('Arguments differ from the saved migration')
        if record['status'] in ('complete', 'verified'):
            validate_artifacts(record)
            provision.wait_ready(record['projects'])
            return record
    else:
        if module.BASE_OUTPUT_DIR.exists() and any(module.BASE_OUTPUT_DIR.iterdir()):
            raise ValueError('Destination already contains a pool without a migration record')
        legacy, old_rows, network, memory = inspect(args)
        # Validate names before persisting state or using them in service/file names.
        import re
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', args.project_prefix) or args.project_prefix == 'logrotate':
            raise ValueError('Invalid migration project name')
        mappings = []
        for row in sorted(old_rows, key=lambda r: (r['proxy_ip'], int(r['proxy_port']))):
            new = dict(row, user=f'{args.project_prefix}_{row["proxy_port"]}',
                       **{'pass': secrets.token_urlsafe(24)}, proxy_port=str(args.listen_port))
            mappings.append({'old': row, 'new': new})
        record = {'version': 1, 'kind': 'legacy_migration', 'status': 'preparing',
                  'architecture': '3proxy-shared-v1', 'identity': 'host_port_username',
                  'requested_count': len(mappings), 'created_count': 0,
                  'projects': [args.project_prefix], 'listen_port': args.listen_port,
                  'network': network, 'legacy': legacy, 'mappings': mappings,
                  'resources': {'memory_max_bytes': memory['available_bytes'] - provision.RESERVE_BYTES,
                                'available_memory_reserve_bytes': provision.RESERVE_BYTES},
                  'started_at': time.time(), 'code_revision': subprocess.check_output(
                      ['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()}
        # This private journal contains the only persistent credentials; retries reuse them.
        save(record)
    module.SERVICE_LIMITS['MemoryMax'] = record['resources']['memory_max_bytes']
    project = record['projects'][0]
    final = module.BASE_OUTPUT_DIR / project
    if not final.exists():
        module.BASE_OUTPUT_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.migration-', dir=module.BASE_OUTPUT_DIR) as temp:
            staging = Path(temp)
            module.render_project(staging, project, [m['new'] for m in record['mappings']],
                                  record['network']['interface'], shared=True)
            module.atomic_write(staging / 'pool.json', json.dumps({
                'architecture': record['architecture'], 'identity': record['identity'],
                'count': record['requested_count'], 'listen_ipv4': record['network']['ipv4'],
                'listen_port': record['listen_port'], 'interface': record['network']['interface'],
                'ipv6_subnet': record['network']['ipv6_subnet'], 'preserved_legacy_ipv6': True}, indent=2) + '\n')
            record['artifact_hashes'] = {str((final / p.name).relative_to(ROOT)): digest(p)
                                         for p in staging.iterdir() if p.is_file()}
            save(record)
            os.rename(staging, final)
    validate_artifacts(record)
    if module.proxy_records(final / 'proxy_configs') != [m['new'] for m in record['mappings']]:
        raise ValueError('Migration pool differs from the saved mapping')
    provision.require_memory()
    subprocess.run(['bash', str(final / 'start_systemctl.sh')], check=True)
    provision.wait_ready(record['projects'])
    validate_source(record)
    record.update(status='prepared', created_count=record['requested_count'])
    save(record)
    return record


def verify(args):
    record = load_record()
    if record['status'] not in ('prepared', 'verified'):
        raise ValueError('Only a prepared migration can be verified')
    validate_artifacts(record)
    if args.verification != 'passed' or args.verified_count != record['requested_count']:
        record['external_verification'] = 'failed'
        record['status'] = 'prepared'
        save(record)
        raise ValueError('Every logical proxy must pass external verification')
    provision.wait_ready(record['projects'])
    record.update(status='verified', external_verification='passed', verified_at=time.time(),
                  verified_count=args.verified_count, verification_report_sha256=args.report_sha256)
    save(record)
    return record


def finalize():
    record = load_record()
    if record['status'] not in ('verified', 'finalizing', 'complete'):
        raise ValueError('External verification is required before finalization')
    if (record.get('external_verification') != 'passed'
            or record.get('verified_count') != record['requested_count']):
        raise ValueError('External verification is incomplete')
    validate_artifacts(record)
    provision.wait_ready(record['projects'])
    old_endpoints = {(m['old']['proxy_ip'], int(m['old']['proxy_port'])) for m in record['mappings']}
    if connections(old_endpoints):
        raise RuntimeError('Legacy clients are still connected; migrate and drain all consumers first')
    record['status'] = 'finalizing'
    save(record)
    for service in record['legacy']['services']:
        subprocess.run(['systemctl', 'disable', '--now', service], check=True)
    record.update(status='complete', finished_at=time.time())
    save(record)
    return record


def rollback():
    record = load_record()
    # Restore the old listeners first. Clients can then be moved back through Git.
    for service, state in record['legacy']['services'].items():
        subprocess.run(['systemctl', 'enable' if state['enabled'] else 'disable', service], check=True)
        subprocess.run(['systemctl', 'start', service], check=True)
    record['status'] = 'rollback_pending'
    save(record)
    new_endpoint = {(record['network']['ipv4'], record['listen_port'])}
    if connections(new_endpoint):
        return record
    for project in record['projects']:
        subprocess.run(['systemctl', 'disable', '--now', f'3proxy-{project}.service'], check=True)
    record.update(status='rolled_back', rolled_back_at=time.time())
    save(record)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('inspect', 'prepare', 'verify', 'finalize', 'rollback'))
    parser.add_argument('--legacy-directory', type=Path, default=Path('/home/3proxy_configs_pub'))
    parser.add_argument('--project-prefix', default='shared')
    parser.add_argument('--listen-port', type=int, default=20000)
    parser.add_argument('--interface')
    parser.add_argument('--external-ipv4')
    parser.add_argument('--ipv6-subnet')
    parser.add_argument('--verification', choices=('passed', 'failed'))
    parser.add_argument('--verified-count', type=int)
    parser.add_argument('--report-sha256')
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise PermissionError('Migration requires root')
    if not 1 <= args.listen_port <= 65535:
        parser.error('Invalid listen port')
    os.umask(0o077)
    with (ROOT / '.provision.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.action == 'inspect':
            if RECORD.exists():
                record = load_record()
            else:
                _, rows, network, _ = inspect(args)
                record = {'status': 'inspected', 'requested_count': len(rows), 'network': network}
        elif args.action == 'prepare':
            record = prepare(args)
        elif args.action == 'verify':
            record = verify(args)
        elif args.action == 'finalize':
            record = finalize()
        else:
            record = rollback()
        print(json.dumps({k: record[k] for k in ('status', 'requested_count', 'created_count', 'network') if k in record}))


if __name__ == '__main__':
    main()
