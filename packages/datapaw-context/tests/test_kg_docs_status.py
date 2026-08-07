from __future__ import annotations

import asyncio
import importlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from datapaw.context.blocking_io import BlockingIOGovernor

from context_manager.api import doc_api
from context_manager.kg_docs.ingest_status import IngestStatusStore
from context_manager.kg_docs.local_doc_client import LocalDocClient


class KgDocPathConfigTest(unittest.TestCase):
    def test_default_doc_storage_dir_uses_datapaw_home(self) -> None:
        old_home = os.environ.get("DATAPAW_HOME")
        old_storage = os.environ.get("DOC_STORAGE_DIR")
        temp_dir = tempfile.TemporaryDirectory()
        import context_manager.config as config_module

        try:
            os.environ["DATAPAW_HOME"] = temp_dir.name
            os.environ["DOC_STORAGE_DIR"] = ""

            reloaded = importlib.reload(config_module)
            cfg = reloaded.Config()
            self.assertEqual(
                Path(cfg.doc_storage_dir),
                Path(temp_dir.name).resolve() / "data-bridge" / "kg" / "documents",
            )
        finally:
            if old_home is None:
                os.environ.pop("DATAPAW_HOME", None)
            else:
                os.environ["DATAPAW_HOME"] = old_home
            if old_storage is None:
                os.environ.pop("DOC_STORAGE_DIR", None)
            else:
                os.environ["DOC_STORAGE_DIR"] = old_storage
            importlib.reload(config_module)
            temp_dir.cleanup()


class KgIngestCachePathTest(unittest.TestCase):
    def test_default_cache_and_report_paths_use_datapaw_home(self) -> None:
        old_home = os.environ.get("DATAPAW_HOME")
        temp_dir = tempfile.TemporaryDirectory()
        import datapaw.context.paths as paths_module
        import context_manager.knowledge.extractor as extractor_module
        import context_manager.knowledge.normalize as normalize_module

        try:
            os.environ["DATAPAW_HOME"] = temp_dir.name

            paths_module = importlib.reload(paths_module)
            extractor_module = importlib.reload(extractor_module)
            normalize_module = importlib.reload(normalize_module)

            expected_cache_dir = (
                Path(temp_dir.name).resolve() / "data-bridge" / "cache" / "knowledge_ingest"
            )
            expected_report_path = expected_cache_dir / "knowledge_ingest_report.md"

            self.assertEqual(paths_module.knowledge_ingest_cache_dir(), expected_cache_dir)
            self.assertEqual(paths_module.knowledge_ingest_report_path(), expected_report_path)
            self.assertEqual(extractor_module.DEFAULT_CACHE_DIR, expected_cache_dir)
            self.assertEqual(normalize_module.DEFAULT_CACHE_DIR, expected_cache_dir)
        finally:
            if old_home is None:
                os.environ.pop("DATAPAW_HOME", None)
            else:
                os.environ["DATAPAW_HOME"] = old_home
            importlib.reload(paths_module)
            importlib.reload(extractor_module)
            importlib.reload(normalize_module)
            temp_dir.cleanup()


class IngestStatusStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.storage_dir = Path(self.temp_dir.name) / "docs"
        self.storage_dir.mkdir()
        self.store = IngestStatusStore(self.storage_dir / ".kg-status")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_status_file_uses_document_filename(self) -> None:
        self.store.begin("kg-docs/report.md")
        self.assertEqual(
            self.store._status_path("kg-docs/report.md").name,
            "report.md.json",
        )

    def test_begin_finalize_ready_deletes_status_file(self) -> None:
        token = self.store.begin("kg-docs/report.md")
        status, error = self.store.resolve("report.md")
        self.assertEqual(status, "building")
        self.assertIsNone(error)

        updated = self.store.finalize("kg-docs/report.md", token, "ready")
        self.assertTrue(updated)
        status, error = self.store.resolve("report.md")
        self.assertEqual(status, "ready")
        self.assertIsNone(error)
        self.assertFalse(self.store._status_path("kg-docs/report.md").exists())

    def test_finalize_failed_keeps_status_file(self) -> None:
        token = self.store.begin("kg-docs/broken.docx")
        updated = self.store.finalize(
            "kg-docs/broken.docx",
            token,
            "failed",
            "Invalid API key",
        )
        self.assertTrue(updated)
        status, error = self.store.resolve("kg-docs/broken.docx")
        self.assertEqual(status, "failed")
        self.assertEqual(error, "Invalid API key")
        self.assertTrue(self.store._status_path("kg-docs/broken.docx").exists())

    def test_resolve_unknown_doc_as_ready(self) -> None:
        status, error = self.store.resolve("kg-docs/legacy.txt")
        self.assertEqual(status, "ready")
        self.assertIsNone(error)

    def test_finalize_ignores_stale_build_token(self) -> None:
        first = self.store.begin("kg-docs/report.md")
        second = self.store.begin("kg-docs/report.md")
        self.assertFalse(self.store.finalize("kg-docs/report.md", first, "ready"))
        self.assertTrue(self.store.finalize("kg-docs/report.md", second, "ready"))

    def test_remove_clears_status(self) -> None:
        self.store.mark_failed("kg-docs/report.md", "Invalid API key")
        self.store.remove("kg-docs/report.md")
        status, error = self.store.resolve("kg-docs/report.md")
        self.assertEqual(status, "ready")
        self.assertIsNone(error)

    def test_marks_interrupted_build_as_failed_after_restart(self) -> None:
        self.store.begin("kg-docs/report.md")
        with patch(
            "context_manager.kg_docs.ingest_status.os.kill",
            side_effect=ProcessLookupError,
        ):
            status, error = self.store.resolve("kg-docs/report.md")
        self.assertEqual(status, "failed")
        self.assertEqual(error, "Knowledge graph build was interrupted")

    def test_persists_across_store_instances(self) -> None:
        token = self.store.begin("kg-docs/report.md")
        self.store.finalize("kg-docs/report.md", token, "failed", "Invalid API key")

        restarted = IngestStatusStore(self.storage_dir / ".kg-status")
        status, error = restarted.resolve("kg-docs/report.md")
        self.assertEqual(status, "failed")
        self.assertEqual(error, "Invalid API key")


class LocalDocClientTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.client = LocalDocClient(Path(self.temp_dir.name) / "docs", max_size=1024)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_upload_and_delete(self) -> None:
        uploaded = self.client.upload("report.md", b"hello")
        self.assertEqual(uploaded.doc_id, "kg-docs/report.md")
        self.assertTrue(self.client.file_exists(uploaded.doc_id))

        deleted = self.client.delete(uploaded.doc_id)
        self.assertEqual(deleted, "kg-docs/report.md")
        self.assertFalse(self.client.file_exists(uploaded.doc_id))


class KgIngestWorkerStatusTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.storage_dir = Path(self.temp_dir.name) / "docs"
        self.client = LocalDocClient(self.storage_dir, max_size=1024)
        self.store = IngestStatusStore(self.storage_dir / ".kg-status")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_marks_document_ready(self) -> None:
        doc = self.client.upload("report.md", b"hello")
        token = self.store.begin(doc.doc_id)

        with patch.object(doc_api, "build_kg_from_bytes", return_value=None):
            doc_api._run_kg_ingest_sync(
                self.client,
                self.store,
                object(),
                doc.doc_id,
                doc.filename,
                b"hello",
                token,
            )

        status, error = self.store.resolve(doc.doc_id)
        self.assertEqual(status, "ready")
        self.assertIsNone(error)

    def test_marks_document_failed(self) -> None:
        doc = self.client.upload("broken.md", b"hello")
        token = self.store.begin(doc.doc_id)

        with patch.object(
            doc_api,
            "build_kg_from_bytes",
            side_effect=RuntimeError("Invalid API key provided"),
        ):
            doc_api._run_kg_ingest_sync(
                self.client,
                self.store,
                object(),
                doc.doc_id,
                doc.filename,
                b"hello",
                token,
            )

        status, error = self.store.resolve(doc.doc_id)
        self.assertEqual(status, "failed")
        self.assertEqual(error, "Invalid API key")

    def test_skips_finalize_when_document_removed(self) -> None:
        doc = self.client.upload("report.md", b"hello")
        token = self.store.begin(doc.doc_id)
        self.client.delete(doc.doc_id)
        self.store.remove(doc.doc_id)

        with patch.object(doc_api, "build_kg_from_bytes", return_value=None):
            doc_api._run_kg_ingest_sync(
                self.client,
                self.store,
                object(),
                doc.doc_id,
                doc.filename,
                b"hello",
                token,
            )

        status, error = self.store.resolve(doc.doc_id)
        self.assertEqual(status, "ready")
        self.assertIsNone(error)


class KgDocsApiContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.storage_dir = Path(self.temp_dir.name) / "docs"
        self.local_client = LocalDocClient(self.storage_dir, max_size=1024)
        self.status_store = IngestStatusStore(self.storage_dir / ".kg-status")
        app = FastAPI()
        app.state.driver = None
        self.governor = BlockingIOGovernor()
        app.state.blocking_io = self.governor
        app.include_router(doc_api.router)
        self.http = TestClient(app)

    def tearDown(self) -> None:
        self.http.close()
        asyncio.run(self.governor.aclose())
        self.temp_dir.cleanup()

    def test_list_matches_ingest_contract(self) -> None:
        doc = self.local_client.upload("broken.md", b"hello")
        self.status_store.mark_failed(doc.doc_id, "Invalid API key")

        with patch.object(doc_api, "get_local_doc_client", return_value=self.local_client), patch.object(
            doc_api,
            "get_ingest_status_store",
            return_value=self.status_store,
        ):
            response = self.http.get("/api/v1/docs")

        self.assertEqual(response.status_code, 200)
        item = response.json()["data"]["list"][0]
        self.assertEqual(item["doc_id"], "kg-docs/broken.md")
        self.assertEqual(item["ingest_status"], "failed")
        self.assertEqual(item["ingest_error"], "Invalid API key")

    def test_prefixed_doc_id_download_and_delete(self) -> None:
        self.local_client.upload("report.md", b"hello")

        with patch.object(doc_api, "get_local_doc_client", return_value=self.local_client), patch.object(
            doc_api,
            "get_ingest_status_store",
            return_value=self.status_store,
        ):
            download = self.http.get("/api/v1/docs/kg-docs/report.md/download")
            deleted = self.http.delete("/api/v1/docs/kg-docs%2Freport.md")

        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.content, b"hello")
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(deleted.json()["data"]["doc_id"], "kg-docs/report.md")


if __name__ == "__main__":
    unittest.main()
