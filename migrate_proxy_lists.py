#!/usr/bin/env python3
"""Convert explicitly selected consumer lists using a private migration journal."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re

from remote_setup_script import atomic_local


def line_for(row):
    return f"{row['proxy_ip']}:{row['proxy_port']}@{row['user']}:{row['pass']}"


def convert(text, deployments, *, reverse=False):
    mapping, target_hosts = {}, set()
    for record in deployments:
        if record.get('kind') != 'legacy_migration' or record.get('version') != 1:
            raise ValueError('Expected a migration journal')
        if not reverse and record.get('external_verification') != 'passed':
            raise ValueError('A pool must pass verification before consumer conversion')
        for pair in record['mappings']:
            source, target = (pair['new'], pair['old']) if reverse else (pair['old'], pair['new'])
            before, after = line_for(source), line_for(target)
            if before in mapping and mapping[before] != after:
                raise ValueError('Conflicting migration mappings')
            mapping[before] = after
            target_hosts.add(source['proxy_ip'])
    already = set(mapping.values())
    output, changed, matched = [], 0, 0
    for number, line in enumerate(text.splitlines(keepends=True), 1):
        value = line.strip()
        if not value or value.startswith('#'):
            output.append(line)
            continue
        host = re.match(r'^(\d+(?:\.\d+){3}):', value)
        if host and host[1] in target_hosts:
            matched += 1
            if value in mapping:
                output.append(mapping[value] + ('\n' if line.endswith('\n') else ''))
                changed += 1
            elif value in already:
                output.append(line)
            else:
                raise ValueError(f'Unknown proxy for a migrating host at line {number}')
        else:
            output.append(line)
    return ''.join(output), {'changed': changed, 'matched': matched, 'rows': len(text.splitlines())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--deployment', type=Path, action='append', required=True)
    parser.add_argument('--file', type=Path, action='append', required=True)
    parser.add_argument('--apply', action='store_true', help='Default is validation only, with no credentials printed')
    parser.add_argument('--reverse', action='store_true')
    args = parser.parse_args()
    records = [json.loads(p.read_text()) for p in args.deployment]
    prepared = []
    for path in args.file:
        original = path.read_bytes()
        converted, report = convert(original.decode(), records, reverse=args.reverse)
        prepared.append((path, original, converted, report))
    # Validate every file before changing any file; reject concurrent edits.
    for path, original, _, _ in prepared:
        if path.read_bytes() != original:
            raise RuntimeError(f'Consumer file changed concurrently: {path}')
    for path, original, converted, report in prepared:
        if args.apply and report['changed']:
            atomic_local(path, converted)
        print(json.dumps({'file': str(path), 'applied': args.apply, **report,
                          'before_sha256': hashlib.sha256(original).hexdigest(),
                          'after_sha256': hashlib.sha256(converted.encode()).hexdigest()}))


if __name__ == '__main__':
    main()
