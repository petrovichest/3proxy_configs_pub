#!/usr/bin/env python3
"""Controlled HTTP/WSS fixture and load driver; no production APIs or parser state."""
import argparse
import asyncio
import base64
from collections import Counter
import ipaddress
import json
import math
import os
from pathlib import Path
import secrets
import resource
import ssl
import subprocess
import time
from urllib.parse import quote

import aiohttp
from aiohttp import web

ROOT = Path(__file__).resolve().parent
LAB = ROOT / 'capacity_results' / 'architecture'
FIXTURE_IPV4 = '5.9.117.153'
FIXTURE_IPV6 = '2a01:4f8:162:62a8::2'
# Stay below the ephemeral client port range on the generator host.
FIXTURE_HTTP = 18080
FIXTURE_HTTPS = 18443


def proxy_url(row):
    return f"http://{quote(row['username'], safe='')}:{quote(row['password'], safe='')}@{row['host']}:{row['port']}"


def same_ip(actual, expected):
    try:
        return ipaddress.ip_address(actual) == ipaddress.ip_address(expected)
    except ValueError:
        return False


class Histogram:
    def __init__(self):
        self.buckets = Counter()

    def add(self, seconds):
        self.buckets[max(0, math.ceil(seconds * 1000))] += 1

    def summary(self):
        count = sum(self.buckets.values())
        if not count:
            return {'count': 0}
        result = {'count': count, 'max_ms': max(self.buckets)}
        ordered = sorted(self.buckets.items())
        for quantile in (50, 95, 99):
            target, accumulated = math.ceil(count * quantile / 100), 0
            for value, occurrences in ordered:
                accumulated += occurrences
                if accumulated >= target:
                    result[f'p{quantile}_ms'] = value
                    break
        return result


def fixture_config():
    return json.loads((LAB / 'fixture.json').read_text())


async def fixture():
    if resource.getrlimit(resource.RLIMIT_NOFILE)[0] < 32768:
        raise RuntimeError('Fixture requires LimitNOFILE >= 32768')
    LAB.mkdir(mode=0o700, parents=True, exist_ok=True)
    cert, key = LAB / 'fixture.crt', LAB / 'fixture.key'
    if not cert.exists() or not key.exists():
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                        '-keyout', str(key), '-out', str(cert), '-days', '7', '-subj',
                        '/CN=proxy-architecture-fixture', '-addext',
                        f'subjectAltName=DNS:proxy-architecture-fixture,IP:{FIXTURE_IPV6},IP:{FIXTURE_IPV4}'],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    key.chmod(0o600)
    config_path = LAB / 'fixture.json'
    if not config_path.exists():
        config_path.write_text(json.dumps({'token': secrets.token_urlsafe(32),
                                         'ipv6': FIXTURE_IPV6, 'ipv4': FIXTURE_IPV4}))
        config_path.chmod(0o600)
    config = fixture_config()
    payload = base64.b64encode(os.urandom(3072)).decode()
    allowed = [ipaddress.ip_network(n) for n in (
        '2a0b:4140:965d::/48', FIXTURE_IPV6 + '/128', '213.165.33.195/32', FIXTURE_IPV4 + '/32',
        '::1/128', '127.0.0.1/32')]

    @web.middleware
    async def authorize(request, handler):
        peer = ipaddress.ip_address(request.remote)
        if peer.version == 6 and peer.ipv4_mapped:
            peer = peer.ipv4_mapped
        if not any(peer in network for network in allowed):
            raise web.HTTPForbidden()
        if request.headers.get('X-Lab-Token') != config['token']:
            raise web.HTTPUnauthorized()
        return await handler(request)

    async def ip(request):
        return web.json_response({'exit': request.remote})

    async def quote_handler(request):
        await asyncio.sleep(.05)
        return web.json_response({'exit': request.remote, 'data': payload})

    async def websocket(request):
        socket = web.WebSocketResponse(compress=False, autoping=True, heartbeat=30)
        await socket.prepare(request)
        async def publish():
            started, sequence = time.monotonic(), 0
            while not socket.closed:
                await socket.send_json({'exit': request.remote, 'seq': sequence,
                                        'time': time.time(), 'data': payload})
                sequence += 1
                await asyncio.sleep(max(0, started + sequence * .8 - time.monotonic()))
        sender = asyncio.create_task(publish())
        try:
            async for message in socket:
                if message.type == aiohttp.WSMsgType.ERROR:
                    break
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
            await socket.close()
        return socket

    app = web.Application(middlewares=[authorize])
    app.router.add_get('/ip', ip)
    app.router.add_get('/quote', quote_handler)
    app.router.add_get('/ws', websocket)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert, key)
    for address in (FIXTURE_IPV6, FIXTURE_IPV4):
        await web.TCPSite(runner, address, FIXTURE_HTTP).start()
        await web.TCPSite(runner, address, FIXTURE_HTTPS, ssl_context=tls).start()
    print(json.dumps({'fixture_ready': True, 'ipv6': FIXTURE_IPV6}), flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


class Workload:
    def __init__(self, args):
        self.args = args
        self.pool = json.loads(args.pool.read_text())
        self.config = fixture_config()
        self.headers = {'X-Lab-Token': self.config['token']}
        self.https = f"https://[{self.config['ipv6']}]:{FIXTURE_HTTPS}"
        self.http = f"http://[{self.config['ipv6']}]:{FIXTURE_HTTP}"
        self.tls = ssl.create_default_context(cafile=str(LAB / 'fixture.crt'))
        self.counters = Counter()
        self.latency = Histogram()
        self.ws_gaps = Histogram()
        self.measuring = False
        self.stopped = False
        self.ws_open = 0
        self.ws_min = args.ws
        self.sockets = set()
        self.reconnect_generation = 0
        self.begin = 0
        self.live_requests = set()

    def summary(self):
        elapsed = max(.001, time.monotonic() - self.begin) if self.begin else 0
        return {'time': time.time(), 'elapsed': elapsed, 'counters': dict(self.counters),
                'valid_rps': self.counters['http_ok'] / elapsed if elapsed else 0,
                'ws_open': self.ws_open, 'ws_min': self.ws_min,
                'http_latency': self.latency.summary(), 'ws_gaps': self.ws_gaps.summary()}

    async def request(self, session, row):
        started = time.monotonic()
        measured = self.measuring
        if measured:
            self.counters['http_started'] += 1
        try:
            async with session.get(self.https + '/quote', proxy=proxy_url(row), headers=self.headers,
                                   timeout=aiohttp.ClientTimeout(total=15)) as response:
                if response.status != 200:
                    raise RuntimeError('HTTP status ' + str(response.status))
                data = await response.json()
                if not same_ip(data.get('exit', ''), row['ipv6']):
                    if measured:
                        self.counters['wrong_exit'] += 1
                    raise RuntimeError('Incorrect exit IPv6')
                if measured:
                    self.counters['http_ok'] += 1
                    self.latency.add(time.monotonic() - started)
        except (aiohttp.ClientError, TimeoutError, RuntimeError, ValueError) as exc:
            self.counters['http_error' if measured else 'warmup_http_error'] += 1
            self.counters['error_' + type(exc).__name__] += 1

    async def http_loop(self, session):
        if not self.args.rps:
            return
        pool = self.pool[:self.args.http_pool or len(self.pool)]
        started, index = time.monotonic(), 0
        while not self.stopped:
            await asyncio.sleep(max(0, started + index / self.args.rps - time.monotonic()))
            if self.stopped:
                break
            if len(self.live_requests) >= 1024:
                self.counters['generator_backpressure'] += 1
            else:
                task = asyncio.create_task(self.request(session, pool[index % len(pool)]))
                self.live_requests.add(task)
                task.add_done_callback(self.live_requests.discard)
            index += 1

    async def ws_worker(self, session, row, semaphore):
        previous = None
        while not self.stopped:
            generation = self.reconnect_generation
            opened = False
            try:
                async with semaphore:
                    socket = await session.ws_connect(self.https.replace('https:', 'wss:') + '/ws',
                                                      proxy=proxy_url(row), headers=self.headers,
                                                      heartbeat=30, compress=0)
                self.sockets.add(socket)
                self.ws_open += 1
                opened = True
                async for message in socket:
                    if message.type != aiohttp.WSMsgType.TEXT:
                        raise RuntimeError('Unexpected WebSocket message')
                    data = message.json()
                    if not same_ip(data.get('exit', ''), row['ipv6']):
                        self.counters['wrong_exit'] += 1
                        raise RuntimeError('Incorrect WebSocket exit IPv6')
                    now = time.monotonic()
                    if self.measuring:
                        self.counters['ws_updates'] += 1
                        if previous is not None:
                            self.ws_gaps.add(now - previous)
                    previous = now
                if not self.stopped and generation == self.reconnect_generation:
                    self.counters['ws_error'] += 1
                    return
            except asyncio.CancelledError:
                return
            except Exception as exc:
                self.counters['ws_error'] += 1
                self.counters['error_' + type(exc).__name__] += 1
                return
            finally:
                if opened:
                    self.ws_open -= 1
                    if self.measuring:
                        self.ws_min = min(self.ws_min, self.ws_open)
                    self.sockets.discard(socket)
                    await socket.close()
            if not self.stopped:
                self.counters['ws_reconnections'] += 1

    async def check(self):
        semaphore = asyncio.Semaphore(20)
        connector = aiohttp.TCPConnector(ssl=self.tls, force_close=True, limit=20)
        results = Counter()
        async with aiohttp.ClientSession(connector=connector, trust_env=False) as session:
            async def one(row):
                async with semaphore:
                    try:
                        async with session.get(self.https + '/ip', proxy=proxy_url(row),
                                               headers=self.headers, timeout=aiohttp.ClientTimeout(total=15)) as r:
                            data = await r.json()
                            results['passed' if r.status == 200 and same_ip(data.get('exit', ''), row['ipv6']) else 'failed'] += 1
                    except Exception:
                        results['failed'] += 1
            await asyncio.gather(*(one(r) for r in self.pool))
            row = dict(self.pool[0], password='incorrect-' + secrets.token_hex(8))
            try:
                async with session.get(self.http + '/ip', proxy=proxy_url(row), headers=self.headers,
                                       timeout=aiohttp.ClientTimeout(total=10)) as r:
                    results['bad_password_rejected'] = int(r.status == 407)
            except Exception:
                results['bad_password_rejected'] = 0
            for family, address in (('ipv4', self.config['ipv4']),
                                    ('mapped_ipv4', '[::ffff:' + self.config['ipv4'] + ']')):
                try:
                    async with session.get(f'http://{address}:{FIXTURE_HTTP}/ip',
                                           proxy=proxy_url(self.pool[0]), headers=self.headers,
                                           timeout=aiohttp.ClientTimeout(total=10)) as r:
                        results[family + '_rejected'] = int(r.status != 200)
                except (aiohttp.ClientError, TimeoutError):
                    results[family + '_rejected'] = 1
        # Reuse connections, alternate identities, and validate plain HTTP as well as CONNECT.
        connector = aiohttp.TCPConnector(ssl=self.tls, limit=20, keepalive_timeout=1800)
        async with aiohttp.ClientSession(connector=connector, trust_env=False) as session:
            for index in [0, len(self.pool)-1, len(self.pool)//2] * 3:
                row = self.pool[index]
                for prefix in (self.http, self.https):
                    try:
                        async with session.get(prefix + '/ip', proxy=proxy_url(row), headers=self.headers,
                                               timeout=aiohttp.ClientTimeout(total=10)) as r:
                            data = await r.json()
                            results['reuse_passed' if r.status == 200 and same_ip(data.get('exit', ''), row['ipv6']) else 'reuse_failed'] += 1
                    except Exception:
                        results['reuse_failed'] += 1
        print(json.dumps({'check': dict(results), 'count': len(self.pool)}), flush=True)
        return int(bool(results['failed'] or results['reuse_failed'] or
                        any(not results[k] for k in ('bad_password_rejected', 'ipv4_rejected', 'mapped_ipv4_rejected'))))

    async def run(self):
        if resource.getrlimit(resource.RLIMIT_NOFILE)[0] < 32768:
            raise RuntimeError('Load generator requires LimitNOFILE >= 32768')
        connector = aiohttp.TCPConnector(ssl=self.tls, limit=0, keepalive_timeout=1800)
        async with aiohttp.ClientSession(connector=connector, trust_env=False) as session:
            semaphore = asyncio.Semaphore(30)
            tasks = [asyncio.create_task(self.ws_worker(session, self.pool[i % len(self.pool)], semaphore))
                     for i in range(self.args.ws)]
            http = asyncio.create_task(self.http_loop(session))
            try:
                await asyncio.sleep(self.args.warmup)
                self.begin = time.monotonic()
                self.measuring = True
                self.ws_min = self.ws_open
                if self.ws_open != self.args.ws:
                    self.counters['initial_ws_shortfall'] = self.args.ws - self.ws_open
                deadline = self.begin + self.args.duration
                reconnected = False
                while time.monotonic() < deadline:
                    await asyncio.sleep(min(10, max(0, deadline - time.monotonic())))
                    self.ws_min = min(self.ws_min, self.ws_open)
                    print(json.dumps(self.summary()), flush=True)
                    if self.args.reconnect_at and not reconnected and time.monotonic()-self.begin >= self.args.reconnect_at:
                        reconnected = True
                        self.reconnect_generation += 1
                        await asyncio.gather(*(s.close() for s in list(self.sockets)))
                    if self.counters['wrong_exit'] or self.counters['generator_backpressure']:
                        self.counters['stopped_early'] += 1
                        break
                    errors = self.counters['http_error']
                    if errors >= 10 and errors > self.counters['http_started'] * .01:
                        self.counters['stopped_early'] += 1
                        break
                result = self.summary()
                result['final'] = True
                result['configuration'] = {'count': len(self.pool), 'http_pool': self.args.http_pool or len(self.pool),
                                           'rps': self.args.rps, 'ws': self.args.ws,
                                           'warmup': self.args.warmup, 'duration': self.args.duration}
                print(json.dumps(result), flush=True)
            finally:
                self.stopped = True
                self.measuring = False
                await http
                await asyncio.gather(*self.live_requests, return_exceptions=True)
                await asyncio.gather(*(s.close() for s in list(self.sockets)), return_exceptions=True)
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        return int(bool(self.counters['wrong_exit'] or self.counters['ws_error'] or
                        self.counters['http_error'] or self.counters['generator_backpressure']))


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('fixture', 'check', 'load'))
    parser.add_argument('--pool', type=Path)
    parser.add_argument('--rps', type=float, default=104)
    parser.add_argument('--ws', type=int, default=400)
    parser.add_argument('--http-pool', type=int, default=1000)
    parser.add_argument('--warmup', type=float, default=60)
    parser.add_argument('--duration', type=float, default=180)
    parser.add_argument('--reconnect-at', type=float, default=0)
    args = parser.parse_args()
    if args.mode == 'fixture':
        asyncio.run(fixture())
        return
    if args.pool is None:
        parser.error('--pool is required')
    worker = Workload(args)
    raise SystemExit(asyncio.run(worker.check() if args.mode == 'check' else worker.run()))


if __name__ == '__main__':
    main()
