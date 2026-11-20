from fastapi import FastAPI

from semantic_config.integration import mount_semantic_config


def test_integrated_app_mounts_excel_export_route():
    app = FastAPI()

    mount_semantic_config(app)

    assert "/api/semantic-config/export/excel" in app.openapi()["paths"]
