import asyncio
from collections import Counter
from types import SimpleNamespace
from pathlib import Path
import tempfile
import time
import unittest
from proxy_load_test import resource_stop,parse_proxy,percentile,Stage,ws_restriction,valid_ws_quote


class LoadGuardTests(unittest.TestCase):
    def test_single_established_ws_reset_can_reconnect_but_error_burst_stops(self):
        stage=Stage(SimpleNamespace(),[{'endpoint':'192.0.2.1:10000'}],0,375)
        for index in range(3):self.assertTrue(stage.ws_failure(index,'1006',True,True))
        self.assertFalse(stage.ws_failure(3,'1006',True,True))
        self.assertEqual(stage.reason,'ws_error_rate')

    def test_ws_reconnect_never_retries_api_limits_or_repeated_failures(self):
        for code,reconnect,established in [('429',True,True),('1008',True,True),('1006',False,True),('1006',True,False)]:
            stage=Stage(SimpleNamespace(),[{'endpoint':'192.0.2.1:10000'}],0,375)
            self.assertFalse(stage.ws_failure(0,code,reconnect,established))
            self.assertTrue(stage.stop.is_set())

    def test_ws_error_window_excludes_old_failures(self):
        stage=Stage(SimpleNamespace(),[{'endpoint':'192.0.2.1:10000'}],0,375)
        stage.ws_failures.extend([time.monotonic()-61]*4)
        self.assertTrue(stage.ws_failure(0,'1006',True,True))
        self.assertEqual(len(stage.ws_failures),1)

    def test_ws_reconnect_keeps_the_same_slot_and_is_attempted_once(self):
        async def run():
            stage=Stage(SimpleNamespace(),[{'endpoint':'192.0.2.1:10000'}],0,375)
            calls=[]
            async def once(index,can_reconnect):
                calls.append((index,can_reconnect))
                return True
            async def wait(seconds):pass
            stage.websocket_once=once;stage.wait=wait
            await stage.websocket(17)
            self.assertEqual(calls,[(17,True),(17,False)])
            self.assertEqual(stage.counts['ws_reconnects'],1)
        asyncio.run(run())

    def test_interrupted_measurement_retains_elapsed_time(self):
        async def run(directory):
            stage=Stage(SimpleNamespace(output=Path(directory)/'result.jsonl',host='192.0.2.1',
                                        warmup=0,duration=300),[],0,0)
            async def monitor():
                stage.monitor_ready.set()
                await asyncio.Future()
            async def prepare():stage.counts['ws_error_TestWarmup']=1
            async def wait(seconds):
                if seconds:
                    await asyncio.sleep(.02)
                    raise asyncio.CancelledError()
            stage.monitor=monitor;stage.prepare=prepare;stage.wait=wait
            result=await stage.run()
            self.assertEqual(result['reason'],'interrupted')
            self.assertGreater(result['measurement_seconds'],0)
            self.assertEqual(result['warmup_counts']['ws_error_TestWarmup'],1)
            self.assertNotIn('ws_error_TestWarmup',result['counts'])
        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(run(directory))

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

    def test_captured_sell_subscription_validates_its_actual_amount_and_direction(self):
        client=SimpleNamespace(INPUT_MINT_USDT='USDT',INPUT_MINT_SOL='SOL',_safe_str=str)
        subscription={'input_mint':'TOKEN','output_mint':'USDT','amount':5054493975}
        row={'inputMint':'TOKEN','outputMint':'USDT','inAmount':'5054493975','outAmount':'49426802'}
        message={'StreamData':{'payload':{'SwapQuotes':{'quotes':{'Titan':row}}}}}
        self.assertTrue(valid_ws_quote(message,client,subscription))
        self.assertFalse(valid_ws_quote(message,client))
        row['inAmount']='50000000'
        self.assertFalse(valid_ws_quote(message,client,subscription))
        row.update(inAmount='5054493975',outputMint='SOL')
        self.assertFalse(valid_ws_quote(message,client,subscription))


if __name__=='__main__':unittest.main()
