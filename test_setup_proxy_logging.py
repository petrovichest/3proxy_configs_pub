import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import setup_proxy_logging
from setup_proxy_logging import add_logging_to_config


class LoggingConfigTests(unittest.TestCase):
    def test_preserves_generated_proxy_configuration(self):
        original = (
            "maxconn 10000\n"
            "setgid 65535\n"
            "setuid 65535\n"
            "users example:CL:private-value\n"
            "proxy -64 -p10000 -i192.0.2.1 -e2001:db8::1\n"
        )
        updated = add_logging_to_config(original, "example_1")
        self.assertTrue(updated.endswith(original))
        self.assertEqual(updated, add_logging_to_config(updated, "example_1"))
        self.assertIn("log /var/log/3proxy/example_1.log", updated)
        self.assertNotIn("%U", updated)
        self.assertNotIn("%T", updated)
        self.assertIn("%e", updated)

    def test_existing_log_format_is_preserved(self):
        old = add_logging_to_config('auth strong\n', 'example_1').replace(' %e ', ' ')
        self.assertEqual(add_logging_to_config(old, 'example_1'), old)

    def test_rejects_conflicts_without_rewriting(self):
        with self.assertRaises(ValueError):
            add_logging_to_config("log /tmp/legacy.log\n", "example_1")
        with self.assertRaises(ValueError):
            add_logging_to_config("maxconn 10000\n", "../example")
        with self.assertRaises(ValueError):
            add_logging_to_config(add_logging_to_config("maxconn 10000\n", "example_1"), "example_2")

    def test_installs_log_file_and_rotation_units(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            log_dir = root / "logs"
            with (
                patch.object(setup_proxy_logging, "LOG_DIR", log_dir),
                patch.object(setup_proxy_logging, "LOGROTATE_CONFIG", root / "logrotate.conf"),
                patch.object(setup_proxy_logging, "ROTATE_SERVICE", root / "3proxy-logrotate.service"),
                patch.object(setup_proxy_logging, "ROTATE_TIMER", root / "3proxy-logrotate.timer"),
                patch.object(setup_proxy_logging.os, "geteuid", return_value=0),
                patch.object(setup_proxy_logging.os, "chown") as chown,
                patch.object(setup_proxy_logging.shutil, "which", return_value="/usr/sbin/logrotate"),
                patch.object(setup_proxy_logging.subprocess, "run") as run,
            ):
                setup_proxy_logging.install_system_logging("example_1")
                self.assertTrue((log_dir / "example_1.log").is_file())
                self.assertTrue((root / "logrotate.conf").is_file())
                self.assertTrue((root / "3proxy-logrotate.timer").is_file())
                self.assertEqual(chown.call_count, 2)
                self.assertEqual(run.call_count, 2)


if __name__ == "__main__":
    unittest.main()
