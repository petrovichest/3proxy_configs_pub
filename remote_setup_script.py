#!/usr/bin/env python3
"""One-time SSH provisioning; application files reach the server through Git."""
import argparse
import getpass
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
import tempfile

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
            raise subprocess.CalledProcessError(code,argv,output=''.join(output))
        return ''.join(output)
    finally:
        channel.close()


def atomic_local(path, content):
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def download_pool(sftp, directory, local):
    """Preserve credentials and diagnostics even when creation/verification fails."""
    local.mkdir(mode=0o700, parents=True, exist_ok=True)
    local.chmod(0o700)
    try:
        with sftp.open(directory + '/deployment.json') as source:
            deployment = json.loads(source.read())
    except FileNotFoundError:
        return None
    atomic_local(local / 'deployment.json', json.dumps(deployment, indent=2) + '\n')
    combined = []
    for project in deployment.get('projects', []):
        if Path(project).name != project or project in ('.', '..'):
            raise ValueError('Invalid project in deployment record')
        destination = local / project
        destination.mkdir(mode=0o700, exist_ok=True)
        destination.chmod(0o700)
        for name in ('extracted_proxy', 'proxy_configs'):
            try:
                with sftp.open(f'{directory}/generated_proxy_configs/{project}/{name}') as source:
                    content = source.read().decode()
            except FileNotFoundError:
                continue
            atomic_local(destination / name, content)
            if name == 'extracted_proxy':
                combined.extend(content.splitlines())
    atomic_local(local / 'extracted_proxy', '\n'.join(combined) + ('\n' if combined else ''))
    return deployment


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host')
    parser.add_argument('--user',default='root')
    parser.add_argument('--key')
    parser.add_argument('--password',action='store_true',help='Prompt securely for an SSH password')
    parser.add_argument('--target-count',type=int)
    parser.add_argument('--project-prefix',default='capacity')
    parser.add_argument('--ipv6-subnet')
    parser.add_argument('--interface')
    parser.add_argument('--external-ipv4')
    parser.add_argument('--repo-url',default='https://github.com/petrovichest/3proxy_configs_pub.git')
    parser.add_argument('--directory',default='/home/3proxy_configs_pub')
    parser.add_argument('--skip-install',action='store_true')
    parser.add_argument('--skip-check',action='store_true',help='Export an unverified pool; returns exit code 2')
    parser.add_argument('--output-dir',type=Path,default=Path('downloaded_configs'))
    args=parser.parse_args()
    args.host=args.host or input('SSH host: ').strip()
    args.target_count=args.target_count if args.target_count is not None else int(input('Total proxy count: '))
    if args.target_count<=0:
        parser.error('Positive target count required')
    if Path(args.host).name != args.host or args.host in ('.', '..'):
        parser.error('Invalid host')
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
        network = []
        for option, value in (('--interface', args.interface), ('--external-ipv4', args.external_ipv4),
                              ('--ipv6-subnet', args.ipv6_subnet)):
            if value:
                network += [option, value]
        # Discovery and the existing-pool guard run before installing packages.
        run(client, ['python3', 'server_preflight.py', *network], args.directory)
        if not args.skip_install:
            run(client,['bash','install_all.sh'],args.directory)
        local=args.output_dir/args.host
        try:
            run(client, ['venv/bin/python', 'provision_server.py', 'create', '--count', str(args.target_count),
                         '--project-prefix', args.project_prefix, *network], args.directory)
        finally:
            deployment = download_pool(sftp, args.directory, local)
        if deployment is None:
            raise RuntimeError('Remote deployment record is missing')
        if args.skip_check:
            print(f'Pool exported but NOT VERIFIED: {local}. Installation is not complete.')
            return 2
        failures = 0
        for project in deployment['projects']:
            result = subprocess.run([sys.executable, str(Path(__file__).with_name('4_proxy_checker.py')),
                                     '--project-name', project, '--base-dir', str(local)])
            failures += result.returncode != 0
        try:
            run(client, ['venv/bin/python', 'provision_server.py', 'finalize',
                         '--verification', 'failed' if failures else 'passed'], args.directory)
        finally:
            download_pool(sftp, args.directory, local)
        if failures:
            raise RuntimeError(f'External verification failed; credentials and reports retained in {local}')
        print(f'Verified {args.target_count} proxies; credentials saved privately to {local}')
        sftp.close()
        return 0
    finally:
        client.close()


if __name__=='__main__':
    raise SystemExit(main())
