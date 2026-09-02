# -*- coding: utf-8 -*-
"""WeChat iLink QR-code login handler.

Two methods used by the channel-config API to support QR-code login when a
``bot_token`` is not pre-configured:

- ``fetch_qrcode()`` — call iLink ``get_bot_qrcode``, build the scan URL, and
  render it as a base64 PNG (via ``segno``).
- ``poll_status(token)`` — call iLink ``get_qrcode_status`` and return the
  authorization status plus the ``bot_token`` credential on success.

The frontend polls ``poll_status`` until ``status == "confirmed"``; the
returned ``bot_token`` is then written into the wechat config form and saved.
"""
from __future__ import annotations

import base64
import io
import logging
from dataclasses import dataclass
from typing import Any, Dict

from fastapi import HTTPException

from .bot_client import WeChatILinkClient

logger = logging.getLogger("qwenpaw_data.channels.wechat.qrcode_auth")


@dataclass
class QRCodeResult:
    """Value object returned by ``fetch_qrcode``."""

    qrcode_img: str  # base64-encoded PNG
    poll_token: str  # opaque token used for subsequent polling


@dataclass
class PollResult:
    """Value object returned by ``poll_status``."""

    status: str
    credentials: Dict[str, Any]


def generate_qrcode_image(scan_url: str) -> str:
    """Generate a base64-encoded PNG QR code image from *scan_url*."""
    try:
        import segno
    except ImportError as exc:  # pragma: no cover
        raise HTTPException(
            status_code=500,
            detail="QR code generation requires the 'segno' package",
        ) from exc
    try:
        qr = segno.make(scan_url, error="M")
        buf = io.BytesIO()
        qr.save(buf, kind="png", scale=6, border=2)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"QR code image generation failed: {exc}",
        ) from exc


class WeChatQRCodeAuthHandler:
    """QR code auth handler for WeChat iLink Bot login."""

    async def fetch_qrcode(self) -> QRCodeResult:
        client = WeChatILinkClient()
        await client.start()
        try:
            qr_data = await client.get_bot_qrcode()
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"WeChat QR code fetch failed: {exc}",
            ) from exc
        finally:
            await client.stop()

        qrcode = qr_data.get("qrcode", "")
        qrcode_img_content = qr_data.get("qrcode_img_content", "")

        if not qrcode and not qrcode_img_content:
            raise HTTPException(
                status_code=502,
                detail="WeChat returned empty QR code data",
            )

        # qrcode_img_content may itself be an HTTP URL to scan; otherwise build
        # the canonical iLink scan URL from the qrcode token.
        if qrcode_img_content.startswith("http"):
            scan_url = qrcode_img_content
        else:
            scan_url = (
                "https://liteapp.weixin.qq.com/q/7GiQu1"
                f"?qrcode={qrcode}&bot_type=3"
            )

        return QRCodeResult(
            qrcode_img=generate_qrcode_image(scan_url),
            poll_token=qrcode,
        )

    async def poll_status(self, token: str) -> PollResult:
        client = WeChatILinkClient()
        await client.start()
        try:
            data = await client.get_qrcode_status(token)
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"WeChat status check failed: {exc}",
            ) from exc
        finally:
            await client.stop()

        return PollResult(
            status=data.get("status", "waiting"),
            credentials={
                "bot_token": data.get("bot_token", ""),
                "base_url": data.get("baseurl", ""),
            },
        )


# Singleton — the handler is stateless.
wechat_qrcode_auth_handler = WeChatQRCodeAuthHandler()
