"""iLink media compatibility and download policy."""

import base64

import httpx
import pytest

from qwenpaw_data.host.core.channels.wechat.bot_client import WeChatILinkClient, aes_ecb_encrypt


def test_outbound_media_keeps_its_wire_format():
    key = base64.b64encode(b"0123456789abcdef").decode()
    assert aes_ecb_encrypt(b"synthetic media bytes", key) == bytes.fromhex(
        "a622f238e33d2cefcaf92d1d3a0b08faee2ac02f2f04923eb8e5cad8ed8d4d42"
    )


@pytest.mark.asyncio
async def test_only_keyless_https_media_is_fetched():
    requests = []

    def handle(request):
        requests.append(str(request.url))
        return httpx.Response(200, content=b"image bytes")

    client = WeChatILinkClient()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client._client = http
        url = "https://cdn.example/image"
        for options in ({}, {"encrypt_query_param": "cdn-token"}):
            with pytest.raises(ValueError, match="^Encrypted WeChat media downloads are not supported$"):
                await client.download_media(url, aes_key_b64="private-test-key", **options)
        with pytest.raises(ValueError, match="require an HTTPS URL"):
            await client.download_media("http://cdn.example/image")
        assert requests == []
        assert await client.download_media(url) == b"image bytes"
        assert requests == [url]
