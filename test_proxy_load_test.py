import asyncio
from collections import Counter
from types import SimpleNamespace
import unittest
from proxy_load_test import resource_stop,parse_proxy,percentile,Stage


class LoadGuardTests(unittest.TestCase):
    def test_resource_guards(self):
        healthy={'memory_available':1024**3,'services':[]}
        self.assertIsNone(resource_stop(healthy))
        self.assertEqual(resource_stop({**healthy,'memory_available':1}),'server_memory')
        self.assertEqual(resource_stop({**healthy,'services':[{'pids.events':{'max':'1'}}]}),'server_task_limit')
        self.assertEqual(resource_stop({**healthy,'services':[{'memory.events':{'oom':'1'}}]}),'server_oom')

    def test_api_restriction_stops_without_retry(self):
        for code in (401,403,429):
            stage=Stage(SimpleNamespace(),[],10,0)
            stage.status(code)
            self.assertTrue(stage.stop.is_set())
            self.assertEqual(stage.reason,f'api_http_{code}')

    def test_percentiles_and_proxy_escaping(self):
        self.assertIsNone(percentile([],.95))
        self.assertEqual(percentile([1,2,3,4,5],.95),5)
        proxy=parse_proxy('192.0.2.1:10000@user:a/b')
        self.assertEqual(proxy['endpoint'],'192.0.2.1:10000')
        self.assertIn('a%2Fb',proxy['url'])


if __name__=='__main__':unittest.main()
