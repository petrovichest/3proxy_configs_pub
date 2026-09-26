import argparse
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import provision_server as provision
import remote_setup_script as remote


class CreationTests(unittest.TestCase):
    def test_resource_defaults_and_explicit_budget_reserve_host_memory(self):
        with patch.object(provision, 'memory_info', return_value={'available_bytes': 64 * 1024**3}):
            default = provision.creation_resources(argparse.Namespace())
            self.assertEqual(default['memory_max_bytes'], 64 * 1024**3 - 512 * 1024**2)
            self.assertNotIn('cpu_quota_percent', default)
            args = argparse.Namespace(memory_max_mib=8192, reserve_memory_mib=32768, cpu_quota_percent=400)
            self.assertEqual(provision.creation_resources(args), {
                'memory_max_bytes': 8 * 1024**3,
                'available_memory_reserve_bytes': 32 * 1024**3,
                'cpu_quota_percent': 400})
            args.memory_max_mib = 40000
            with self.assertRaisesRegex(RuntimeError, 'reserve'):
                provision.creation_resources(args)

    def test_persisted_reserve_is_enforced_after_creation(self):
        with tempfile.TemporaryDirectory() as temp:
            record = Path(temp) / 'deployment.json'
            record.write_text(json.dumps({'resources': {'available_memory_reserve_bytes': 32 * 1024**3}}))
            with patch.object(provision, 'RECORD', record), patch.object(provision, 'memory_info', return_value={'available_bytes': 31 * 1024**3}):
                with self.assertRaisesRegex(RuntimeError, '32768 MiB'):
                    provision.require_memory()

    def test_invalid_resource_budgets_are_rejected(self):
        for option in ('memory_max_mib', 'reserve_memory_mib', 'cpu_quota_percent'):
            for value in (0, -1):
                with self.subTest(option=option, value=value), self.assertRaises(ValueError):
                    provision.creation_resources(argparse.Namespace(**{option: value}))

    def test_existing_pool_is_rejected_before_deployment_record_is_written(self):
        args = argparse.Namespace(count=3000, interface=None, external_ipv4=None, ipv6_subnet=None,
                                  project_prefix='capacity')
        with (patch.object(provision, 'generator'),
              patch.object(provision, 'inspect', side_effect=ValueError('existing pool')),
              patch.object(provision, 'save') as save):
            with self.assertRaisesRegex(ValueError, 'existing pool'):
                provision.create(args)
            save.assert_not_called()

    def test_memory_shortage_is_a_failure(self):
        with patch.object(provision, 'memory_info', return_value={'available_bytes': 511 * 1024**2}):
            with self.assertRaisesRegex(RuntimeError, 'incomplete'):
                provision.require_memory()

    def test_incomplete_count_cannot_be_finalized(self):
        with tempfile.TemporaryDirectory() as temp:
            record = Path(temp) / 'deployment.json'
            original = {'status': 'started_unverified', 'projects': ['capacity_1'],
                        'requested_count': 3000, 'created_count': 2999}
            record.write_text(json.dumps(original))
            with patch.object(provision, 'RECORD', record), patch.object(provision, 'wait_ready'), patch.object(provision, 'save') as save:
                with self.assertRaisesRegex(RuntimeError, 'incomplete'):
                    provision.finalize('passed')
                self.assertEqual(save.call_args.args[0]['status'], 'verification_failed')


class ExportTests(unittest.TestCase):
    def test_failed_installation_still_exports_credentials_and_status(self):
        deployment = {'status': 'failed', 'projects': ['capacity_1'], 'requested_count': 3, 'created_count': 2}
        files = {'/remote/deployment.json': json.dumps(deployment).encode(),
                 '/remote/generated_proxy_configs/capacity_1/extracted_proxy': b'192.0.2.1:10000@test:password\n',
                 '/remote/generated_proxy_configs/capacity_1/proxy_configs': b'mapping\n'}
        sftp = Mock()
        def open_remote(path):
            if path not in files:
                raise FileNotFoundError(path)
            return io.BytesIO(files[path])
        sftp.open.side_effect = open_remote
        with tempfile.TemporaryDirectory() as temp:
            local = Path(temp)
            result = remote.download_pool(sftp, '/remote', local)
            self.assertEqual(result['status'], 'failed')
            self.assertEqual((local / 'extracted_proxy').read_bytes(), files['/remote/generated_proxy_configs/capacity_1/extracted_proxy'])
            self.assertEqual((local / 'extracted_proxy').stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads((local / 'deployment.json').read_text())['requested_count'], 3)


if __name__ == '__main__':
    unittest.main()
