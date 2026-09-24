import tempfile
from pathlib import Path
import unittest

from server_preflight import discover, existing_installation


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.addresses = [{'ifname': 'net0', 'addr_info': [
            {'family': 'inet', 'local': '192.0.2.10', 'scope': 'global', 'prefixlen': 32},
            {'family': 'inet6', 'local': '2001:db8:1234::2', 'scope': 'global', 'prefixlen': 48},
        ]}]
        self.routes = [{'dst': 'default', 'dev': 'net0'}]

    def detect(self, **kw):
        return discover(self.addresses, self.routes, self.routes, **kw)

    def test_one_unambiguous_allocation_needs_no_network_arguments(self):
        result = self.detect()
        self.assertEqual(result['interface'], 'net0')
        self.assertEqual(result['ipv4'], '192.0.2.10')
        self.assertEqual(result['ipv6_subnet'], '2001:db8:1234::/48')
        self.assertTrue(result['ipv6_per_64'])

    def test_assigned_64_does_not_imply_a_provider_48(self):
        self.addresses[0]['addr_info'][1]['prefixlen'] = 64
        self.assertEqual(self.detect()['ipv6_subnet'], '2001:db8:1234::/64')
        self.assertFalse(self.detect()['ipv6_per_64'])

    def test_two_independent_prefixes_require_explicit_selection(self):
        self.addresses[0]['addr_info'].append(
            {'family': 'inet6', 'local': '2001:db8:5678::2', 'scope': 'global', 'prefixlen': 48})
        with self.assertRaisesRegex(ValueError, 'unambiguously'):
            self.detect()
        self.assertEqual(self.detect(subnet='2001:db8:1234::/48')['subnet_source'], 'explicit')

    def test_addresses_inside_an_assigned_48_do_not_create_ambiguity(self):
        self.addresses[0]['addr_info'].append(
            {'family': 'inet6', 'local': '2001:db8:1234:1::66', 'scope': 'global', 'prefixlen': 64})
        self.assertEqual(self.detect()['ipv6_subnet'], '2001:db8:1234::/48')

    def test_no_ipv6_route_is_fatal(self):
        with self.assertRaisesRegex(ValueError, 'default route'):
            discover(self.addresses, self.routes, [])

    def test_ipv4_must_belong_to_the_selected_interface(self):
        with self.assertRaisesRegex(ValueError, 'not assigned'):
            self.detect(interface='net0', ipv4='192.0.2.11')

    def test_tentative_ipv6_does_not_count_as_a_ready_allocation(self):
        self.addresses[0]['addr_info'][1]['tentative'] = True
        with self.assertRaisesRegex(ValueError, 'unambiguously'):
            self.detect()


class ExistingPoolTests(unittest.TestCase):
    def test_partial_pool_is_rejected_without_modification(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            directory = root / 'generated_proxy_configs' / '.staging-incomplete'
            directory.mkdir(parents=True)
            with self.assertRaisesRegex(ValueError, 'manual recovery'):
                existing_installation(root, unit_directory=root, processes=[])
            self.assertTrue(directory.exists())

    def test_engine_in_another_checkout_is_detected(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, 'already running'):
                existing_installation(Path(temp), unit_directory=Path(temp), processes=['gost'])

    def test_log_rotation_service_alone_is_not_a_pool(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / '3proxy-logrotate.service').touch()
            existing_installation(root, unit_directory=root, processes=[])


if __name__ == '__main__':
    unittest.main()
