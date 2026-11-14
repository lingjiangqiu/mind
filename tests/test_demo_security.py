import unittest

from fastapi import HTTPException

from app.core.security import create_demo_token, hash_password, valid_demo_access_code, verify_demo_token, verify_password


class DemoSecurityTests(unittest.TestCase):
    def test_password_hash_uses_salted_argon2id(self):
        first = hash_password("correct horse battery staple")
        second = hash_password("correct horse battery staple")

        self.assertTrue(first.startswith("$argon2id$"))
        self.assertNotEqual(first, second)
        self.assertTrue(verify_password("correct horse battery staple", first))
        self.assertFalse(verify_password("wrong password", first))

    def test_signed_demo_token_round_trip(self):
        token = create_demo_token("student", "test-secret", 60)

        self.assertEqual(verify_demo_token(token, "test-secret"), "student")

    def test_tampered_demo_token_is_rejected(self):
        token = create_demo_token("student", "test-secret", 60)
        tampered = f"{token[:-1]}{'A' if token[-1] != 'A' else 'B'}"

        with self.assertRaises(HTTPException):
            verify_demo_token(tampered, "test-secret")

    def test_access_code_comparison_is_exact(self):
        self.assertTrue(valid_demo_access_code("777", "777"))
        self.assertFalse(valid_demo_access_code("778", "777"))


if __name__ == "__main__":
    unittest.main()
