import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from capacity_guard import Limits, heartbeat_error, stop_tests


def sample(seconds, busy=0, idle=0, memory=1024, rx=0, tx=0):
    return {'time': seconds, 'memory_available': memory,
            'cpu': [busy, 0, 0, idle, 0, 0, 0, 0], 'network': [rx, tx]}


class GuardTests(unittest.TestCase):
    def test_missing_stale_and_future_heartbeat_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'heartbeat'
            self.assertEqual(heartbeat_error(path, 60, 100), 'missing_heartbeat')
            path.touch()
            os.utime(path, (100, 100))
            self.assertIsNone(heartbeat_error(path, 60, 150))
            self.assertEqual(heartbeat_error(path, 60, 161), 'stale_heartbeat')
            self.assertEqual(heartbeat_error(path, 60, 90), 'stale_heartbeat')

    def test_memory_failure_does_not_need_a_previous_sample(self):
        limits = Limits(512, 75, 700, 30)
        self.assertEqual(limits.check(sample(0, memory=511), None), ['memory_reserve'])

    def test_brief_cpu_spike_recovers_but_sustained_load_stops(self):
        limits = Limits(512, 75, 700, 30)
        a, b, c = sample(0), sample(20, busy=200), sample(40, busy=200, idle=200)
        self.assertEqual(limits.check(b, a), [])
        self.assertEqual(limits.check(c, b), [])
        d, e = sample(60, busy=400, idle=200), sample(80, busy=600, idle=200)
        self.assertEqual(limits.check(d, c), [])
        self.assertEqual(limits.check(e, d), ['sustained_cpu'])

    def test_idle_and_iowait_are_not_counted_as_busy(self):
        limits = Limits(512, 75, 700, 2)
        current = sample(2, idle=10)
        current['cpu'][4] = 90
        self.assertEqual(limits.check(current, sample(0)), [])

    def test_either_network_direction_can_exhaust_budget(self):
        for direction in ('rx', 'tx'):
            limits = Limits(512, 75, 700, 30)
            current = sample(2, idle=100, **{direction: 200_000_000})
            self.assertEqual(limits.check(current, sample(0)), ['network_budget'])

    def test_unexpected_clock_or_counter_reset_stops(self):
        limits = Limits(512, 75, 700, 30)
        self.assertEqual(limits.check(sample(0), sample(1)), ['invalid_sample'])
        self.assertEqual(limits.check(sample(2), sample(1, busy=10)), ['invalid_sample'])

    def test_stop_scope_cannot_include_production_or_proxy_services(self):
        with patch('capacity_guard.subprocess.run') as run:
            for units in ([], ['pm2-root.service'], ['3proxy-botscap.service'], ['*.service']):
                with self.assertRaises(ValueError):
                    stop_tests(units)
            run.assert_not_called()
            stop_tests(['proxy-capacity-client.service'])
            self.assertEqual(run.call_args.args[0],
                             ['systemctl', 'stop', '--no-block', 'proxy-capacity-client.service'])


if __name__ == '__main__':
    unittest.main()
