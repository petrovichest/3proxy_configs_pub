#!/usr/bin/env python3
"""Standalone real-API load tests; no parser workers, Redis writes or transactions."""
import argparse
import asyncio
from collections import Counter,deque
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import resource
import signal
import shlex
import subprocess
import sys
import time
from urllib.parse import quote


def percentile(values,p):
    if not values:return None
    rows=sorted(values)
    return round(rows[max(0,math.ceil(len(rows)*p)-1)],3)


def parse_proxy(line):
    endpoint,credentials=line.strip().split('@',1)
    user,password=credentials.split(':',1)
    return {'raw':line.strip(),'endpoint':endpoint,'url':f'http://{quote(user,safe="")}:{quote(password,safe="")}@{endpoint}'}


def resource_stop(sample):
    if sample['memory_available']<256*1024**2:return 'server_memory'
    for service in sample['services']:
        if int(service.get('pids.events',{}).get('max',0)):return 'server_task_limit'
        if int(service.get('memory.events',{}).get('oom',0)):return 'server_oom'
    return None


def process_usage(pid):
    """Include the running Node bridge, which is not counted by RUSAGE_SELF."""
    directory=Path('/proc')/str(pid)
    fields=(directory/'stat').read_text().rsplit(') ',1)[1].split()
    status=dict(line.split(':',1) for line in (directory/'status').read_text().splitlines() if ':' in line)
    return {'cpu_seconds':(int(fields[11])+int(fields[12]))/os.sysconf('SC_CLK_TCK'),
            'rss':int(status['VmRSS'].split()[0])*1024}


def ws_restriction(decoded):
    """Inspect control/error fields, never arbitrary transaction bytes or addresses."""
    controls=[value for key,value in decoded.items() if key!='StreamData']
    payload=decoded.get('StreamData',{}).get('payload',{})
    if isinstance(payload,dict):
        controls.extend(value for key,value in payload.items() if key!='SwapQuotes')
        swap=payload.get('SwapQuotes',{})
        if isinstance(swap,dict):
            controls.extend(value for key,value in swap.items() if key.lower() in ('error','errors'))
            for quote_data in swap.get('quotes',{}).values():
                if isinstance(quote_data,dict):
                    controls.extend(value for key,value in quote_data.items() if key.lower() in ('error','errors'))
    pattern=r'\b(rate[ _-]?limit(?:ed|ing)?|too[ _-]?many|quota|forbidden|unauthori[sz]ed)\b'
    while controls:
        value=controls.pop()
        if isinstance(value,dict):
            if any(str(value.get(key,'')) in ('401','403','429') for key in ('code','status','statusCode')):
                return 'http_status'
            controls.extend(value.values())
        elif isinstance(value,list):controls.extend(value)
        elif isinstance(value,str):
            match=re.search(pattern,value,re.I)
            if match:return re.sub(r'[ _-]+','_',match.group(1).lower())
    return None


def valid_ws_quote(decoded,client):
    """Validate the requested quote without formatting unused transaction routes."""
    try:
        quotes=decoded.get('StreamData',{}).get('payload',{}).get('SwapQuotes',{}).get('quotes',{})
    except (AttributeError,TypeError):return False
    if not isinstance(quotes,dict):return False
    for row in quotes.values():
        if not isinstance(row,dict) or row.get('error'):continue
        try:
            if int(row.get('inAmount',0))!=50_000_000 or int(row.get('outAmount',0))<=0:continue
        except (TypeError,ValueError):continue
        if any(row.get(key) and client._safe_str(row[key])!=expected for key,expected in
               [('inputMint',client.INPUT_MINT_USDT),('outputMint',client.INPUT_MINT_SOL)]):continue
        return True
    return False


class Stage:
    def __init__(self,args,proxies,rps,ws_count,baseline=None,ws_baseline=None):
        self.args,self.proxies,self.rps,self.ws_count,self.baseline=args,proxies,rps,ws_count,baseline
        self.ws_baseline=ws_baseline
        self.stop=asyncio.Event()
        self.monitor_ready=asyncio.Event()
        self.reason=None
        self.counts=Counter()
        self.latencies=deque(maxlen=200000)
        self.gaps=deque(maxlen=200000)
        self.recent=deque()
        self.samples=[]
        self.pending=set()
        self.workers=[]
        self.ws_active=0
        self.ws_valid=set()
        self.ws_last={}
        self.ws_failures=deque()
        self.bridge=None
        self.session=None
        self.auth_session=None
        self.llama=None
        self.process=None
        self.started=time.monotonic()
        self.measuring=False
        self.run_id=hashlib.sha256(os.urandom(32)).hexdigest()

    def halt(self,reason):
        if not self.stop.is_set():
            self.reason=reason
            self.stop.set()
            print(json.dumps({'event':'stop','reason':reason}),flush=True)

    def status(self,status):
        if status in (401,403,429):self.halt(f'api_http_{status}')

    async def monitor(self):
        from capacity_monitor import snapshot
        routes=json.loads(subprocess.check_output(['ip','-j','route','show','default'],text=True))
        local_interface=next((row['dev'] for row in routes if 'dev' in row),'lo')
        previous_local=None
        previous_usage=None
        previous_bridge=None
        remote_command=shlex.join(['python3','-u',self.args.remote_directory+'/capacity_monitor.py',
            '--interface',self.args.interface,'--interval','5'])
        self.process=await asyncio.create_subprocess_exec('ssh','-o','BatchMode=yes','-o','ConnectTimeout=10',
            'root@'+self.args.host,remote_command,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL)
        cpu_high_since=None
        backlog_since=None
        try:
            while not self.stop.is_set():
                raw=await asyncio.wait_for(self.process.stdout.readline(),20)
                if not raw:raise RuntimeError('Monitor ended')
                row=json.loads(raw)
                self.monitor_ready.set()
                self.samples.append(row)
                stage_info={'http_rps':self.rps,'ws_target':self.ws_count,'phase':'measurement' if self.measuring else 'warmup'}
                self.metrics.write(json.dumps({'kind':'server',**stage_info,**row})+'\n');self.metrics.flush()
                local=await asyncio.to_thread(snapshot,local_interface)
                usage=resource.getrusage(resource.RUSAGE_SELF)
                local['process_rss_peak']=usage.ru_maxrss*1024
                if previous_local:
                    elapsed=local['time']-previous_local['time']
                    local['process_cpu_percent']=100*(usage.ru_utime+usage.ru_stime-previous_usage)/elapsed
                    self.counts['generator_cpu_max']=max(self.counts['generator_cpu_max'],round(local['process_cpu_percent'],1))
                if self.bridge and self.bridge.process and self.bridge.process.returncode is None:
                    bridge_usage=process_usage(self.bridge.process.pid)
                    local['bridge_rss']=bridge_usage['rss']
                    if previous_bridge and previous_local:
                        local['bridge_cpu_percent']=100*(bridge_usage['cpu_seconds']-previous_bridge['cpu_seconds'])/elapsed
                        self.counts['bridge_cpu_max']=max(self.counts['bridge_cpu_max'],round(local['bridge_cpu_percent'],1))
                    previous_bridge=bridge_usage
                    local['ws_queued_messages']=sum(state['events'].qsize() for state in self.bridge._connections.values())
                    self.counts['generator_ws_queued_max']=max(self.counts['generator_ws_queued_max'],local['ws_queued_messages'])
                previous_local,previous_usage=local,usage.ru_utime+usage.ru_stime
                self.metrics.write(json.dumps({'kind':'generator',**stage_info,**local})+'\n');self.metrics.flush()
                if local['memory_available']<1024**3:self.halt('generator_memory')
                reason=resource_stop(row)
                if reason:self.halt(reason)
                now=time.monotonic()
                if self.measuring and self.ws_count and local.get('ws_queued_messages',0)>2*self.ws_count:
                    backlog_since=backlog_since or now
                    if now-backlog_since>=10:self.halt('generator_ws_backlog')
                else:backlog_since=None
                if row.get('cpu_percent',0)>90:
                    cpu_high_since=cpu_high_since or now
                    if now-cpu_high_since>=60:self.halt('server_cpu')
                else:cpu_high_since=None
                self.recent=deque((t,ok) for t,ok in self.recent if now-t<=60)
                if self.recent and now-self.started>60:
                    errors=sum(not ok for _,ok in self.recent)
                    if errors/len(self.recent)>.01:self.halt('http_error_rate')
                if self.measuring and self.baseline and len(self.latencies)>=30 and percentile(self.latencies,.95)>2*self.baseline:
                    self.halt('http_latency')
                if self.measuring and self.ws_baseline and len(self.gaps)>=30 and percentile(self.gaps,.95)>2*self.ws_baseline:
                    self.halt('ws_update_latency')
                if self.measuring and self.ws_count:
                    self.counts['ws_active_min']=min(self.counts.get('ws_active_min',self.ws_active),self.ws_active)
                    stale=sum(now-t>10 for t in self.ws_last.values())
                    if stale/max(1,self.ws_count)>.01:self.halt('ws_stale_quotes')
                loop_deadline=time.monotonic()+.05
                await asyncio.sleep(.05)
                lag=time.monotonic()-loop_deadline
                self.counts['generator_lag_max_ms']=max(self.counts['generator_lag_max_ms'],round(lag*1000))
                if lag>1:self.halt('generator_event_loop')
        except asyncio.CancelledError:raise
        except Exception as exc:self.halt('monitor_'+type(exc).__name__)

    async def prepare(self):
        from curl_cffi.requests import AsyncSession
        from curl_cffi.const import CurlMOpt
        self.session=AsyncSession(impersonate='chrome',trust_env=False,max_clients=max(256,2*len(self.proxies)),timeout=15)
        self.session.acurl.setopt(CurlMOpt.MAXCONNECTS,2*len(self.proxies))
        if self.rps:
            from dotenv import dotenv_values
            from jup_async_parser.parsers.llama_base_parser import LlamaBaseConfig,LlamaBaseParser
            from jup_async_parser.parsers.llama_robinhood_parser import TokenState
            values=dotenv_values(self.args.parser_root/'.env')
            key=values.get('DEFILLAMA_API_KEY') or os.getenv('DEFILLAMA_API_KEY')
            if not key:raise ValueError('DEFILLAMA_API_KEY is missing')
            self.config=LlamaBaseConfig(api_key=key)
            self.llama=LlamaBaseParser(self.config,None,[p['url'] for p in self.proxies])
            self.llama.decimals[self.config.quote_address]=6
            # One initialization request, never part of the benchmark hot path.
            await self.llama.refresh_gas()
            if not self.llama.gas_price:raise RuntimeError('Gas price initialization failed')
            state=TokenState(address=self.config.native_address,symbol='ETH',decimals=18)
            self.request=self.llama.request_data(state,'buy')
        if self.ws_count:
            from jup_async_parser.api_clients.titan_client import TitanAPIClient
            from jup_async_parser.config import JupParserConfig
            from jup_async_parser.parsers.titan_ws_transport import TitanNodeWebSocketTransport
            self.titan=TitanAPIClient(JupParserConfig(exchange_name='benchmark',script_name='capacity',telegram_token='',telegram_chat_id=''),{})
            self.auth_session=AsyncSession(impersonate='chrome120',trust_env=False,max_clients=32,timeout=15)
            self.bridge=TitanNodeWebSocketTransport()
            await self.bridge.start()

    async def http_request(self,proxy):
        from jup_async_parser.parsers.llama_robinhood_parser import QUOTE_URL,HEADERS,normalize_quote
        started=time.monotonic()
        ok=False
        try:
            params,body,source,destination,amount=self.request
            response=await self.session.post(QUOTE_URL,params=params,data=json.dumps(body,separators=(',',':')),
                headers=HEADERS,proxy=proxy['url'],allow_redirects=False,timeout=5,discard_cookies=True)
            self.counts['http_'+str(response.status_code)]+=1
            self.status(response.status_code)
            if response.headers.get('cf-mitigated')=='challenge':self.halt('api_challenge')
            if response.status_code==200:
                data=response.json()
                if data.get('error') and re.search(r'429|rate.?limit|too many',str(data['error']),re.I):self.halt('api_upstream_rate_limit')
                normalize_quote(data,source,destination,amount,'buy',time.time(),time.monotonic()-started,
                    chain_id=self.config.chain_id,network=self.config.network,quote_address=self.config.quote_address,quote_symbol=self.config.quote_symbol)
                ok=True
                if self.measuring:
                    self.counts['http_valid']+=1
                    self.latencies.append((time.monotonic()-started)*1000)
            self.counts['bytes_received']+=len(response.content)
        except asyncio.CancelledError:raise
        except Exception as exc:
            self.counts['http_error_'+type(exc).__name__]+=1
        finally:
            self.recent.append((time.monotonic(),ok))
            self.counts['http_completed']+=1

    async def http_loop(self):
        origin=time.monotonic();index=0
        while not self.stop.is_set():
            deadline=origin+index/self.rps
            await asyncio.sleep(max(0,deadline-time.monotonic()))
            if self.stop.is_set():break
            if len(self.pending)>=2*len(self.proxies):
                self.halt('client_concurrency');break
            if time.monotonic()-deadline>.1:
                self.counts['missed_schedule']+=1
                index=max(index,int((time.monotonic()-origin)*self.rps))
            proxy=self.proxies[index%len(self.proxies)]
            task=asyncio.create_task(self.http_request(proxy))
            self.pending.add(task);task.add_done_callback(self.pending.discard)
            index+=1

    def ws_failure(self,index,code,can_reconnect,established):
        now=time.monotonic()
        self.ws_failures.append(now)
        while self.ws_failures and now-self.ws_failures[0]>60:self.ws_failures.popleft()
        if code in ('401','403','429'):
            self.halt('api_ws_'+code)
        elif len(self.ws_failures)/max(1,self.ws_count)>.01:
            self.halt('ws_error_rate')
        retry=code=='1006' and established and can_reconnect and not self.stop.is_set()
        print(json.dumps({'event':'ws_failure','time':time.time(),'index':index,
            'endpoint':self.proxies[index%len(self.proxies)]['endpoint'],'code':code,'reconnect':retry}),flush=True)
        if not retry and not self.stop.is_set():self.halt('ws_connection_failure')
        return retry

    async def websocket(self,index):
        for attempt in range(2):
            if not await self.websocket_once(index,can_reconnect=attempt==0) or self.stop.is_set() or attempt==1:return
            self.counts['ws_reconnects']+=1
            await self.wait(1)
            if self.stop.is_set():return

    async def websocket_once(self,index,can_reconnect):
        import base58
        proxy=self.proxies[index%len(self.proxies)]
        identity_bytes=hashlib.sha256(f'{self.run_id}:{index}'.encode()).digest()
        identity=base58.b58encode(identity_bytes).decode()
        ws=None;ping=None;established=False
        try:
            response=await self.auth_session.get('https://titan.exchange/api/apollo-jwt',params={'address':identity},
                headers=self.titan.AUTH_HEADERS,proxy=proxy['url'],allow_redirects=False,timeout=15)
            self.counts['auth_'+str(response.status_code)]+=1
            self.status(response.status_code)
            if self.stop.is_set():return
            if response.status_code!=200:raise RuntimeError('Authentication response failed')
            token=response.json().get('token')
            if not token:raise RuntimeError('Missing authentication token')
            headers=dict(self.titan.WS_HEADERS)
            headers.update({'Accept-Language':'en-US,en;q=0.9','User-Agent':'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36'})
            ws=await self.bridge.connect(url=self.titan.WS_URL_BASE+'?auth='+token,proxy=proxy['raw'],headers=headers,subprotocols=['v1.api.titan.ag+gzip'])
            self.ws_active+=1
            self.counts['ws_opened']+=1
            await ws.send(self.titan.create_subscription_payload(self.titan.INPUT_MINT_SOL,amount=50_000_000,req_id=1,user_public_key_bytes=identity_bytes))
            async def heartbeat():
                while not self.stop.is_set():
                    await asyncio.sleep(15)
                    await ws.send(json.dumps({'type':'PING'}))
            ping=asyncio.create_task(heartbeat())
            first_deadline=time.monotonic()+20
            while not self.stop.is_set():
                message=await asyncio.wait_for(ws.recv(),20)
                decoded=self.titan.decode_message(message)
                if decoded:
                    restriction=ws_restriction(decoded)
                    if restriction:
                        self.counts['ws_restriction_'+restriction]+=1
                        self.halt('api_ws_restriction');break
                    if valid_ws_quote(decoded,self.titan):
                        established=True
                        now=time.monotonic()
                        if index in self.ws_last and self.measuring:
                            gap=(now-self.ws_last[index])*1000
                            self.gaps.append(gap)
                            self.counts['ws_gap_max_ms']=max(self.counts['ws_gap_max_ms'],round(gap,3))
                        self.ws_last[index]=now
                        self.ws_valid.add(index)
                        if self.measuring:self.counts['ws_valid_messages']+=1
                if index not in self.ws_valid and time.monotonic()>first_deadline:
                    self.halt('ws_no_valid_quote');break
        except asyncio.CancelledError:raise
        except Exception as exc:
            self.counts['ws_error_'+type(exc).__name__]+=1
            # The existing transport exposes only a short code, never its URL/token.
            code=re.fullmatch(r'Titan Node WebSocket (?:open failed: )?([A-Za-z0-9_]+)',str(exc))
            if code:self.counts['ws_code_'+code.group(1)]+=1
            if not self.stop.is_set():return self.ws_failure(index,code.group(1) if code else type(exc).__name__,can_reconnect,established)
        finally:
            if ping:
                ping.cancel();await asyncio.gather(ping,return_exceptions=True)
            if ws:
                self.ws_active-=1
                await ws.close()

    async def wait(self,seconds):
        try:await asyncio.wait_for(self.stop.wait(),seconds)
        except asyncio.TimeoutError:pass

    async def run(self):
        self.args.output.parent.mkdir(parents=True,exist_ok=True)
        self.metrics=(self.args.output.with_suffix('.metrics.jsonl')).open('a')
        monitor=asyncio.create_task(self.monitor())
        measured_seconds=0
        measure_start=None
        print(json.dumps({'event':'stage_start','http_rps':self.rps,'ws_connections':self.ws_count,'proxy_count':len(self.proxies)}),flush=True)
        try:
            await asyncio.wait_for(self.monitor_ready.wait(),20)
            await self.prepare()
            if self.rps:self.workers.append(asyncio.create_task(self.http_loop()))
            for index in range(self.ws_count):
                if self.stop.is_set():break
                self.workers.append(asyncio.create_task(self.websocket(index)))
                await self.wait(.1)
            await self.wait(self.args.warmup)
            if not self.stop.is_set() and self.ws_count and len(self.ws_valid)<self.ws_count:
                self.halt('ws_incomplete_useful_connections')
            if not self.stop.is_set():
                self.measuring=True
                measure_start=time.monotonic()
                self.counts.clear();self.latencies.clear();self.gaps.clear();self.samples.clear()
                print(json.dumps({'event':'measurement_start'}),flush=True)
                await self.wait(self.args.duration)
        except asyncio.CancelledError:self.halt('interrupted')
        except Exception as exc:self.halt('setup_'+type(exc).__name__)
        finally:
            if measure_start is not None:
                measured_seconds=time.monotonic()-measure_start
            self.stop.set()
            for task in self.workers+list(self.pending):task.cancel()
            await asyncio.gather(*self.workers,*list(self.pending),return_exceptions=True)
            monitor.cancel();await asyncio.gather(monitor,return_exceptions=True)
            if self.process and self.process.returncode is None:
                self.process.terminate();await self.process.wait()
            if self.bridge:await self.bridge.shutdown()
            if self.session:await self.session.close()
            if self.auth_session:await self.auth_session.close()
            if self.llama and self.llama.rpc_session:await self.llama.rpc_session.close()
            self.metrics.close()
        result={'time':time.time(),'host':self.args.host,'proxy_count':len(self.proxies),'http_rps_target':self.rps,'ws_target':self.ws_count,
            'status':'passed' if self.reason is None else 'stopped','reason':self.reason,'measurement_seconds':round(measured_seconds,2),
            'http_valid_rps':round(self.counts['http_valid']/max(1,measured_seconds),2),
            'http_latency_ms':{p:percentile(self.latencies,f) for p,f in [('p50',.5),('p95',.95),('p99',.99)]},
            'ws_gap_ms':{p:percentile(self.gaps,f) for p,f in [('p50',.5),('p95',.95),('p99',.99)]},
            'ws_valid_connections':len(self.ws_valid),'counts':dict(self.counts),
            'cpu_max':max((s.get('cpu_percent',0) for s in self.samples),default=None),
            'memory_available_min':min((s['memory_available'] for s in self.samples),default=None),
            'threads_max':max((sum(p['threads'] for p in s['services']) for s in self.samples),default=None),
            'fds_max':max((sum(p['fds'] for p in s['services']) for s in self.samples),default=None)}
        if result['status']=='passed' and self.rps and result['http_valid_rps']<.99*self.rps:
            result.update(status='stopped',reason='insufficient_valid_http_throughput')
        with self.args.output.open('a') as stream:stream.write(json.dumps(result)+'\n')
        print(json.dumps({'event':'stage_result',**result}),flush=True)
        return result


async def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host',required=True)
    parser.add_argument('--interface',default='net0')
    parser.add_argument('--remote-directory',default='/home/3proxy_configs_pub')
    parser.add_argument('--parser-root',type=Path,required=True)
    parser.add_argument('--proxy-file',type=Path,required=True)
    parser.add_argument('--profile',choices=['http','ws','mixed'],required=True)
    parser.add_argument('--rps',type=float,default=10)
    parser.add_argument('--connections',type=int,default=100)
    parser.add_argument('--warmup',type=float,default=60)
    parser.add_argument('--duration',type=float,default=300)
    parser.add_argument('--ramp',action='store_true')
    parser.add_argument('--ramp-axis',choices=['http','ws'],default='http')
    parser.add_argument('--baseline-p95-ms',type=float,help='Reuse a measured low-load HTTP p95 for a separate boundary/soak test')
    parser.add_argument('--baseline-ws-p95-ms',type=float,help='Reuse a measured low-load WebSocket update-gap p95')
    parser.add_argument('--limit-proxies',type=int,default=0)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.rps<=0 or args.connections<=0 or args.warmup<0 or args.duration<=0:parser.error('Invalid load/duration')
    if args.baseline_p95_ms is not None and args.baseline_p95_ms<=0:parser.error('Baseline p95 must be positive')
    if args.baseline_ws_p95_ms is not None and args.baseline_ws_p95_ms<=0:parser.error('WebSocket baseline p95 must be positive')
    os.umask(0o077)
    args.parser_root=args.parser_root.resolve()
    sys.path[:0]=[str(args.parser_root),str(args.parser_root.parent/'arb_parsers_models')]
    from loguru import logger
    logger.remove()  # Imported clients sometimes include credentials in their error text.
    node_modules=Path(__file__).resolve().parent/'capacity_results/node/node_modules'
    if node_modules.exists():os.environ['NODE_PATH']=str(node_modules)
    soft,hard=resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE,(min(hard,max(soft,131072)),hard))
    proxies=[parse_proxy(l) for l in args.proxy_file.read_text().splitlines() if l.strip()]
    if not proxies:parser.error('Empty proxy pool')
    if any(p['endpoint'].split(':')[0]!=args.host for p in proxies):parser.error('Proxy file must contain only the selected server')
    random.Random(42).shuffle(proxies)
    if args.limit_proxies:proxies=proxies[:args.limit_proxies]
    rps=args.rps if args.profile!='ws' else 0
    connections=args.connections if args.profile!='http' else 0
    baseline=args.baseline_p95_ms
    ws_baseline=args.baseline_ws_p95_ms
    while True:
        if rps>len(proxies):
            print(json.dumps({'event':'profile_limit','reason':'one_http_request_per_proxy_per_second'}),flush=True);break
        stage=Stage(args,proxies,rps,connections,baseline,ws_baseline)
        result=await stage.run()
        if result['status']!='passed' or not args.ramp:return 0 if result['status']=='passed' else 2
        baseline=baseline or result['http_latency_ms']['p95']
        ws_baseline=ws_baseline or result['ws_gap_ms']['p95']
        if args.profile=='ws' or (args.profile=='mixed' and args.ramp_axis=='ws'):
            connections=math.ceil(connections*1.5)
        else:rps=math.ceil(rps*1.5)
        await asyncio.sleep(5)
    return 0


if __name__=='__main__':
    sys.exit(asyncio.run(main()))
