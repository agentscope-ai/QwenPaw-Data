from __future__ import annotations

import aiosqlite
from fastapi import APIRouter, Depends, File, UploadFile

from datapaw.context.uploads import read_upload

from semantic_config.db import get_db
from semantic_config.errors import BadRequestError
from semantic_config.services import excel_import_service as service

router = APIRouter(prefix="/api/semantic-config/import", tags=["import"])


@router.post("/excel")
async def import_excel(
    file: UploadFile | None = File(default=None),
    db: aiosqlite.Connection = Depends(get_db),
):
    if file is None:
        raise BadRequestError("上传文件不能为空")
    content = await read_upload(file)
    return await service.import_excel(db, file.filename or "", content)
