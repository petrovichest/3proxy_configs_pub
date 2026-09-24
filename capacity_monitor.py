#!/usr/bin/env python3
"""Read-only Linux resource samples for proxy capacity tests (JSONL)."""
import argparse
import json
import os
from pathlib import Path
import time


def snapshot(interface, process_names=('3proxy',)):
    memory={line.split(':')[0]:int(line.split()[1])*1024 for line in Path('/proc/meminfo').read_text().splitlines() if len(line.split())>1}
    cpu=list(map(int,Path('/proc/stat').read_text().splitlines()[0].split()[1:9]))
    services=[]
    for directory in Path('/proc').iterdir():
        if not directory.name.isdigit():continue
        try:
            if (directory/'comm').read_text().strip() not in process_names:continue
            status=dict(l.split(':',1) for l in (directory/'status').read_text().splitlines() if ':' in l)
            group=(directory/'cgroup').read_text().strip().split('::',1)[1]
            cgroup=Path('/sys/fs/cgroup'+group)
            row={'pid':int(directory.name),'threads':int(status['Threads']),'rss':int(status['VmRSS'].split()[0])*1024,
                 'fds':len(list((directory/'fd').iterdir())),'group':group}
            for name in ('pids.current','pids.max','pids.events','memory.current','memory.events'):
                p=cgroup/name
                if p.exists():
                    value=p.read_text().strip()
                    row[name]=dict(l.split() for l in value.splitlines()) if '.events' in name else value
            services.append(row)
        except (OSError,KeyError,IndexError,ValueError):continue
    counters={}
    for name in ('snmp','netstat'):
        lines=Path('/proc/net/'+name).read_text().splitlines()
        for header,values in zip(lines[::2],lines[1::2]):
            for key,value in zip(header.split()[1:],values.split()[1:]):
                if key in ('RetransSegs','OutSegs','ListenOverflows','ListenDrops','TCPTimeouts','TCPSynRetrans'):
                    counters[key]=int(value)
    network={}
    for name in ('rx_bytes','tx_bytes','rx_dropped','tx_dropped','rx_errors','tx_errors'):
        network[name]=int(Path(f'/sys/class/net/{interface}/statistics/{name}').read_text())
    return {'time':time.time(),'cpu_ticks':cpu,'memory_available':memory['MemAvailable'],'memory_total':memory['MemTotal'],
            'swap_used':memory['SwapTotal']-memory['SwapFree'],'services':services,'tcp':counters,'network':network,
            'kernel_threads_max':int(Path('/proc/sys/kernel/threads-max').read_text())}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--interface',required=True)
    parser.add_argument('--interval',type=float,default=5)
    parser.add_argument('--once',action='store_true')
    args=parser.parse_args()
    if args.interval<=0:parser.error('Positive interval required')
    previous=None
    while True:
        row=snapshot(args.interface)
        if previous:
            ticks=[b-a for a,b in zip(previous['cpu_ticks'],row['cpu_ticks'])]
            total=max(1,sum(ticks))
            row['cpu_percent']=(total-ticks[3]-ticks[4])*100/total
            row['steal_percent']=ticks[7]*100/total
            elapsed=row['time']-previous['time']
            row['rx_mbps']=(row['network']['rx_bytes']-previous['network']['rx_bytes'])*8/elapsed/1e6
            row['tx_mbps']=(row['network']['tx_bytes']-previous['network']['tx_bytes'])*8/elapsed/1e6
        print(json.dumps(row),flush=True)
        previous=row
        if args.once:break
        time.sleep(args.interval)


if __name__=='__main__':main()
