import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from context_manager.api.model_config_api import EmbeddingConfigPayload, router
from context_manager.model_config_store import ModelConfigStore


EMBEDDING_FIELDS = {"model", "base_url", "api_key", "dim"}


class ModelConfigStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.path = Path(self.temp_dir.name) / "models.json"

    def test_initial_embedding_config_uses_remote_contract(self) -> None:
        store = ModelConfigStore(self.path)

        embedding = store.get_masked()["embedding"]

        self.assertEqual(set(embedding), EMBEDDING_FIELDS)
        self.assertEqual(
            set(EmbeddingConfigPayload.model_fields),
            EMBEDDING_FIELDS,
        )

    def test_get_endpoint_initializes_without_local_embedding_fields(self) -> None:
        store = ModelConfigStore(self.path)
        app = FastAPI()
        app.include_router(router)

        with patch(
            "context_manager.api.model_config_api.get_model_config_store",
            return_value=store,
        ):
            response = TestClient(app).get("/api/system/model-config/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json()["embedding"]), EMBEDDING_FIELDS)

    def test_api_keys_are_masked_with_four_visible_characters_per_side(self) -> None:
        self.path.write_text(
            json.dumps(
                {
                    "llm": {"api_key": "abcd12345678wxyz"},
                    "embedding": {"api_key": "short"},
                }
            ),
            encoding="utf-8",
        )
        store = ModelConfigStore(self.path)

        masked = store.get_masked()

        self.assertEqual(masked["llm"]["api_key"], "abcd****wxyz")
        self.assertEqual(masked["embedding"]["api_key"], "****")

    def test_legacy_local_fields_are_filtered_and_not_persisted_on_update(self) -> None:
        self.path.write_text(
            json.dumps(
                {
                    "llm": {},
                    "embedding": {
                        "provider": "local",
                        "device": "cpu",
                        "model": "legacy-model",
                        "base_url": "https://example.com/v1",
                        "api_key": "legacy-secret",
                        "dim": 384,
                    },
                }
            ),
            encoding="utf-8",
        )
        store = ModelConfigStore(self.path)

        self.assertEqual(set(store.get_masked()["embedding"]), EMBEDDING_FIELDS)

        with patch.object(store, "_invalidate_embedding_client"):
            updated, rebuild_required = store.update_embedding(
                {
                    "provider": "local",
                    "device": "cpu",
                    "model": "remote-model",
                    "base_url": "https://example.com/v1",
                    "api_key": "",
                    "dim": 1024,
                }
            )

        persisted = json.loads(self.path.read_text(encoding="utf-8"))["embedding"]
        self.assertEqual(set(updated), EMBEDDING_FIELDS)
        self.assertEqual(set(persisted), EMBEDDING_FIELDS)
        self.assertEqual(persisted["model"], "remote-model")
        self.assertEqual(persisted["api_key"], "legacy-secret")
        self.assertTrue(rebuild_required)

    def test_embedding_connection_test_uses_openai_compatible_client(self) -> None:
        store = ModelConfigStore(self.path)
        response = MagicMock()
        response.data = [MagicMock(embedding=[0.1, 0.2, 0.3])]

        with patch("openai.OpenAI") as openai_client:
            openai_client.return_value.embeddings.create.return_value = response
            result = store.test_embedding(
                {
                    "model": "remote-model",
                    "base_url": "https://example.com/v1",
                    "api_key": "secret",
                }
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["detected_dim"], 3)
        openai_client.return_value.embeddings.create.assert_called_once_with(
            model="remote-model",
            input=["test"],
        )


if __name__ == "__main__":
    unittest.main()
