import unittest

import agent_bridge_server


class AgentBridgeCorsTest(unittest.TestCase):
    def test_exact_loopback_origins_are_allowed(self):
        self.assertTrue(agent_bridge_server.is_allowed_origin("http://localhost:8799"))
        self.assertTrue(agent_bridge_server.is_allowed_origin("https://127.0.0.1:443"))
        self.assertTrue(agent_bridge_server.is_allowed_origin("http://[::1]:8799"))

    def test_deceptive_or_credentialed_origins_are_rejected(self):
        self.assertFalse(agent_bridge_server.is_allowed_origin("http://localhost.evil.com"))
        self.assertFalse(agent_bridge_server.is_allowed_origin("http://127.0.0.1.evil.test"))
        self.assertFalse(agent_bridge_server.is_allowed_origin("http://user@localhost:8799"))
        self.assertFalse(agent_bridge_server.is_allowed_origin("null"))

    def test_non_http_schemes_are_rejected(self):
        self.assertFalse(agent_bridge_server.is_allowed_origin("file://localhost/tmp"))
        self.assertFalse(agent_bridge_server.is_allowed_origin("javascript://localhost"))


if __name__ == "__main__":
    unittest.main()
