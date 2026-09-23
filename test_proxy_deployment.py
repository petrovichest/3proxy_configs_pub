import importlib.util
import ipaddress
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch,Mock
import subprocess
import contextlib
import io
import remote_setup_script


def load(name,path):
    spec=importlib.util.spec_from_file_location(name,Path(__file__).parent/path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gen=load('generator','1_generate_proxy_configs.py')
bind=load('binder','2_bind_ipv6_addresses.py')
checker=load('checker','4_proxy_checker.py')


class GenerationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name)/'generated'
        self.patches=[patch.object(gen,'BASE_OUTPUT_DIR',self.base),patch.object(gen,'STATE_FILE',self.base/'proxy_states.json')]
        for p in self.patches:
            p.start();self.addCleanup(p.stop)

    def generate(self,n,**kw):
        return gen.generate_proxy_configs(n,'test','2001:db8:1234::/48','net0','192.0.2.1',target=True,batch_size=3,**kw)

    def test_growth_preserves_every_existing_byte(self):
        self.generate(5)
        snapshot={p.relative_to(self.base):p.read_bytes() for p in self.base.glob('test_*/*')}
        self.generate(9)
        for p,data in snapshot.items():
            self.assertEqual((self.base/p).read_bytes(),data)
        projects,ports,ips=gen.inspect_projects()
        self.assertEqual(sum(map(len,projects.values())),9)
        self.assertEqual(len(ports),9)
        self.assertEqual(len(ips),9)
        self.assertTrue(all(ipaddress.IPv6Address(ip) in ipaddress.IPv6Network('2001:db8:1234::/48') for ip in ips))

    def test_same_target_is_noop_and_retains_credentials(self):
        names=self.generate(5)
        state=gen.STATE_FILE.read_bytes()
        self.assertEqual(self.generate(5),names)
        self.assertEqual(gen.STATE_FILE.read_bytes(),state)

    def test_corrupt_state_is_not_reset(self):
        self.base.mkdir()
        gen.STATE_FILE.write_text('{broken')
        with self.assertRaises(ValueError):self.generate(5)
        self.assertEqual(gen.STATE_FILE.read_text(),'{broken')
        self.assertFalse(list(self.base.glob('test_*')))

    def test_reserved_addresses_and_listening_ports_are_skipped(self):
        self.generate(3,reserved_ports={10000,10002},reserved_addresses={'2001:db8:1234::66'})
        _,ports,ips=gen.inspect_projects()
        self.assertFalse({10000,10002}&{p for _,p in ports})
        self.assertNotIn('2001:db8:1234::66',ips)

    def test_short_port_range_fails_before_publishing(self):
        with patch.object(gen,'DEFAULT_END_PORT',10001):
            with self.assertRaises(ValueError):self.generate(3)
        self.assertFalse(gen.STATE_FILE.exists())
        self.assertFalse(list(self.base.glob('test_*')))

    def test_decrease_and_project_replacement_rejected(self):
        self.generate(5)
        with self.assertRaises(ValueError):self.generate(4)
        with self.assertRaises(ValueError):
            gen.generate_proxy_configs(9,'test_1','2001:db8:1234::/48','net0','192.0.2.1')

    def test_64_avoids_primary_and_preserves_subnet(self):
        gen.generate_proxy_configs(3,'local','2001:db8:1234:5::/64','net0','192.0.2.1')
        _,_,ips=gen.inspect_projects()
        self.assertNotIn('2001:db8:1234:5::2',ips)
        self.assertEqual(len(ips),3)

    def test_bad_input_does_not_create_output(self):
        for count in (0,-1):
            with self.assertRaises(ValueError):self.generate(count)
        with self.assertRaises(ValueError):
            gen.generate_proxy_configs(3,'../bad','2001:db8:1234::/48','net0','192.0.2.1')
        self.assertFalse(self.base.exists())

    def test_configuration_mismatch_is_fatal(self):
        self.generate(3)
        (self.base/'test_1/full_proxy_config').write_text('')
        with self.assertRaises(ValueError):self.generate(5)

    def test_generated_shell_scripts_parse(self):
        self.generate(3)
        for path in self.base.glob('test_1/*.sh'):
            subprocess.run(['bash','-n',str(path)],check=True)

    def test_allocator_tuning_preserves_mappings_and_is_repeatable(self):
        self.generate(3)
        project=self.base/'test_1'
        original=(project/'full_proxy_config').read_bytes()
        unit=project/'service.unit'
        self.assertTrue(gen.configure_allocator(unit,2))
        self.assertFalse(gen.configure_allocator(unit,2))
        self.assertTrue(gen.configure_allocator(unit,4))
        self.assertEqual(unit.read_text().count('MALLOC_ARENA_MAX='),1)
        self.assertIn('Environment=MALLOC_ARENA_MAX=4',unit.read_text())
        self.assertEqual((project/'full_proxy_config').read_bytes(),original)


class BindingTests(unittest.TestCase):
    def test_repeated_bind_is_noop(self):
        with patch.object(bind,'current_addresses',return_value={'2001:db8::3':{}}),patch.object(bind.subprocess,'run') as run:
            bind.bind_addresses([ipaddress.IPv6Interface('2001:db8::3/64')],'net0','add')
            run.assert_not_called()

    def test_only_missing_addresses_are_batched(self):
        old={'2001:db8::3':{}}
        final={**old,'2001:db8::4':{}}
        with patch.object(bind,'current_addresses',side_effect=[old,final]),patch.object(bind.subprocess,'run') as run:
            bind.bind_addresses([ipaddress.IPv6Interface(x+'/64') for x in ('2001:db8::3','2001:db8::4')],'net0','add')
            self.assertEqual(run.call_count,1)
            self.assertNotIn('::3',run.call_args.kwargs['input'])
            self.assertIn('::4',run.call_args.kwargs['input'])

    def test_failed_batch_fails_start(self):
        with patch.object(bind,'current_addresses',return_value={}),patch.object(bind.subprocess,'run',side_effect=subprocess.CalledProcessError(1,['ip'])):
            with self.assertRaises(subprocess.CalledProcessError):
                bind.bind_addresses([ipaddress.IPv6Interface('2001:db8::3/64')],'net0','add')

    def test_dad_failure_fails_start(self):
        with patch.object(bind,'current_addresses',return_value={'2001:db8::3':{'dadfailed':True}}):
            with self.assertRaises(RuntimeError):
                bind.bind_addresses([ipaddress.IPv6Interface('2001:db8::3/64')],'net0','add')


class CheckerTests(unittest.TestCase):
    def test_html_ipv4_and_wrong_ipv6_are_failures(self):
        for response in ('<html>OK</html>','192.0.2.1','2001:db8::4'):
            self.assertFalse(checker.validate_address(response,'2001:db8::3')[0])
        self.assertTrue(checker.validate_address('2001:0db8:0:0::3\n','2001:db8::3')[0])


class RemoteCommandTests(unittest.TestCase):
    def command(self,code):
        channel=Mock()
        channel.recv_ready.return_value=False
        channel.recv_stderr_ready.side_effect=[True,False,False]
        channel.recv_stderr.return_value=b'warning\n'
        channel.exit_status_ready.return_value=True
        channel.recv_exit_status.return_value=code
        client=Mock()
        client.get_transport.return_value.open_session.return_value=channel
        with contextlib.redirect_stderr(io.StringIO()):
            return remote_setup_script.run(client,['test-command'])

    def test_warning_on_stderr_is_not_failure(self):
        self.assertEqual(self.command(0),'')

    def test_nonzero_exit_is_failure(self):
        with self.assertRaises(subprocess.CalledProcessError):self.command(7)


if __name__=='__main__':unittest.main()
