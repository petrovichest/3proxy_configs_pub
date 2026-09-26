import asyncio
from collections import Counter
from types import SimpleNamespace
import unittest

from architecture_workload import Histogram, Workload, proxy_url, same_ip, uniform_pool


class MeasurementTests(unittest.TestCase):
    def test_active_subset_covers_the_whole_configured_pool(self):
        self.assertEqual(uniform_pool(list(range(9000)), 3), [0, 3000, 6000])
        self.assertEqual(uniform_pool([1, 2], 1000), [1, 2])
        self.assertEqual(uniform_pool([1, 2], 0), [])

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


class WarmupTests(unittest.IsolatedAsyncioTestCase):
    async def test_generator_queue_pressure_is_attributed_to_its_measurement_phase(self):
        worker = object.__new__(Workload)
        worker.args = SimpleNamespace(rps=1000, http_pool=1)
        worker.pool = [{}]
        worker.live_requests = set(range(1024))
        worker.counters = Counter()
        worker.stopped = worker.measuring = False
        task = asyncio.create_task(worker.http_loop(None))

        async def wait_for_counter(name):
            while not worker.counters[name]:
                await asyncio.sleep(.001)

        try:
            await asyncio.wait_for(wait_for_counter('warmup_generator_backpressure'), 1)
            self.assertEqual(worker.counters['generator_backpressure'], 0)
            warmup_count = worker.counters['warmup_generator_backpressure']
            worker.measuring = True
            await asyncio.wait_for(wait_for_counter('generator_backpressure'), 1)
            self.assertEqual(worker.counters['warmup_generator_backpressure'], warmup_count)
        finally:
            worker.stopped = True
            await asyncio.wait_for(task, 1)


if __name__ == '__main__':
    unittest.main()
