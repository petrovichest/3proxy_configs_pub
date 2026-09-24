#!/usr/bin/env python3
"""Isolated, reversible proxy architecture experiments. Deploy this file through Git."""
import argparse
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parent
LAB = ROOT / 'capacity_results' / 'architecture'
PROXY_HOST = '213.165.33.195'
GOST_URL = 'https://github.com/go-gost/gost/releases/download/v3.3.0/gost_3.3.0_linux_amd64.tar.gz'
GOST_SHA256 = '676fb7f78d267b6ae73df719c0c7f2b565dde7147da935cfafbc1e1da558b6d5'


def command(*args):
    return subprocess.check_output([str(a) for a in args], text=True).strip()


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + '.new')
    temporary.write_text(json.dumps(data, indent=2) + '\n')
    temporary.chmod(0o600)
    temporary.replace(path)


def require_test_host():
    assigned = json.loads(command('ip', '-j', 'addr', 'show'))
    if not any(a['local'] == PROXY_HOST for link in assigned for a in link['addr_info']):
        raise RuntimeError('Proxy experiments are restricted to the designated test host')


def records():
    rows = []
    paths = sorted((ROOT / 'generated_proxy_configs').glob('capacity_*/proxy_configs'),
                   key=lambda p: int(p.parent.name.rsplit('_', 1)[1]))
    for path in paths:
        rows.extend(dict(x.split(':', 1) for x in line.split())
                    for line in path.read_text().splitlines() if line.strip())
    return rows


def file_hashes():
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((ROOT / 'generated_proxy_configs').rglob('*')) if p.is_file()}


def stop_trials():
    units = command('systemctl', 'list-units', '--all', '--no-legend', '--plain',
                    '--no-pager', 'proxy-lab-*').splitlines()
    names = [line.split()[0] for line in units if line.split()[0].startswith('proxy-lab-')]
    if names:
        subprocess.run(['systemctl', 'stop', *names], check=True)
        subprocess.run(['systemctl', 'reset-failed', *names], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)


def prepare():
    require_test_host()
    LAB.mkdir(mode=0o700, parents=True, exist_ok=True)
    snapshot_path = LAB / 'original.json'
    if snapshot_path.exists():
        raise RuntimeError('A lab snapshot already exists; restore it before preparing again')
    rows = records()
    if len(rows) != 9000:
        raise RuntimeError('Unexpected original pool size')
    sockets = command('ss', '-H', '-nt', 'state', 'established')
    if any(ip + ':' in sockets for ip in ('72.56.71.143', '5.9.117.153')):
        raise RuntimeError('A production parser host has an established connection')
    units = []
    for directory in sorted((ROOT / 'generated_proxy_configs').glob('capacity_*')):
        name = '3proxy-' + directory.name + '.service'
        units.append({'name': name, 'active': command('systemctl', 'is-active', name) == 'active',
                      'enabled': command('systemctl', 'is-enabled', name)})
    with tarfile.open(LAB / 'original-configs.tar.gz', 'w:gz') as archive:
        archive.add(ROOT / 'generated_proxy_configs', arcname='generated_proxy_configs')
    (LAB / 'original-configs.tar.gz').chmod(0o600)
    write_json(snapshot_path, {'time': time.time(), 'units': units, 'hashes': file_hashes()})
    subprocess.run(['systemctl', 'stop', *[u['name'] for u in units if u['active']]], check=True)
    print(json.dumps({'prepared': True, 'preserved_proxies': len(rows), 'stopped_units': len(units)}))


def restore():
    require_test_host()
    snapshot = json.loads((LAB / 'original.json').read_text())
    stop_trials()
    if file_hashes() != snapshot['hashes']:
        raise RuntimeError('Original files changed; refusing to overwrite them automatically')
    for unit in snapshot['units']:
        if unit['active']:
            subprocess.run(['systemctl', 'start', unit['name']], check=True)
        if command('systemctl', 'is-enabled', unit['name']) != unit['enabled']:
            raise RuntimeError('Original enablement changed')
    print(json.dumps({'restored': True, 'units': len(snapshot['units']), 'hashes_unchanged': True}))


def install_gost():
    require_test_host()
    binary = LAB / 'bin' / 'gost'
    if binary.exists():
        return binary
    binary.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=LAB) as temp:
        archive = Path(temp) / 'gost.tar.gz'
        with urllib.request.urlopen(GOST_URL, timeout=60) as source, archive.open('wb') as dest:
            shutil.copyfileobj(source, dest)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != GOST_SHA256:
            raise RuntimeError('GOST release checksum mismatch')
        with tarfile.open(archive) as tar:
            member = next(m for m in tar.getmembers() if m.name == 'gost' and m.isfile())
            with tar.extractfile(member) as source, binary.open('wb') as dest:
                shutil.copyfileobj(source, dest)
    binary.chmod(0o755)
    return binary


def render(engine, count, processes):
    require_test_host()
    if not (LAB / 'original.json').exists():
        raise RuntimeError('Prepare and preserve the original pool first')
    if not 1 <= count <= 9000 or not 1 <= processes <= count:
        raise ValueError('Invalid experiment size')
    stop_trials()
    case = LAB / f'{engine}-{count}-{processes}'
    case.mkdir(mode=0o700, parents=True, exist_ok=True)
    source = records()[:count]
    password = secrets.token_urlsafe(24)
    pool, configs = [], []
    for number in range(processes):
        part = source[number * count // processes:(number + 1) * count // processes]
        chunk = []
        for offset, old in enumerate(part, start=number * count // processes):
            chunk.append({'host': PROXY_HOST, 'port': 20000 + (number if engine == 'shared' else offset),
                          'username': f'ip{offset:05}' if engine == 'shared' else 'arch',
                          'password': password, 'ipv6': old['ipv6'].split('/')[0]})
        pool.extend(chunk)
        log = Path('/var/log/3proxy') / f'architecture-{number}.log'
        log.parent.mkdir(parents=True, exist_ok=True)
        log.touch(mode=0o600, exist_ok=True)
        os.chown(log, 65535, 65535)
        if engine in ('ports', 'shared'):
            content = (f'log {log}\nlogformat "G%Y-%m-%dT%H:%M:%S %C %p %R %E %D %I %O"\n'
                       f'maxconn 16000\n{"nscache6" if engine == "shared" else "nscache"} 65536\n'
                       'timeouts 1 5 30 60 180 1800 15 60\n'
                       'setgid 65535\nsetuid 65535\nflush\nauth strong\n')
            for user in sorted({r['username'] for r in chunk}):
                content += f'users {user}:CL:{password}\n'
            content += 'deny * * 0.0.0.0/0\ndeny * * ::ffff:0.0.0.0/96\n'
            if engine == 'shared':
                for row in chunk:
                    content += f"allow {row['username']}\nparent 1000 extip {row['ipv6']} 0\n"
                content += f'deny *\nproxy -6 -n -a -p{20000 + number} -i{PROXY_HOST}\n'
            else:
                content += 'allow arch\n'
                for row in chunk:
                    content += f"proxy -64 -n -a -p{row['port']} -i{PROXY_HOST} -e{row['ipv6']}\n"
            path = case / f'{number}.cfg'
            path.write_text(content)
            binary_args = [str(ROOT / '3proxy_binaries/3proxy'), str(path)]
        elif engine == 'gost':
            binary = install_gost()
            services = [{'name': f'ip{i}', 'addr': f"{PROXY_HOST}:{row['port']}",
                         'interface': row['ipv6'],
                         'handler': {'type': 'http', 'auth': {'username': 'arch', 'password': password}},
                         'listener': {'type': 'tcp'}} for i, row in enumerate(chunk)]
            path = case / f'{number}.json'
            path.write_text(json.dumps({'log': {'level': 'warn'}, 'services': services}))
            binary_args = [str(binary), '-C', str(path)]
        else:
            raise ValueError('Unknown engine')
        path.chmod(0o600)
        configs.append({'unit': f'proxy-lab-engine-{number}', 'command': binary_args,
                        'config_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                        'binary_sha256': hashlib.sha256(Path(binary_args[0]).read_bytes()).hexdigest(),
                        'ports': sorted({r['port'] for r in chunk})})
    write_json(case / 'pool.json', pool)
    write_json(LAB / 'current.json', {'case': str(case), 'engine': engine, 'count': count,
                                    'processes': processes, 'configs': configs,
                                    'code_revision': command('git', '-C', ROOT, 'rev-parse', 'HEAD')})
    print(json.dumps({'case': str(case), 'engine': engine, 'count': count, 'processes': processes}))


def memory_available():
    return next(int(l.split()[1]) * 1024 for l in Path('/proc/meminfo').read_text().splitlines()
                if l.startswith('MemAvailable:'))


def start():
    require_test_host()
    current = json.loads((LAB / 'current.json').read_text())
    expected = {p for c in current['configs'] for p in c['ports']}
    occupied = {int(l.split()[3].rsplit(':', 1)[1])
                for l in command('ss', '-H', '-lnt').splitlines()}
    if expected & occupied:
        raise RuntimeError('An experimental port is already occupied')
    # One shared cgroup budget, regardless of the number of experimental processes.
    subprocess.run(['systemd-run', '--quiet', '--collect', '--unit=proxy-lab-budget',
                    '--slice=proxy-lab.slice', '/usr/bin/sleep', 'infinity'], check=True)
    subprocess.run(['systemctl', 'set-property', '--runtime', 'proxy-lab.slice',
                    'MemoryMax=1500M', 'TasksMax=24000'], check=True)
    try:
        for config in current['configs']:
            if memory_available() < 256 * 1024**2:
                raise RuntimeError('Insufficient available memory')
            subprocess.run(['systemd-run', '--quiet', '--collect', '--unit=' + config['unit'],
                            '--slice=proxy-lab.slice', '--property=LimitNOFILE=131072',
                            '--property=LimitNPROC=32768', '--property=TasksMax=20000',
                            '--property=WorkingDirectory=' + current['case'],
                            *config['command']], check=True)
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            actual = {int(l.split()[3].rsplit(':', 1)[1])
                      for l in command('ss', '-H', '-lnt').splitlines()}
            if expected <= actual:
                print(json.dumps({'started': current['count'], 'listeners': len(expected)}))
                return
            if memory_available() < 256 * 1024**2:
                raise RuntimeError('Memory guard during startup')
            for config in current['configs']:
                if subprocess.run(['systemctl', 'is-active', '--quiet', config['unit']]).returncode:
                    raise RuntimeError('Experimental process exited before readiness')
            time.sleep(.5)
        raise TimeoutError('Proxy listeners did not become ready')
    except BaseException:
        stop_trials()
        raise


def monitor(duration, interval):
    spec = importlib.util.spec_from_file_location('monitor', ROOT / 'capacity_monitor.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    interface = json.loads(command('ip', '-j', 'route', 'show', 'default'))[0]['dev']
    group_name = command('systemctl', 'show', 'proxy-lab.slice', '--property=ControlGroup', '--value')
    if not group_name:
        raise RuntimeError('Experimental cgroup is missing')
    group = Path('/sys/fs/cgroup' + group_name)
    start_at = time.monotonic()
    previous = None
    while time.monotonic() - start_at <= duration:
        row = mod.snapshot(interface, process_names=('3proxy', 'gost'))
        for name in ('memory.current', 'memory.peak', 'memory.events', 'pids.current'):
            path = group / name
            if path.exists():
                row['lab_' + name] = path.read_text().strip()
        if previous:
            ticks = [b-a for a, b in zip(previous['cpu_ticks'], row['cpu_ticks'])]
            row['cpu_percent'] = (sum(ticks)-ticks[3]-ticks[4])*100/max(1, sum(ticks))
        print(json.dumps(row), flush=True)
        previous = row
        if row['memory_available'] < 256 * 1024**2:
            print(json.dumps({'guard': 'available_memory', 'time': time.time()}), flush=True)
            stop_trials()
            return
        time.sleep(interval)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'restore', 'render', 'start', 'stop', 'monitor'))
    parser.add_argument('--engine', choices=('ports', 'shared', 'gost'))
    parser.add_argument('--count', type=int, default=3000)
    parser.add_argument('--processes', type=int, default=1)
    parser.add_argument('--duration', type=float, default=600)
    parser.add_argument('--interval', type=float, default=2)
    args = parser.parse_args()
    if args.action == 'render':
        render(args.engine, args.count, args.processes)
    elif args.action == 'monitor':
        monitor(args.duration, args.interval)
    else:
        require_test_host()
        {'prepare': prepare, 'restore': restore, 'start': start, 'stop': stop_trials}[args.action]()


if __name__ == '__main__':
    main()
