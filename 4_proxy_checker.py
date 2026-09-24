#!/usr/bin/env python3
"""Verify that every proxy exits through its assigned IPv6 address."""
import argparse
import asyncio
import ipaddress
import json
from pathlib import Path
import re
import sys
from urllib.parse import quote

import aiohttp

CHECK_URL='https://api6.ipify.org'


def parse_proxy_line(line):
    match=re.fullmatch(r'(\d+(?:\.\d+){3}):(\d+)@([^:]+):(.+)',line.strip())
    if not match:
        raise ValueError('Invalid proxy record')
    ip,port,user,password=match.groups()
    ipaddress.IPv4Address(ip)
    return dict(endpoint=f'{ip}:{port}', user=user,
                url=f'http://{quote(user,safe="")}:{quote(password,safe="")}@{ip}:{port}')


def validate_address(text,expected):
    try:
        detected=ipaddress.ip_address(text.strip())
    except ValueError:
        return False,'Response is not an IP address'
    if detected.version!=6:
        return False,'Exit address is not IPv6'
    if detected != ipaddress.IPv6Address(expected):
        return False,'Exit IPv6 differs from assigned address'
    return True,str(detected)


async def check_proxy(client,proxy,semaphore,url):
    async with semaphore:
        identity = dict(endpoint=proxy['endpoint'], user=proxy['user'])
        try:
            async with client.get(url,proxy=proxy['url'],allow_redirects=False) as response:
                if response.status!=200:
                    return dict(**identity,ok=False,error=f'HTTP {response.status}')
                valid,detail=validate_address(await response.text(),proxy['expected'])
                return dict(**identity,ok=valid,**({'ipv6':detail} if valid else {'error':detail}))
        except (aiohttp.ClientError,asyncio.TimeoutError) as exc:
            return dict(**identity,ok=False,error=type(exc).__name__)


def load_proxies(directory):
    expected = {}
    for line in (directory/'proxy_configs').read_text().splitlines():
        if not line.strip():
            continue
        row = dict(x.split(':', 1) for x in line.split())
        key = (row['proxy_ip'] + ':' + row['proxy_port'], row['user'])
        if key in expected:
            raise ValueError('Duplicate logical proxy identity')
        expected[key] = str(ipaddress.IPv6Interface(row['ipv6']).ip)
    proxies = [parse_proxy_line(line) for line in (directory/'extracted_proxy').read_text().splitlines() if line.strip()]
    keys = [(p['endpoint'], p['user']) for p in proxies]
    if not proxies or len(set(keys)) != len(keys) or set(keys) != set(expected):
        raise ValueError('Missing or inconsistent proxy records')
    for proxy in proxies:
        proxy['expected'] = expected[(proxy['endpoint'], proxy['user'])]
    return proxies


async def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-name',required=True)
    parser.add_argument('--base-dir',type=Path,default=Path(__file__).resolve().parent/'generated_proxy_configs',
                        help='Project directories; may point to downloaded_configs/<host> for an external check')
    parser.add_argument('--concurrency',type=int,default=20)
    parser.add_argument('--output-file',default='proxy_check_results.txt')
    parser.add_argument('--check-url',default=CHECK_URL)
    parser.add_argument('--sample',type=int,default=0)
    parser.add_argument('--no-progress',action='store_true')
    args=parser.parse_args()
    if args.concurrency<=0 or args.sample<0:
        parser.error('Invalid concurrency/sample')
    if Path(args.project_name).name != args.project_name:
        parser.error('Invalid project name')
    directory=args.base_dir/args.project_name
    proxies=load_proxies(directory)
    if args.sample and args.sample<len(proxies):
        proxies=[proxies[i*len(proxies)//args.sample] for i in range(args.sample)]
    semaphore=asyncio.Semaphore(args.concurrency)
    # Every proxy is checked once: release its connection immediately afterwards.
    connector=aiohttp.TCPConnector(force_close=True,limit=args.concurrency)
    async with aiohttp.ClientSession(connector=connector,timeout=aiohttp.ClientTimeout(total=20),trust_env=False) as client:
        results=await asyncio.gather(*(check_proxy(client,p,semaphore,args.check_url) for p in proxies))
    output=Path(args.output_file)
    if not output.is_absolute():
        output=directory/output
    output.write_text('\n'.join(json.dumps(r) for r in results)+'\n')
    failures=sum(not r['ok'] for r in results)
    print(json.dumps(dict(checked=len(results),passed=len(results)-failures,failed=failures)))
    return 1 if failures else 0


if __name__=='__main__':
    sys.exit(asyncio.run(main()))
