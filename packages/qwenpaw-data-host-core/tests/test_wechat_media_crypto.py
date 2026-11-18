"""WeChat artifact notices and media download policy."""

import io
import json
from unittest.mock import patch

import httpx
import pytest

from qwenpaw_data.host.core.channels.schema import NativePayload
from qwenpaw_data.host.core.channels.wechat.bot_client import WeChatILinkClient
from qwenpaw_data.host.core.channels.wechat.channel import WeChatChannel


@pytest.mark.asyncio
async def test_artifacts_send_only_console_notices_to_the_original_chat(tmp_path):
    requests = []

    def handle(request):
        requests.append((request.method, str(request.url), json.loads(request.content)))
        return httpx.Response(200, json={"ret": 0})

    channel = WeChatChannel()
    client = WeChatILinkClient(base_url="https://ilink.example")
    channel._client = client
    native = NativePayload(
        channel_id="wechat", sender_id="recipient", meta={"wechat_context_token": "conversation"}
    )
    names = ["chart.png", "分析报告.html"]
    paths = [str(tmp_path / name) for name in names]
    # Delivery must neither open an existing artifact nor require it to exist locally.
    (tmp_path / names[0]).write_bytes(b"private image bytes")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client._client = http
        with patch("builtins.open", wraps=open) as opened, patch("io.open", wraps=io.open) as io_opened:
            await channel._deliver_pending_artifacts(native, [(paths[0], True), (paths[1], False)])
        assert not any(str(call.args[0]) in paths for call in opened.call_args_list + io_opened.call_args_list)
    assert len(requests) == 2
    for (method, url, body), name in zip(requests, names, strict=True):
        assert (method, url) == ("POST", "https://ilink.example/ilink/bot/sendmessage")
        msg = body["msg"]
        assert (msg["to_user_id"], msg["context_token"]) == ("recipient", "conversation")
        [item] = msg["item_list"]
        assert item["type"] == 1
        notice = item["text_item"]["text"]
        assert name in notice and "Console" in notice and "会话产物" in notice
        assert str(tmp_path) not in notice and "https://" not in notice


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
