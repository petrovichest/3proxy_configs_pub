import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import expand_server as expansion


class ExpansionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.module = expansion.provision.generator()
        self.module.BASE_OUTPUT_DIR = self.root / 'generated_proxy_configs'
        self.directory = self.module.BASE_OUTPUT_DIR / 'shared'
        self.directory.mkdir(parents=True)
        self.rows = [dict(user=f'shared_{10000+n}', **{'pass': f'original-secret-{n}'},
                          proxy_ip='192.0.2.10', proxy_port='20000', ipv6=f'2001:db8:1:{n+1}::66/64') for n in range(2)]
        self.module.render_project(self.directory, 'shared', self.rows, 'eth0', shared=True)
        self.pool = dict(architecture='3proxy-shared-v1', identity='host_port_username', count=2,
                         listen_ipv4='192.0.2.10', listen_port=20000, interface='eth0', ipv6_subnet='2001:db8:1::/48')
        self.module.atomic_write(self.directory / 'pool.json', json.dumps(self.pool))
        self.units = self.root / 'units'
        self.units.mkdir()
        (self.units / '3proxy-shared.service').write_bytes((self.directory / 'service.unit').read_bytes())
        (self.root / '3proxy_binaries').mkdir()
        (self.root / '3proxy_binaries/3proxy').write_text('unchanged binary')
        self.deployment = {'status': 'complete', 'projects': ['shared'], 'artifact_hashes': {
            str(p.relative_to(self.root)): expansion.digest(p) for p in self.directory.iterdir()}}
        (self.root / 'deployment.json').write_text(json.dumps(self.deployment))
        self.before = {n: (self.directory / n).read_bytes() for n in expansion.FILES}
        patches = [patch.object(expansion, 'ROOT', self.root), patch.object(expansion, 'RECORD', self.root/'expansion.json'),
                   patch.object(expansion, 'UNITS', self.units), patch.object(expansion.provision, 'generator', return_value=self.module),
                   patch.object(expansion.provision, 'require_memory', return_value={'available_bytes': 2*1024**3}),
                   patch.object(expansion.provision, 'wait_ready'),
                   patch.object(self.module, 'network_preflight', return_value=({20000}, [r['ipv6'].split('/')[0] for r in self.rows])),
                   patch.object(expansion.subprocess, 'check_output', return_value='a'*40+'\n')]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def prepare(self, target=5):
        return expansion.prepare(target)

    def applied(self):
        self.prepare()
        with patch.object(expansion, 'bind'), patch.object(expansion, 'restart'):
            return expansion.apply()

    def test_prepare_preserves_live_files_credentials_and_limits(self):
        record = self.prepare()
        self.assertEqual(self.before, {n: (self.directory/n).read_bytes() for n in expansion.FILES})
        state = self.root / record['state_directory']
        rows, pool, _ = expansion.read_pool(state/'after')
        self.assertEqual(rows[:2], self.rows)
        self.assertEqual(pool['count'], 5)
        self.assertEqual(len({r['ipv6'] for r in rows}), 5)
        self.assertEqual(len({r['user'] for r in rows}), 5)
        self.assertIn('deny * * ::ffff:0.0.0.0/96', (state/'after/full_proxy_config').read_text())
        self.assertEqual((state/'after/full_proxy_config').read_text().split('auth strong')[0], self.before['full_proxy_config'].decode().split('auth strong')[0])
        self.assertEqual((state/'after/proxy_configs').stat().st_mode & 0o777, 0o600)
        self.assertEqual(expansion.prepare(5), record)
        with self.assertRaisesRegex(ValueError, 'pending'):
            expansion.prepare(6)

    def test_allocate_skips_occupied_prefixes_and_preserves_old_mapping(self):
        rows = expansion.allocate(self.rows, self.pool, 5, ['2001:db8:1:3::99'])
        self.assertEqual(rows[:2], self.rows)
        self.assertFalse(any(r['ipv6'].startswith('2001:db8:1:3:') for r in rows[2:]))
        with self.assertRaisesRegex(ValueError, 'shrinks'):
            expansion.allocate(self.rows, self.pool, 1, [])
        with self.assertRaisesRegex(ValueError, 'target'):
            expansion.allocate(self.rows, self.pool, 65537, [])

    def test_allocate_supports_64_and_existing_expansion_users(self):
        pool = dict(self.pool, ipv6_subnet='2001:db8:1:1::/64')
        original = [dict(self.rows[0], user='expanded_00001')]
        rows = expansion.allocate(original, pool, 3, ['2001:db8:1:1::3'])
        self.assertEqual(rows[1]['user'], 'expanded_00002')
        self.assertEqual(rows[1]['ipv6'], '2001:db8:1:1::4/64')

    def test_apply_retries_without_restart_or_credential_regeneration(self):
        self.prepare()
        with patch.object(expansion, 'bind') as bind, patch.object(expansion, 'restart') as restart:
            expansion.apply()
            hashes = {n: expansion.digest(self.directory/n) for n in expansion.FILES}
            expansion.apply()
            bind.assert_called_once()
            restart.assert_called_once()
        self.assertEqual(hashes, {n: expansion.digest(self.directory/n) for n in expansion.FILES})
        self.assertEqual(expansion.read_pool(self.directory)[0][:2], self.rows)

    def test_partial_publish_can_resume(self):
        record = self.prepare()
        state = self.root/record['state_directory']
        (self.directory/'full_proxy_config').write_bytes((state/'after/full_proxy_config').read_bytes())
        record['status'] = 'applying'
        expansion.save(record)
        with patch.object(expansion, 'bind'), patch.object(expansion, 'restart'):
            expansion.apply()
        self.assertEqual(len(expansion.read_pool(self.directory)[0]), 5)

    def test_restart_failure_restores_every_live_file(self):
        self.prepare()
        with patch.object(expansion, 'bind'), patch.object(expansion, 'restart', side_effect=[RuntimeError('failed'), None]):
            with self.assertRaisesRegex(RuntimeError, 'failed'):
                expansion.apply()
        self.assertEqual(expansion.load()['status'], 'rolled_back')
        self.assertEqual(self.before, {n: (self.directory/n).read_bytes() for n in expansion.FILES})
        with patch.object(expansion, 'bind'), patch.object(expansion, 'restart'):
            expansion.apply()
        self.assertEqual(expansion.load()['status'], 'started_unverified')

    def test_drift_blocks_mutations(self):
        self.prepare()
        (self.directory/'full_proxy_config').write_text('unexpected change')
        with patch.object(expansion, 'bind') as bind:
            with self.assertRaisesRegex(ValueError, 'outside'):
                expansion.apply()
            bind.assert_not_called()

    def test_verification_requires_complete_report_and_locks_rollback(self):
        self.applied()
        with self.assertRaisesRegex(ValueError, 'Every'):
            expansion.verify(True, 4, 'a'*64)
        self.assertEqual(expansion.load()['status'], 'verification_failed')
        result = expansion.verify(True, 5, 'a'*64)
        self.assertEqual(result['status'], 'complete')
        with self.assertRaisesRegex(ValueError, 'may be in use'):
            expansion.rollback()
        with self.assertRaises(ValueError):
            expansion.verify(False, 0, None)
        self.assertEqual(expansion.load()['status'], 'complete')
        self.assertEqual(expansion.prepare(5)['status'], 'complete')
        self.assertEqual((self.root/'deployment.json').read_text(), json.dumps(self.deployment))

    def test_second_expansion_preserves_first_and_archives_journal(self):
        self.applied()
        expansion.verify(True, 5, 'a'*64)
        previous_rows = expansion.read_pool(self.directory)[0]
        with patch.object(self.module, 'network_preflight', return_value=({20000}, [r['ipv6'].split('/')[0] for r in previous_rows])):
            record = expansion.prepare(7)
        rows = expansion.read_pool(self.root/record['state_directory']/'after')[0]
        self.assertEqual(rows[:5], previous_rows)
        self.assertTrue((self.root/record['state_directory']/'previous_expansion.json').is_file())


if __name__ == '__main__':
    unittest.main()
