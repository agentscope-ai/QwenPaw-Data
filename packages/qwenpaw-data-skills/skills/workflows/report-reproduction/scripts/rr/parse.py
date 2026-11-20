# -*- coding: utf-8 -*-
"""Parse a source report into Sections.

The tree it writes is owned by the CLI (R5). It does not advance a Section;
that stays with the turn guard (R4).
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import re
import shutil
import unicodedata
import socket
import ssl
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunsplit

from rr.common import (
    COMPARISON_FILE,
    MANIFEST_FILE,
    ReportCliError,
    _CONTENT_SUFFIXES,
    _MAX_DOWNLOAD_BYTES,
    _SOURCE_SUFFIXES,
    _SYNTHETIC_SECTION_TITLES,
)
from rr.models import ReportManifest, ReportSection, SectionProgress


def _gold_splitter(media_dir: Path | None) -> Any:
    """Build the deterministic heading-aware splitter shipped with this skill.

    Lazy for the same reason as the comparison kernel: only ``parse`` needs it,
    and a missing dependency must be a clear message rather than an ImportError
    traceback that hides which subcommand actually failed. The package sits next
    to this script, so it is resolved through the script directory and never
    depends on how the interpreter was pointed at this file.
    """
    scripts_dir = str(Path(__file__).resolve().parents[1])
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    try:
        from gold_parser import ReportSectionSplitter
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise ReportCliError(
            "parse 子命令需要本 Skill 自带的 scripts/gold_parser 包，当前 Python "
            "环境导入不到它。请确认 Skill 目录完整（该包随 Skill 一起分发，不是 "
            f"PyPI 第三方包），原始导入错误：{exc}"
        ) from exc
    return ReportSectionSplitter(media_dir=media_dir)


def split_report(
    source_path: Path,
    *,
    report_id: str | None = None,
    media_dir: Path | None = None,
) -> Any:
    splitter = _gold_splitter(media_dir)
    report = splitter.split(source_path, report_id=report_id)
    if not report.sections:
        raise ReportCliError(
            "报告中没有可解析的章节；请使用 DOCX、HTML、Markdown 或 TXT，"
            "并确认文档含有标题结构"
        )
    return report


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection bound to an already vetted address.

    Resolving the hostname a second time would open a DNS rebinding window
    between the public-address check and the actual connect, so the checked
    address is pinned and the hostname is only used for SNI and certificate
    validation.
    """

    def __init__(self, hostname: str, address: str, port: int) -> None:
        super().__init__(hostname, port=port, timeout=20)
        self._address = address

    def connect(self) -> None:
        raw_socket = socket.create_connection(
            (self._address, self.port),
            timeout=self.timeout,
        )
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.sock = context.wrap_socket(raw_socket, server_hostname=self.host)


def _resolve_public_host(hostname: str, port: int) -> str:
    addresses: list[str] = []
    for record in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM):
        address = ipaddress.ip_address(record[4][0])
        if not address.is_global:
            raise ReportCliError(
                f"报告 URL 必须解析到公网地址，禁止内网或回环地址：{hostname}"
            )
        addresses.append(str(address))
    if not addresses:
        raise ReportCliError(f"报告 URL 域名无法解析：{hostname}")
    return addresses[0]


def _resolve_report_url(url: str) -> tuple[Any, int, str]:
    """Vet a report URL and pin the address it resolves to.

    Returns ``(parsed, port, address)``. Split out of the download for two
    reasons: ``initialize`` can refuse a URL before it creates anything on
    disk, and the address that passed the public-range check is the very
    address that gets dialled — resolving the name a second time would reopen
    the DNS rebinding window this guard exists to close.
    """
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ReportCliError("报告 URL 必须使用 http 或 https")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return parsed, port, _resolve_public_host(parsed.hostname, port)


def load_report_url(url: str, destination_dir: Path) -> Path:
    """Download a report over HTTP(S) without following redirects."""
    parsed, port, address = _resolve_report_url(url)
    connection: http.client.HTTPConnection
    if parsed.scheme == "https":
        connection = _PinnedHTTPSConnection(parsed.hostname, address, port)
    else:
        connection = http.client.HTTPConnection(address, port=port, timeout=20)
    request_path = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    host_header = parsed.hostname if parsed.port is None else parsed.netloc
    try:
        connection.request(
            "GET",
            request_path,
            headers={
                "Host": host_header,
                "User-Agent": "QwenPaw-Data-Report-Reproduction/0.2",
                "Accept": (
                    "text/html,text/markdown,text/plain,"
                    "application/vnd.openxmlformats-officedocument."
                    "wordprocessingml.document"
                ),
            },
        )
        response = connection.getresponse()
        if 300 <= response.status < 400:
            raise ReportCliError("报告 URL 不允许重定向；请直接提供最终地址")
        if response.status < 200 or response.status >= 300:
            raise ReportCliError(f"报告 URL 返回 HTTP {response.status}")
        content_type = response.headers.get_content_type()
        payload = response.read(_MAX_DOWNLOAD_BYTES + 1)
    except (http.client.HTTPException, OSError) as exc:
        raise ReportCliError(f"下载报告失败：{exc}") from exc
    finally:
        connection.close()
    if len(payload) > _MAX_DOWNLOAD_BYTES:
        raise ReportCliError("报告 URL 响应超过 10 MB 上限")

    suffix = Path(parsed.path).suffix.lower() or _CONTENT_SUFFIXES.get(
        content_type, ".html"
    )
    if suffix not in _SOURCE_SUFFIXES:
        raise ReportCliError(f"不支持的报告内容类型：{content_type}")
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / f"original{suffix}"
    destination.write_bytes(payload)
    return destination


def _slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip().lower()
    normalized = re.sub(r"[^\w\u4e00-\u9fff]+", "_", normalized).strip("_")
    return normalized[:80] or "section"


def _render_subsection(subsection: Any) -> str:
    hashes = "#" * max(2, min(subsection.level, 6))
    parts = [f"{hashes} {subsection.title}"]
    if subsection.content.strip():
        parts.append(subsection.content.strip())
    for child in subsection.children:
        rendered = _render_subsection(child)
        if rendered:
            parts.append(rendered)
    return "\n".join(parts).strip()


def _subsection_body(subsection: Any) -> str:
    parts: list[str] = []
    if subsection.content.strip():
        parts.append(subsection.content.strip())
    for child in subsection.children:
        rendered = _render_subsection(child)
        if rendered:
            parts.append(rendered)
    return "\n".join(parts).strip()


def _promote_subsections(parent: Any) -> list[tuple[str, str, int, str]]:
    items: list[tuple[str, str, int, str]] = []
    for subsection in parent.subsections:
        raw_text = _subsection_body(subsection)
        if not raw_text.strip():
            continue
        section_id = (
            f"{parent.section_id}__{subsection.order:02d}_{_slug(subsection.title)}"
        )
        items.append((section_id, subsection.title, subsection.level, raw_text))
    return items


def _manifest_sections(
    parsed_sections: list[Any],
) -> list[tuple[str, str, int, str]]:
    # Header chrome before the first heading (kicker / meta) used to become a
    # synthetic "正文" root and blocked promoting 一、/二、 H2 chapters.
    working = [
        section
        for section in parsed_sections
        if section.title.strip() not in _SYNTHETIC_SECTION_TITLES
    ]
    if not working:
        working = parsed_sections
    if len(working) == 1 and working[0].subsections:
        promoted = _promote_subsections(working[0])
        if promoted:
            return promoted
    return [
        (
            parsed_section.section_id,
            parsed_section.title,
            parsed_section.level,
            parsed_section.raw_text,
        )
        for parsed_section in working
        if parsed_section.raw_text.strip()
    ]


def _validate_source(source_path: Path | None, url: str | None) -> None:
    """Reject an unusable report source before anything is written.

    A refused URL or suffix must leave no trace: an empty product root would
    not trigger the workflow marker, but it is still litter that the next
    ``parse`` would have to reason about.
    """
    if bool(source_path) == bool(url):
        raise ReportCliError("parse 需要且只需要 --file 与 --url 中的一个")
    if url is not None:
        _resolve_report_url(url)
        return
    assert source_path is not None
    if source_path.suffix.lower() not in _SOURCE_SUFFIXES:
        raise ReportCliError(
            "支持的报告格式：DOCX、HTML、Markdown、TXT（.docx/.html/.htm/.md/.txt）"
        )


def parse_document(
    *,
    product_root: Path,
    source_path: Path | None = None,
    url: str | None = None,
    report_id: str | None = None,
    source_period: str | None = None,
    target_period: str | None = None,
) -> ReportManifest:
    """Materialise the source report and split it into reproducible Sections."""
    _validate_source(source_path, url)

    source_dir = product_root / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    if url:
        materialized = load_report_url(url, source_dir)
    else:
        assert source_path is not None
        materialized = source_dir / f"original{source_path.suffix.lower()}"
        if source_path != materialized:
            shutil.copy2(source_path, materialized)

    parsed = split_report(
        materialized,
        report_id=report_id,
        media_dir=source_dir / "media",
    )
    section_progress: list[SectionProgress] = []
    previous_id: str | None = None
    for section_id, title, level, raw_text in _manifest_sections(parsed.sections):
        section_dir = product_root / "sections" / section_id
        section_dir.mkdir(parents=True, exist_ok=True)
        (section_dir / "baseline" / "data").mkdir(parents=True, exist_ok=True)
        source_md = section_dir / "source.md"
        source_md.write_text(
            f"# {title}\n\n{raw_text.strip()}\n",
            encoding="utf-8",
        )
        section_progress.append(
            SectionProgress(
                section=ReportSection(
                    section_id=section_id,
                    title=title,
                    level=level,
                    source_path=source_md.relative_to(product_root).as_posix(),
                    draft_path=(section_dir / "baseline" / "draft.md")
                    .relative_to(product_root)
                    .as_posix(),
                    spec_path=(section_dir / "spec.json")
                    .relative_to(product_root)
                    .as_posix(),
                    comparison_path=(section_dir / "baseline" / COMPARISON_FILE)
                    .relative_to(product_root)
                    .as_posix(),
                    blocked_by=[previous_id] if previous_id else [],
                )
            )
        )
        previous_id = section_id

    return ReportManifest(
        report_id=parsed.report_id,
        title=parsed.title,
        source_path=materialized.relative_to(product_root).as_posix(),
        source_period=source_period,
        target_period=target_period,
        sections=section_progress,
    )
