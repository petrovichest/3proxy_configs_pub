#!/usr/bin/env python3
"""Host-local stop guard for controlled proxy load tests, independent of SSH."""
import argparse
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time

TEST_UNITS = ('proxy-capacity-client.service', 'proxy-capacity-fixture.service')


def snapshot(interface):
    memory = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
    return {'time': time.monotonic(),
            'memory_available': int(memory['MemAvailable'].split()[0]) * 1024,
            'cpu': list(map(int, Path('/proc/stat').read_text().splitlines()[0].split()[1:9])),
            'network': [int(Path(f'/sys/class/net/{interface}/statistics/{name}_bytes').read_text())
                        for name in ('rx', 'tx')]}


def heartbeat_error(path, timeout, now=None):
    try:
        age = (time.time() if now is None else now) - path.stat().st_mtime
    except OSError:
        return 'missing_heartbeat'
    return 'stale_heartbeat' if age > timeout or age < -2 else None


class Limits:
    def __init__(self, reserve_bytes, max_cpu, max_mbps, sustained_seconds):
        self.reserve_bytes = reserve_bytes
        self.max_cpu = max_cpu
        self.max_mbps = max_mbps
        self.sustained_seconds = sustained_seconds
        self.high_cpu_since = None

    def check(self, current, previous):
        faults = []
        if current['memory_available'] < self.reserve_bytes:
            faults.append('memory_reserve')
        if previous is None:
            return faults
        elapsed = current['time'] - previous['time']
        ticks = [b - a for a, b in zip(previous['cpu'], current['cpu'])]
        if elapsed <= 0 or any(t < 0 for t in ticks):
            return faults + ['invalid_sample']
        total = sum(ticks)
        cpu = 100 * (total - ticks[3] - ticks[4]) / max(1, total)
        if cpu > self.max_cpu:
            if self.high_cpu_since is None:
                self.high_cpu_since = previous['time']
            if current['time'] - self.high_cpu_since >= self.sustained_seconds:
                faults.append('sustained_cpu')
        else:
            self.high_cpu_since = None
        if any((b - a) * 8 / elapsed / 1e6 > self.max_mbps
               for a, b in zip(previous['network'], current['network'])):
            faults.append('network_budget')
        return faults


def stop_tests(units):
    # Never accept application, proxy-pool, slice or arbitrary service names.
    if not units or any(unit not in TEST_UNITS for unit in units):
        raise ValueError('Only explicit load-test units may be stopped')
    subprocess.run(['systemctl', 'stop', '--no-block', *units], check=True, timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--interface', required=True)
    parser.add_argument('--heartbeat', type=Path, required=True)
    parser.add_argument('--stop-unit', choices=TEST_UNITS, action='append', required=True)
    parser.add_argument('--reserve-memory-mib', type=float, required=True)
    parser.add_argument('--max-cpu-percent', type=float, default=75)
    parser.add_argument('--max-network-mbps', type=float, default=700)
    parser.add_argument('--sustained-seconds', type=float, default=30)
    parser.add_argument('--heartbeat-timeout', type=float, default=60)
    parser.add_argument('--interval', type=float, default=2)
    args = parser.parse_args()
    values = (args.reserve_memory_mib, args.max_cpu_percent, args.max_network_mbps,
              args.sustained_seconds, args.heartbeat_timeout, args.interval)
    if (any(not math.isfinite(v) or v <= 0 for v in values) or args.max_cpu_percent > 100
            or not re.fullmatch(r'[A-Za-z0-9_.:-]+', args.interface)):
        parser.error('Finite positive limits and a valid interface are required')
    if os.geteuid() != 0:
        raise PermissionError('Stopping test services requires root')
    limits = Limits(args.reserve_memory_mib * 1024**2, args.max_cpu_percent,
                    args.max_network_mbps, args.sustained_seconds)
    previous = None
    print(json.dumps({'guard_ready': True, 'stop_units': args.stop_unit}), flush=True)
    try:
        while True:
            current = snapshot(args.interface)
            faults = limits.check(current, previous)
            heartbeat = heartbeat_error(args.heartbeat, args.heartbeat_timeout)
            if heartbeat:
                faults.append(heartbeat)
            if faults:
                print(json.dumps({'stop': faults, 'time': time.time()}), flush=True)
                return 2
            previous = current
            time.sleep(args.interval)
    finally:
        # Also bind each test unit to the guard via BindsTo= and After= so PID 1
        # stops it if this process crashes or systemctl cannot be reached here.
        stop_tests(args.stop_unit)


if __name__ == '__main__':
    raise SystemExit(main())
