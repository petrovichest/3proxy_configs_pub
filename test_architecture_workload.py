import unittest

from architecture_workload import Histogram, proxy_url, same_ip


class MeasurementTests(unittest.TestCase):
    def test_histogram_covers_entire_run_including_early_slow_events(self):
        histogram = Histogram()
        for _ in range(100):
            histogram.add(1)
        for _ in range(900):
            histogram.add(.01)
        self.assertEqual(histogram.summary(), {
            'count': 1000, 'max_ms': 1000, 'p50_ms': 10, 'p95_ms': 1000, 'p99_ms': 1000})

    def test_addresses_are_compared_as_addresses(self):
        self.assertTrue(same_ip('2001:db8:0:0::66', '2001:db8::66'))
        self.assertFalse(same_ip('192.0.2.1', '2001:db8::66'))
        self.assertFalse(same_ip('<html>error</html>', '2001:db8::66'))

    def test_credentials_are_url_encoded(self):
        self.assertEqual(proxy_url({'host': '192.0.2.1', 'port': 10000,
                                    'username': 'a@b', 'password': 'x:y/z'}),
                         'http://a%40b:x%3Ay%2Fz@192.0.2.1:10000')


if __name__ == '__main__':
    unittest.main()
