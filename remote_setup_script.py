#!/usr/bin/env python3
"""Idempotent SSH deployment; application files reach the server through Git."""
import argparse
import getpass
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

import paramiko


def connect(host, user, key=None, password=None):
    config = paramiko.SSHConfig()
    path = Path.home()/'.ssh/config'
    if path.exists():
        with path.open() as stream:
            config.parse(stream)
    settings = config.lookup(host)
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    client.connect(hostname=settings.get('hostname',host),port=int(settings.get('port',22)),
                   username=user or settings.get('user','root'),password=password,
                   key_filename=key or settings.get('identityfile'),allow_agent=True,
                   look_for_keys=True,timeout=15,banner_timeout=15,auth_timeout=20)
    client.get_transport().set_keepalive(30)
    return client


def run(client, argv, cwd=None):
    command=shlex.join([str(x) for x in argv])
    if cwd:
        command=f'cd {shlex.quote(cwd)} && '+command
    channel=client.get_transport().open_session(timeout=20)
    channel.exec_command(command)
    output=[]
    deadline=time.monotonic()+1800
    try:
        while True:
            while channel.recv_ready():
                chunk=channel.recv(65536).decode(errors='replace')
                output.append(chunk)
                print(chunk,end='',flush=True)
            while channel.recv_stderr_ready():
                chunk=channel.recv_stderr(65536).decode(errors='replace')
                print(chunk,end='',file=sys.stderr,flush=True)
            if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                break
            if time.monotonic()>deadline:
                raise TimeoutError('Remote command exceeded 30 minutes')
            time.sleep(.02)
        code=channel.recv_exit_status()
        if code:
            raise subprocess.CalledProcessError(code,argv)
        return ''.join(output)
    finally:
        channel.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host')
    parser.add_argument('--user',default='root')
    parser.add_argument('--key')
    parser.add_argument('--password',action='store_true',help='Prompt securely for an SSH password')
    parser.add_argument('--target-count',type=int)
    parser.add_argument('--project-prefix',default='capacity')
    parser.add_argument('--batch-size',type=int,default=1000)
    parser.add_argument('--ipv6-subnet')
    parser.add_argument('--interface')
    parser.add_argument('--repo-url',default='https://github.com/petrovichest/3proxy_configs_pub.git')
    parser.add_argument('--directory',default='/home/3proxy_configs_pub')
    parser.add_argument('--skip-install',action='store_true')
    parser.add_argument('--skip-check',action='store_true',help='Skip the external IPv6 check (for an explicitly separate verification step)')
    parser.add_argument('--output-dir',type=Path,default=Path('downloaded_configs'))
    args=parser.parse_args()
    args.host=args.host or input('SSH host: ').strip()
    args.target_count=args.target_count if args.target_count is not None else int(input('Total proxy count: '))
    args.ipv6_subnet=args.ipv6_subnet or input('IPv6 subnet: ').strip()
    args.interface=args.interface or input('Network interface: ').strip()
    if args.target_count<=0 or not 1<=args.batch_size<=1000:
        parser.error('Positive target count and batch size 1..1000 required')
    os.umask(0o077)
    client=connect(args.host,args.user,args.key,getpass.getpass('SSH password: ') if args.password else None)
    try:
        if not args.skip_install:
            run(client,['bash','-c','if ! command -v git >/dev/null 2>&1; then '
                        'export DEBIAN_FRONTEND=noninteractive; '
                        'apt-get update -q && apt-get install -y git ca-certificates; fi'])
        sftp=client.open_sftp()
        try:
            sftp.stat(args.directory+'/.git')
        except FileNotFoundError:
            run(client,['git','clone',args.repo_url,args.directory])
        else:
            run(client,['git','pull','--ff-only'],args.directory)
        if not args.skip_install:
            run(client,['bash','install_all.sh'],args.directory)
        output=run(client,['venv/bin/python','1_generate_proxy_configs.py',
                    '--target-count',str(args.target_count),'--project-prefix',args.project_prefix,
                    '--batch-size',str(args.batch_size),'--ipv6-subnet',args.ipv6_subnet,
                    '--interface',args.interface,'--external-ipv4',client.get_transport().getpeername()[0],'--start'],args.directory)
        summary=json.loads(output.strip().splitlines()[-1])
        local=args.output_dir/args.host
        local.mkdir(mode=0o700,parents=True,exist_ok=True)
        combined=[]
        for project in summary['projects']:
            dest=local/project
            dest.mkdir(mode=0o700,exist_ok=True)
            if not args.skip_check:
                run(client,['venv/bin/python','4_proxy_checker.py','--project-name',project],args.directory)
            for name in ('extracted_proxy','proxy_configs'):
                sftp.get(f'{args.directory}/generated_proxy_configs/{project}/{name}',str(dest/name))
                (dest/name).chmod(0o600)
            if not args.skip_check:
                sftp.get(f'{args.directory}/generated_proxy_configs/{project}/proxy_check_results.txt',str(dest/'proxy_check_results.txt'))
            combined.extend((dest/'extracted_proxy').read_text().splitlines())
        (local/'extracted_proxy').write_text('\n'.join(combined)+'\n')
        (local/'extracted_proxy').chmod(0o600)
        print(f'Deployed {len(combined)} proxies; credentials saved privately to {local}')
        sftp.close()
    finally:
        client.close()


if __name__=='__main__':
    main()
