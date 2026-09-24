import argparse
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import migrate_server as migration
from migrate_proxy_lists import convert, line_for


def record():
    pairs = []
    for n in range(3):
        old = {'proxy_ip': '192.0.2.10', 'proxy_port': str(10000 + n), 'user': 'legacy',
               'pass': 'old-secret', 'ipv6': f'2001:db8:1:{n}::66/64'}
        new = dict(old, proxy_port='20000', user=f'shared_{10000+n}', **{'pass': f'new-secret-{n}'})
        pairs.append({'old': old, 'new': new})
    return {'kind': 'legacy_migration', 'version': 1, 'status': 'verified',
            'architecture': '3proxy-shared-v1', 'identity': 'host_port_username',
            'requested_count': 3, 'created_count': 3, 'verified_count': 3,
            'external_verification': 'passed', 'projects': ['shared'], 'mappings': pairs,
            'network': {'ipv4': '192.0.2.10', 'interface': 'ens3', 'ipv6_subnet': '2001:db8:1::/48'},
            'listen_port': 20000, 'legacy': {'root': '/legacy', 'files': {},
                                         'services': {'3proxy-old.service': {'enabled': True}}},
            'resources': {'memory_max_bytes': 1024**3}}


class ConversionTests(unittest.TestCase):
    def test_membership_order_comments_other_hosts_and_rollback(self):
        r = record()
        text = '# pool\n' + line_for(r['mappings'][2]['old']) + '\n\n' + line_for(r['mappings'][0]['old'])
        text += '\n198.51.100.5:7777@other:password\n'
        result, counts = convert(text, [r])
        self.assertEqual(counts['changed'], 2)
        self.assertIn(line_for(r['mappings'][2]['new']), result)
        self.assertNotIn(line_for(r['mappings'][1]['new']), result)
        self.assertEqual(convert(result, [r])[0], result)
        self.assertEqual(convert(result, [r])[1]['changed'], 0)
        self.assertEqual(convert(result, [r], reverse=True)[0], text)

    def test_unverified_or_unknown_credentials_fail_without_disclosure(self):
        r = record()
        text = '192.0.2.10:10000@legacy:wrong-secret\n'
        with self.assertRaisesRegex(ValueError, 'line 1') as error:
            convert(text, [r])
        self.assertNotIn('wrong-secret', str(error.exception))
        r['external_verification'] = 'failed'
        with self.assertRaisesRegex(ValueError, 'verification'):
            convert(text, [r])


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        spec = importlib.util.spec_from_file_location('migration_generator', '1_generate_proxy_configs.py')
        self.gen = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.gen)
        self.gen.BASE_OUTPUT_DIR = self.root / 'generated_proxy_configs'
        self.record_path = self.root / 'deployment.json'
        self.args = argparse.Namespace(legacy_directory=Path('/legacy'), project_prefix='shared',
                                       listen_port=20000, interface=None, external_ipv4=None, ipv6_subnet=None)
        for target, value in [('ROOT', self.root), ('RECORD', self.record_path)]:
            p = patch.object(migration, target, value); p.start(); self.addCleanup(p.stop)
        p = patch.object(migration.provision, 'generator', return_value=self.gen)
        p.start(); self.addCleanup(p.stop)

    def persist(self, r):
        self.record_path.write_text(json.dumps(r))

    def test_interrupted_prepare_reuses_credentials_and_ipv6_and_never_stops_legacy(self):
        r = record(); r['status'] = 'preparing'; r['created_count'] = 0
        self.persist(r)
        with (patch.object(migration.provision, 'require_memory'),
              patch.object(migration.provision, 'wait_ready', side_effect=RuntimeError('interrupted')),
              patch.object(migration.subprocess, 'run') as run):
            with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                migration.prepare(self.args)
            self.assertTrue(all('disable' not in call.args[0] for call in run.call_args_list))
        original = {p.name:p.read_bytes() for p in (self.gen.BASE_OUTPUT_DIR/'shared').iterdir()}
        with (patch.object(migration.provision, 'require_memory'),
              patch.object(migration.provision, 'wait_ready'), patch.object(migration.subprocess, 'run')):
            done = migration.prepare(self.args)
        self.assertEqual(done['status'], 'prepared')
        self.assertEqual(done['mappings'], r['mappings'])
        for name, data in original.items():
            self.assertEqual((self.gen.BASE_OUTPUT_DIR/'shared'/name).read_bytes(), data)
        self.assertEqual(self.record_path.stat().st_mode & 0o777, 0o600)

    def test_source_change_blocks_resume(self):
        r = record(); path = self.root/'original'; path.write_text('before')
        r['legacy']['files'][str(path)] = migration.digest(path)
        self.persist(r); path.write_text('after')
        with self.assertRaisesRegex(ValueError, 'Legacy installation changed'):
            migration.load_record()

    def test_occupied_port_fails_before_record_creation(self):
        r = record()
        old = [pair['old'] for pair in r['mappings']]
        addresses = {pair['old']['ipv6'].split('/')[0] for pair in r['mappings']}
        assigned = [{'addr_info': [{'local': address, 'scope': 'global'} for address in addresses]}]
        with (patch.object(migration, 'legacy_inventory', return_value=(r['legacy'], old, set(), addresses)),
              patch.object(migration, 'discover', return_value=r['network']),
              patch.object(migration, 'ip_json', return_value=assigned),
              patch.object(self.gen, 'network_preflight', return_value=({20000}, []))):
            with self.assertRaisesRegex(ValueError, 'occupied'):
                migration.inspect(self.args)
        self.assertFalse(self.record_path.exists())

    def test_mapping_cannot_change_ipv6(self):
        r = record(); r['mappings'][0]['new']['ipv6'] = '2001:db8:1:ffff::66/64'
        self.persist(r)
        with self.assertRaisesRegex(ValueError, 'Inconsistent'):
            migration.load_record()

    def test_connected_clients_prevent_retirement(self):
        self.persist(record())
        with (patch.object(migration.provision, 'wait_ready'), patch.object(migration, 'connections', return_value=1),
              patch.object(migration.subprocess, 'run') as run):
            with self.assertRaisesRegex(RuntimeError, 'still connected'):
                migration.finalize()
            run.assert_not_called()

    def test_failed_recheck_cannot_reuse_old_success(self):
        self.persist(record())
        args = argparse.Namespace(verification='failed', verified_count=2, report_sha256=None)
        with self.assertRaisesRegex(ValueError, 'Every logical'):
            migration.verify(args)
        with self.assertRaisesRegex(ValueError, 'verification'):
            migration.finalize()

    def test_retirement_and_rollback_never_delete_addresses(self):
        self.persist(record())
        with (patch.object(migration.provision, 'wait_ready'), patch.object(migration, 'connections', return_value=0),
              patch.object(migration.subprocess, 'run') as run):
            self.assertEqual(migration.finalize()['status'], 'complete')
            self.assertEqual(migration.rollback()['status'], 'rolled_back')
            commands = [call.args[0] for call in run.call_args_list]
            self.assertTrue(all(cmd[0] == 'systemctl' for cmd in commands))
            self.assertIn(['systemctl', 'disable', '--now', '3proxy-old.service'], commands)
            self.assertIn(['systemctl', 'start', '3proxy-old.service'], commands)
            self.assertIn(['systemctl', 'disable', '--now', '3proxy-shared.service'], commands)

    def test_rollback_restores_old_service_before_waiting_for_new_clients(self):
        self.persist(record())
        with patch.object(migration, 'connections', return_value=1), patch.object(migration.subprocess, 'run') as run:
            self.assertEqual(migration.rollback()['status'], 'rollback_pending')
            self.assertNotIn(['systemctl', 'disable', '--now', '3proxy-shared.service'],
                             [call.args[0] for call in run.call_args_list])


if __name__ == '__main__':
    unittest.main()
