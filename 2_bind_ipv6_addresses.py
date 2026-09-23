#!/usr/bin/env python3
"""Bind only missing project IPv6 addresses in one iproute2 batch."""
import argparse
import ipaddress
import json
from pathlib import Path
import subprocess
import time


def extract_ipv6_addresses(path):
    addresses = []
    seen = set()
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        fields = dict(x.split(':', 1) for x in line.split())
        address = ipaddress.IPv6Interface(fields['ipv6'])
        if address not in seen:
            addresses.append(address)
            seen.add(address)
    if not addresses:
        raise ValueError('No IPv6 addresses in project')
    return addresses


def current_addresses(interface):
    data = json.loads(subprocess.check_output(['ip','-j','-6','addr','show','dev',interface],text=True))
    return {str(ipaddress.IPv6Address(a['local'])):a for link in data for a in link['addr_info']}


def bind_addresses(addresses, interface, action):
    existing = current_addresses(interface)
    selected = [a for a in addresses if (str(a.ip) not in existing if action == 'add' else str(a.ip) in existing)]
    if selected:
        batch = ''.join(f'addr {action} {a} dev {interface}' + (' noprefixroute' if action == 'add' else '') + '\n' for a in selected)
        subprocess.run(['ip','-6','-batch','-'],input=batch,text=True,check=True)
    deadline = time.monotonic()+15
    while True:
        actual = current_addresses(interface)
        if action == 'del':
            if any(str(a.ip) in actual for a in addresses):
                raise RuntimeError('IPv6 deletion incomplete')
            break
        if any(actual.get(str(a.ip),{}).get('dadfailed') or 'dadfailed' in actual.get(str(a.ip),{}).get('flags',[]) for a in addresses):
            raise RuntimeError('IPv6 duplicate address detection failed')
        ready = all(str(a.ip) in actual and not actual[str(a.ip)].get('tentative') and 'tentative' not in actual[str(a.ip)].get('flags',[]) for a in addresses)
        if ready:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError('IPv6 binding incomplete or still tentative')
        time.sleep(.2)
    print(f'IPv6 {action}: {len(selected)} changed, {len(addresses)} verified')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('project_name')
    parser.add_argument('--interface',required=True)
    parser.add_argument('--action',choices=['add','add_all','del','del_all'],default='add')
    args=parser.parse_args()
    if not all(c.isalnum() or c in '_.:-' for c in args.interface):
        parser.error('Invalid interface')
    path=Path(__file__).resolve().parent/'generated_proxy_configs'/args.project_name/'proxy_configs'
    if Path(args.project_name).name != args.project_name:
        parser.error('Invalid project name')
    bind_addresses(extract_ipv6_addresses(path),args.interface,'add' if args.action.startswith('add') else 'del')


if __name__=='__main__':
    main()
