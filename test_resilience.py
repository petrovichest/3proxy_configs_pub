import importlib.util
import ipaddress
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import install_resilience

spec = importlib.util.spec_from_file_location('binder', Path(__file__).with_name('2_bind_ipv6_addresses.py'))
binder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(binder)


class RecoveryTests(unittest.TestCase):
    def test_missing_address_repair_preserves_existing_pool_and_uses_no_restart(self):
        addresses = [ipaddress.IPv6Interface('2001:db8::1/64'), ipaddress.IPv6Interface('2001:db8::2/64')]
        before = {'2001:db8::1': {'local': '2001:db8::1'}}
        after = dict(before, **{'2001:db8::2': {'local': '2001:db8::2'}})
        with patch.object(binder, 'current_addresses', side_effect=[before, after]), patch.object(binder.subprocess, 'run') as run:
            binder.bind_addresses(addresses, 'eth0', 'add', quiet=True)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.kwargs['input'], 'addr add 2001:db8::2/64 dev eth0 noprefixroute\n')

    def test_healthy_pool_needs_one_read_and_no_mutation(self):
        address = ipaddress.IPv6Interface('2001:db8::1/64')
        with patch.object(binder, 'current_addresses', return_value={'2001:db8::1': {}}) as read, patch.object(binder.subprocess, 'run') as run:
            binder.bind_addresses([address], 'eth0', 'add', quiet=True)
        self.assertEqual(read.call_count, 1)
        run.assert_not_called()

    def test_network_file_discovery_does_not_guess_netplan_name(self):
        with patch.object(install_resilience.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, ' Network File: /run/systemd/network/10-netplan-net0.network\n')):
            self.assertEqual(install_resilience.network_file('net0'), '10-netplan-net0.network')
        with patch.object(install_resilience.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, ' Network File: n/a\n')):
            with self.assertRaises(RuntimeError):
                install_resilience.network_file('net0')


if __name__ == '__main__':
    unittest.main()
