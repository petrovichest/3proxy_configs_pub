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
    return dict(endpoint=f'{ip}:{port}',url=f'http://{quote(user,safe="")}:{quote(password,safe="")}@{ip}:{port}')


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
        try:
            async with client.get(url,proxy=proxy['url'],allow_redirects=False) as response:
                if response.status!=200:
                    return dict(endpoint=proxy['endpoint'],ok=False,error=f'HTTP {response.status}')
                valid,detail=validate_address(await response.text(),proxy['expected'])
                return dict(endpoint=proxy['endpoint'],ok=valid,**({'ipv6':detail} if valid else {'error':detail}))
        except (aiohttp.ClientError,asyncio.TimeoutError) as exc:
            return dict(endpoint=proxy['endpoint'],ok=False,error=type(exc).__name__)


async def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-name',required=True)
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
    directory=Path(__file__).resolve().parent/'generated_proxy_configs'/args.project_name
    expected={}
    for line in (directory/'proxy_configs').read_text().splitlines():
        if line.strip():
            r=dict(x.split(':',1) for x in line.split())
            expected[r['proxy_ip']+':'+r['proxy_port']]=str(ipaddress.IPv6Interface(r['ipv6']).ip)
    proxies=[parse_proxy_line(l) for l in (directory/'extracted_proxy').read_text().splitlines() if l.strip()]
    if not proxies or len(proxies)!=len(expected):
        raise ValueError('Missing or inconsistent proxy records')
    for p in proxies:
        p['expected']=expected[p['endpoint']]
    if args.sample and args.sample<len(proxies):
        proxies=[proxies[i*len(proxies)//args.sample] for i in range(args.sample)]
    semaphore=asyncio.Semaphore(args.concurrency)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20),trust_env=False) as client:
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
