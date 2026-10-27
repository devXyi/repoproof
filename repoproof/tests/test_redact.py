from __future__ import annotations

import pytest

from repoproof.redact import REDACTED, Redactor


@pytest.mark.parametrize(
    "secret",
    [
        "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8",
        "github_pat_" + "11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz",
        "AKIAIOSFODNN7EXAMPLE",
        "xoxb-123456789012-abcdefghijkl",
        "sk_live_" + "abcdefghijklmnop1234",
        "AIza" + "SyA-1234567890abcdefghijklmnopqrstu",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk",
    ],
)
def test_known_token_formats(secret):
    out = Redactor().redact(f"Using {secret} for auth")
    assert secret not in out and REDACTED in out


def test_private_key_blocks():
    key = "-----BEGIN RSA PRIVATE KEY-----\nMIIBOgIBAAJBAK\nline2\n-----END RSA PRIVATE KEY-----"
    out = Redactor().redact(f"before\n{key}\nafter")
    assert "MIIBOg" not in out and out.startswith("before") and out.endswith("after")


def test_credentials_in_urls_and_assignments():
    redactor = Redactor()
    assert "hunter2" not in redactor.redact("git clone https://user:hunter2@example.com/repo.git")
    assert "user:" in redactor.redact("https://user:hunter2@example.com")
    out = redactor.redact("export API_TOKEN=abcd1234efgh PASSWORD='s3cret!' other=value")
    assert "abcd1234efgh" not in out and "s3cret!" not in out and "other=value" in out
    assert "Bearer " in redactor.redact("Authorization: Bearer abcdefghijklmnop1234567890")
    assert "abcdefghijklmnop" not in redactor.redact("Authorization: Bearer abcdefghijklmnop1234567890")


def test_environment_values_are_redacted_wherever_they_appear():
    env = {"DEPLOY_TOKEN": "tok-value-12345", "HOME": "/home/me", "SHORT_SECRET": "abc", "PATH": "/bin"}
    redactor = Redactor.from_environ(env)
    assert redactor.redact("the value is tok-value-12345!") == f"the value is {REDACTED}!"
    assert redactor.redact("home is /home/me") == "home is /home/me"
    assert redactor.redact("abc stays") == "abc stays"  # too short to redact safely


def test_pass_env_names_are_always_treated_as_secret():
    redactor = Redactor.from_environ({"CUSTOM_VALUE": "very-private-value"}, ["CUSTOM_VALUE"])
    assert "very-private-value" not in redactor.redact("x very-private-value y")


def test_ordinary_text_is_untouched():
    text = "Collecting requests\n  Downloading requests-2.32.0-py3-none-any.whl (64 kB)\nok"
    assert Redactor().redact(text) == text
    assert Redactor().redact("") == ""
