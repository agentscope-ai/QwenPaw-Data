# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import timedelta
from email.utils import encode_rfc2231
from urllib.parse import urlparse

from qwenpaw_data.host.core.settings import OssSettings, get_settings


def _oss():
    # Optional "cloud" extra: import the OSS SDK only when a store is used.
    import alibabacloud_oss_v2 as oss

    return oss


class OssStore:
    def __init__(self, settings: OssSettings) -> None:
        settings.require_configured()
        oss = _oss()
        credentials = oss.credentials.StaticCredentialsProvider(
            access_key_id=settings.access_key_id,
            access_key_secret=settings.access_key_secret,
        )
        cfg = oss.config.load_default()
        cfg.credentials_provider = credentials
        cfg.region = region_from_endpoint(settings.endpoint)
        cfg.endpoint = settings.endpoint
        self._client = oss.Client(cfg)
        self._bucket = settings.bucket

    def upload_file(self, oss_path: str, file_bytes: bytes) -> None:
        oss = _oss()
        try:
            self._client.put_object(
                oss.PutObjectRequest(bucket=self._bucket, key=oss_path, body=file_bytes)
            )
        except oss.exceptions.BaseError as exc:
            raise PermissionError(str(exc)) from exc

    def create_signed_download_url(
        self,
        oss_path: str,
        *,
        expires_in: int,
        download_filename: str,
    ) -> str:
        oss = _oss()
        encoded = encode_rfc2231(download_filename, "utf-8")
        ascii_name = (
            download_filename.encode("ascii", "replace")
            .decode("ascii")
            .replace('"', "'")
        )
        result = self._client.presign(
            oss.GetObjectRequest(
                bucket=self._bucket,
                key=oss_path,
                response_content_disposition=(
                    f'attachment; filename="{ascii_name}"; filename*={encoded}'
                ),
            ),
            expires=timedelta(seconds=expires_in),
        )
        url = getattr(result, "url", None)
        if not url:
            raise RuntimeError("OSS did not return a signed download url")
        return str(url)


def region_from_endpoint(endpoint: str) -> str:
    host = (
        urlparse(endpoint if "://" in endpoint else f"https://{endpoint}").hostname
        or ""
    )
    label = host.split(".")[0]
    if not label.startswith("oss-"):
        raise ValueError(f"cannot infer OSS region from endpoint: {endpoint}")
    region = label.removeprefix("oss-")
    if region.endswith("-internal"):
        region = region[: -len("-internal")]
    if not region or region.startswith("accelerate"):
        raise ValueError(f"cannot infer OSS region from endpoint: {endpoint}")
    return region


def get_oss_store() -> OssStore:
    return OssStore(get_settings().oss)
