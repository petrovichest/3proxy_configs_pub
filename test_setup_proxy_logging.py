import unittest

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

    def test_rejects_conflicts_without_rewriting(self):
        with self.assertRaises(ValueError):
            add_logging_to_config("log /tmp/legacy.log\n", "example_1")
        with self.assertRaises(ValueError):
            add_logging_to_config("maxconn 10000\n", "../example")
        with self.assertRaises(ValueError):
            add_logging_to_config(add_logging_to_config("maxconn 10000\n", "example_1"), "example_2")


if __name__ == "__main__":
    unittest.main()
