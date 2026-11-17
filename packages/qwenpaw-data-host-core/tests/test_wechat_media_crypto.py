"""The iLink media format must interoperate without exposing keys in errors."""

import base64

import pytest

from qwenpaw_data.host.core.channels.wechat.bot_client import aes_ecb_decrypt, aes_ecb_encrypt


def test_wechat_media_roundtrip_and_invalid_key_redaction():
    key = base64.b64encode(b"0123456789abcdef").decode()
    plaintext = b"synthetic media bytes"
    assert aes_ecb_decrypt(aes_ecb_encrypt(plaintext, key), key) == plaintext
    invalid_key = "sensitive-invalid-key"
    with pytest.raises(ValueError) as error:
        aes_ecb_decrypt(b"", invalid_key)
    assert "Invalid AES key length" in str(error.value)
    assert invalid_key not in str(error.value)
