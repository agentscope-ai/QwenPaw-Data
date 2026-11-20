# -*- coding: utf-8 -*-
"""WeChatILinkClient: async HTTP client for the WeChat iLink Bot API.

Ported from QwenPaw ``app/channels/wechat/client.py``. All iLink API endpoints
live under https://ilinkai.weixin.qq.com. Protocol: HTTP/JSON, no third-party
SDK required. The client uses ``httpx`` for transport.

Authentication: a ``bot_token`` (bearer token) is supplied externally and used
for all requests. The token is obtained in one of two ways:

1. Pre-configured in the channel config (persisted in the ``channels`` DB
   table) — used when the user already has a bot_token.
2. QR-code login — when no bot_token is configured, the UI calls
   ``get_bot_qrcode()`` → ``get_qrcode_status()`` (polled) to obtain a
   ``bot_token``, which is then saved into the config.

Native attachment uploads and encrypted downloads are not supported.
"""

from __future__ import annotations

import base64
import logging
import secrets
import uuid
from typing import Any, Dict, Optional
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
_CHANNEL_VERSION = "2.0.1"
# Long-poll hold time is up to 35 seconds (server-controlled)
_GETUPDATES_TIMEOUT = 45.0
_DEFAULT_TIMEOUT = 15.0
# iLink holds QR-status polls up to ~30s; use a dedicated longer timeout so the
# transport never cuts off the status check before the server responds.
_QRCODE_STATUS_TIMEOUT = 60.0


def make_headers(bot_token: str = "") -> Dict[str, str]:
    """Build iLink API request headers.

    X-WECHAT-UIN: base64(str(random_uint32)) — anti-replay, per request.
    Authorization: Bearer <bot_token> — only set when token is available.
    """
    uin_val = secrets.randbelow(0xFFFFFFFF)
    uin_b64 = base64.b64encode(str(uin_val).encode()).decode()
    headers: Dict[str, str] = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": uin_b64,
    }
    if bot_token:
        headers["Authorization"] = f"Bearer {bot_token}"
    return headers


class WeChatILinkClient:
    """Async HTTP client for the WeChat iLink Bot API.

    Args:
        bot_token: Bearer token (configured externally; no QR login).
        base_url: iLink API base URL (defaults to ilinkai.weixin.qq.com).
    """

    def __init__(
        self,
        bot_token: str = "",
        base_url: str = _DEFAULT_BASE_URL,
    ) -> None:
        self.bot_token = bot_token
        self.base_url = base_url.rstrip("/")
        self._client: Optional[httpx.AsyncClient] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Create the underlying httpx client."""
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(_GETUPDATES_TIMEOUT),
        )

    async def stop(self) -> None:
        """Close the underlying httpx client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _url(self, path: str) -> str:
        path = path.lstrip("/")
        return f"{self.base_url}/{path}"

    async def _get(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> Any:
        if self._client is None:
            raise RuntimeError("WeChatILinkClient not started")
        headers = make_headers(self.bot_token)
        resp = await self._client.get(
            self._url(path),
            params=params or {},
            headers=headers,
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()

    async def _post(
        self,
        path: str,
        body: Dict[str, Any],
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> Any:
        if self._client is None:
            raise RuntimeError("WeChatILinkClient not started")
        headers = make_headers(self.bot_token)
        resp = await self._client.post(
            self._url(path),
            json=body,
            headers=headers,
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Auth APIs (QR-code login; used when bot_token is not pre-configured)
    # ------------------------------------------------------------------

    async def get_bot_qrcode(self) -> Dict[str, Any]:
        """Fetch login QR code.

        Returns dict with keys:
            qrcode (str): QR code string to poll status.
            qrcode_img_content (str): Base64-encoded PNG image, or an HTTP URL,
                of the QR code.
        """
        return await self._get("ilink/bot/get_bot_qrcode", {"bot_type": 3})

    async def get_qrcode_status(self, qrcode: str) -> Dict[str, Any]:
        """Poll QR code scan status.

        Returns dict with keys:
            status (str): "waiting" | "scanned" | "confirmed" | "expired"
            bot_token (str): Bearer token (only when status=="confirmed")
            baseurl (str): API base URL (only when status=="confirmed")
        """
        return await self._get(
            "ilink/bot/get_qrcode_status",
            {"qrcode": qrcode},
            timeout=_QRCODE_STATUS_TIMEOUT,
        )

    # ------------------------------------------------------------------
    # Messaging APIs
    # ------------------------------------------------------------------

    async def getupdates(self, cursor: str = "") -> Dict[str, Any]:
        """Long-poll for incoming messages (holds up to 35 seconds).

        Args:
            cursor: get_updates_buf from previous response; empty on first call.

        Returns:
            Dict with keys:
                ret (int): 0 = success.
                msgs (list): List of WeChatMessage dicts (may be absent).
                get_updates_buf (str): Cursor for next call.
                longpolling_timeout_ms (int): Server-side hold time.
        """
        body: Dict[str, Any] = {
            "get_updates_buf": cursor,
            "base_info": {"channel_version": _CHANNEL_VERSION},
        }
        return await self._post(
            "ilink/bot/getupdates",
            body,
            timeout=_GETUPDATES_TIMEOUT,
        )

    async def sendmessage(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        """Send a message to a WeChat user.

        Args:
            msg: Message dict. Required fields:
                to_user_id (str): Recipient user ID (xxx@im.wechat).
                message_type (int): 2 = BOT.
                message_state (int): 2 = FINISH.
                context_token (str): Token from inbound message (REQUIRED).
                item_list (list): Content items.

        Returns:
            API response dict.
        """
        return await self._post(
            "ilink/bot/sendmessage",
            {"msg": msg, "base_info": {"channel_version": _CHANNEL_VERSION}},
        )

    async def send_text(
        self,
        to_user_id: str,
        text: str,
        context_token: str,
    ) -> Dict[str, Any]:
        """Convenience: send a plain text message.

        Args:
            to_user_id: Recipient user ID.
            text: Message text.
            context_token: context_token from the inbound message.

        Returns:
            API response dict.
        """
        return await self.sendmessage(
            {
                "from_user_id": "",
                "to_user_id": to_user_id,
                "client_id": str(uuid.uuid4()),
                "message_type": 2,
                "message_state": 2,
                "context_token": context_token,
                "item_list": [{"type": 1, "text_item": {"text": text}}],
            },
        )

    async def getconfig(
        self,
        ilink_user_id: str = "",
        context_token: str = "",
    ) -> Dict[str, Any]:
        """Fetch bot config (e.g. typing_ticket).

        Args:
            ilink_user_id: User ID for the config request.
            context_token: Context token for the config request.

        Returns:
            API response dict.
        """
        body: Dict[str, Any] = {}
        if ilink_user_id:
            body["ilink_user_id"] = ilink_user_id
        if context_token:
            body["context_token"] = context_token
        body["base_info"] = {"channel_version": _CHANNEL_VERSION}
        return await self._post("ilink/bot/getconfig", body)

    async def sendtyping(
        self,
        to_user_id: str,
        typing_ticket: str,
        status: int = 1,
    ) -> Dict[str, Any]:
        """Send "typing..." indicator to a user.

        Args:
            to_user_id: Recipient user ID.
            typing_ticket: Ticket from getconfig().
            status: 1 = start typing, 2 = stop typing.

        Returns:
            API response dict.
        """
        logger.debug(
            "ILinkClient sendtyping: to_user_id=%s..., ticket=%s..., status=%s",
            to_user_id[:20],
            typing_ticket[:20],
            status,
        )
        resp = await self._post(
            "ilink/bot/sendtyping",
            {
                "ilink_user_id": to_user_id,
                "typing_ticket": typing_ticket,
                "status": status,
                "base_info": {"channel_version": _CHANNEL_VERSION},
            },
        )
        ret = resp.get("ret", -1)
        errcode = resp.get("errcode", -1)
        logger.debug(
            "ILinkClient sendtyping response: ret=%s, errcode=%s",
            ret,
            errcode,
        )
        return resp

    # ------------------------------------------------------------------
    # Media helpers
    # ------------------------------------------------------------------

    async def download_media(
        self,
        url: str,
        aes_key_b64: str = "",
        encrypt_query_param: str = "",
    ) -> bytes:
        """Download unencrypted iLink media over HTTPS.

        iLink media files are stored on https://novac2c.cdn.weixin.qq.com/c2c.
        The 'url' field in image_item/file_item is a hex media-ID (not HTTP).
        The actual download URL is built from CDN base + encrypt_query_param.

        Args:
            url: CDN HTTPS URL, or hex media-ID (ignored if encrypt_query_param).
            aes_key_b64: If supplied, reject the encrypted attachment before fetching it.
            encrypt_query_param: Query param from media.encrypt_query_param;
                if provided, use CDN base URL + this param to download.

        Returns:
            Raw file bytes.
        """
        if aes_key_b64:
            raise ValueError("Encrypted WeChat media downloads are not supported")
        if self._client is None:
            raise RuntimeError("WeChatILinkClient not started")

        if encrypt_query_param:
            cdn_base = "https://novac2c.cdn.weixin.qq.com/c2c"
            # Note: parameter name is "encrypted_query_param" (with 'd')
            enc = quote(encrypt_query_param, safe="")
            download_url = f"{cdn_base}/download?encrypted_query_param={enc}"
        else:
            target = httpx.URL(url)
            if target.scheme != "https" or not target.host:
                raise ValueError("WeChat media downloads require an HTTPS URL")
            download_url = str(target)

        resp = await self._client.get(download_url, timeout=60.0)
        resp.raise_for_status()
        return resp.content
