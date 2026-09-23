import asyncio
from collections import Counter
from types import SimpleNamespace
import unittest
from proxy_load_test import resource_stop,parse_proxy,percentile,Stage,ws_restriction,valid_ws_quote


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

    def test_ws_control_restrictions_exclude_quote_transaction_bytes(self):
        quote={'StreamData':{'payload':{'SwapQuotes':{'quotes':{'Titan':{
            'outAmount':123,'transaction':b'\x00quota\xff','pool':'AbcQuOtAxyz'}}}}}}
        self.assertIsNone(ws_restriction(quote))
        self.assertEqual(ws_restriction({'StreamEnd':{'reason':'Rate limited'}}),'rate_limited')
        self.assertEqual(ws_restriction({'Error':{'message':'Subscription quota exceeded'}}),'quota')
        self.assertEqual(ws_restriction({'Error':{'status':429}}),'http_status')
        quote['StreamData']['payload']['SwapQuotes']['quotes']['Titan']['error']='Too many requests'
        self.assertEqual(ws_restriction(quote),'too_many')

    def test_useful_ws_quote_requires_expected_amount_and_tokens(self):
        client=SimpleNamespace(INPUT_MINT_USDT='USDT',INPUT_MINT_SOL='SOL',_safe_str=str)
        row={'inAmount':'50000000','outAmount':'123','inputMint':'USDT','outputMint':'SOL'}
        message={'StreamData':{'payload':{'SwapQuotes':{'quotes':{'Titan':row}}}}}
        self.assertTrue(valid_ws_quote(message,client))
        for key,value in [('outAmount','0'),('inAmount','1'),('outputMint','WRONG'),('error','invalid')]:
            original=dict(row);row[key]=value
            self.assertFalse(valid_ws_quote(message,client))
            row.clear();row.update(original)
        self.assertFalse(valid_ws_quote({'StreamData':None},client))


if __name__=='__main__':unittest.main()
