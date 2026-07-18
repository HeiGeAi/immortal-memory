import unittest

import web_capture


class WebCaptureSecurityTest(unittest.TestCase):
    def test_url_userinfo_is_removed(self):
        sanitized, domain, query_redacted = web_capture.sanitize_url(
            "https://alice:secret@example.com:8443/private?token=value"
        )

        self.assertEqual(sanitized, "https://example.com:8443/private")
        self.assertEqual(domain, "example.com")
        self.assertTrue(query_redacted)
        self.assertNotIn("alice", sanitized)
        self.assertNotIn("secret", sanitized)

    def test_ipv6_host_and_port_are_preserved_without_userinfo(self):
        sanitized, domain, _ = web_capture.sanitize_url(
            "http://user:pass@[::1]:8080/status"
        )

        self.assertEqual(sanitized, "http://[::1]:8080/status")
        self.assertEqual(domain, "::1")


if __name__ == "__main__":
    unittest.main()
