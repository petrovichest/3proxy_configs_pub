#!/usr/bin/env python3
"""Validate the HTTP client used by Llama against the isolated fixture."""
import argparse
import asyncio
from collections import Counter
import json
from pathlib import Path

from curl_cffi import CurlMOpt
from curl_cffi.requests import AsyncSession

from architecture_workload import LAB, FIXTURE_HTTPS, fixture_config, proxy_url, same_ip


async def check(path):
    pool = json.loads(path.read_text())
    fixture = fixture_config()
    selected = [pool[i * (len(pool) - 1) // 19] for i in range(20)]
    results = Counter()
    async with AsyncSession(impersonate='chrome', trust_env=False, max_clients=30,
                            verify=str(LAB / 'fixture.crt')) as session:
        session.acurl.setopt(CurlMOpt.MAXCONNECTS, 2 * len(selected))
        async def one(row, scheme, port):
            try:
                response = await session.get(f"{scheme}://[{fixture['ipv6']}]:{port}/ip",
                    proxy=proxy_url(row), headers={'X-Lab-Token': fixture['token']}, timeout=15)
                valid = response.status_code == 200 and same_ip(response.json().get('exit', ''), row['ipv6'])
                results['passed' if valid else 'failed'] += 1
                results[scheme + ('_passed' if valid else '_failed')] += 1
            except Exception as exc:
                results['failed'] += 1
                results[scheme + '_' + type(exc).__name__ + '_' + str(getattr(exc, 'code', 'unknown'))] += 1
        # A shared session alternates identities, then repeats concurrently over cached connections.
        for _ in range(3):
            for row in selected:
                await one(row, 'https', FIXTURE_HTTPS)
            # Llama uses HTTPS quotes. Plain HTTP is covered by the functional suite;
            # curl_cffi 0.16.3 fails plain HTTP IPv6 literals with CURLE_OUT_OF_MEMORY
            # even against the existing 3proxy layout, before a request reaches it.
            await asyncio.gather(*(one(row, 'https', FIXTURE_HTTPS) for row in reversed(selected)))
    print(json.dumps({'curl_cffi': dict(results), 'logical_proxies': len(pool)}))
    return int(bool(results['failed']))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pool', required=True, type=Path)
    raise SystemExit(asyncio.run(check(parser.parse_args().pool)))
