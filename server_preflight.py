#!/usr/bin/env python3
"""Read-only discovery and validation before a one-time proxy installation."""
import argparse
import ipaddress
import json
from pathlib import Path
import re
import subprocess


def ip_json(*args):
    return json.loads(subprocess.check_output(['ip', '-j', *args], text=True))


def choose(values, label, option):
    values = sorted(set(values))
    if len(values) != 1:
        raise ValueError(f'Cannot determine {label} unambiguously; specify {option}')
    return values[0]


def discover(addresses, routes4, routes6, *, interface=None, ipv4=None, subnet=None):
    """Infer only locally evidenced assignments; never invent provider allocations."""
    if interface is None:
        defaults = [r for r in routes4 if r.get('dst') == 'default' and r.get('dev')]
        if ipv4:
            interface = choose([a['ifname'] for a in addresses
                                if any(v['local'] == ipv4 for v in a.get('addr_info', []))],
                               'interface for IPv4', '--interface')
        else:
            metric = min((r.get('metric', 0) for r in defaults), default=None)
            interface = choose([r['dev'] for r in defaults if r.get('metric', 0) == metric],
                               'default interface', '--interface')
    if not re.fullmatch(r'[A-Za-z0-9_.:-]+', interface):
        raise ValueError('Invalid interface name')
    links = [a for a in addresses if a['ifname'] == interface]
    if len(links) != 1:
        raise ValueError('Interface does not exist')
    assigned = links[0].get('addr_info', [])
    ipv4s = [a['local'] for a in assigned if a['family'] == 'inet' and a.get('scope') == 'global']
    ipv4 = ipv4 or choose(ipv4s, 'listening IPv4', '--external-ipv4')
    ipaddress.IPv4Address(ipv4)
    if ipv4 not in ipv4s:
        raise ValueError('Listening IPv4 is not assigned to the selected interface')
    if not any(r.get('dst') == 'default' and r.get('dev') == interface for r in routes6):
        raise ValueError('An IPv6 default route on the selected interface is required')
    candidates = set()
    for a in assigned:
        if a['family'] != 'inet6' or a.get('scope') != 'global':
            continue
        if a.get('tentative') or a.get('dadfailed') or {'tentative', 'dadfailed'} & set(a.get('flags', [])):
            continue
        if a['prefixlen'] in (48, 64):
            candidates.add(ipaddress.IPv6Network(f"{a['local']}/{a['prefixlen']}", strict=False))
    # An assigned /48 containing an assigned /64 is the stronger local evidence.
    candidates = {n for n in candidates if not any(n != p and n.subnet_of(p) for p in candidates)}
    network = ipaddress.IPv6Network(subnet, strict=True) if subnet else ipaddress.IPv6Network(
        choose([str(n) for n in candidates], 'allocated IPv6 prefix', '--ipv6-subnet'))
    if network.prefixlen not in (48, 64):
        raise ValueError('Only /48 and /64 IPv6 allocations are supported')
    if network.is_link_local or network.is_multicast or network.is_unspecified:
        raise ValueError('A globally routable IPv6 allocation is required')
    return {'interface': interface, 'ipv4': ipv4, 'ipv6_subnet': str(network),
            'subnet_source': 'explicit' if subnet else 'assigned_address',
            'ipv6_per_64': network.prefixlen == 48}


def existing_installation(root, *, unit_directory=Path('/etc/systemd/system'), processes=None):
    base = root / 'generated_proxy_configs'
    if base.exists() and any(p.name != '.allocation.lock' for p in base.iterdir()):
        raise ValueError('Existing or incomplete proxy pool found; manual recovery is required')
    if (root / 'deployment.json').exists():
        raise ValueError('An installation record already exists; automatic resume is disabled')
    units = [p for p in unit_directory.glob('3proxy-*.service') if p.name != '3proxy-logrotate.service']
    units += list(unit_directory.glob('ipv6-proxy*.service'))
    if units:
        raise ValueError('Existing proxy service found; this installer requires a new server')
    if processes is None:
        processes = []
        for path in Path('/proc').glob('[0-9]*/comm'):
            try:
                processes.append(path.read_text().strip())
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                pass
    if {'3proxy', 'gost'} & set(processes):
        raise ValueError('A proxy engine is already running; manual inspection is required')


def memory_info():
    values = {line.split(':')[0]: int(line.split()[1]) * 1024
              for line in Path('/proc/meminfo').read_text().splitlines() if len(line.split()) > 1}
    return {'total_bytes': values['MemTotal'], 'available_bytes': values['MemAvailable']}


def inspect(root, *, interface=None, ipv4=None, subnet=None):
    existing_installation(root)
    result = discover(ip_json('addr', 'show'), ip_json('route', 'show', 'default'),
                      ip_json('-6', 'route', 'show', 'default'),
                      interface=interface, ipv4=ipv4, subnet=subnet)
    result['memory'] = memory_info()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--interface')
    parser.add_argument('--external-ipv4')
    parser.add_argument('--ipv6-subnet')
    args = parser.parse_args()
    print(json.dumps(inspect(Path(__file__).resolve().parent, interface=args.interface,
                             ipv4=args.external_ipv4, subnet=args.ipv6_subnet)))


if __name__ == '__main__':
    main()
