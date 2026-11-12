from __future__ import annotations

from io import BytesIO

from qwenpaw_data_auth import Principal
from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from qwenpaw_data.context.uploads import read_upload
from semantic_config.db import get_db
from semantic_config.errors import BadRequestError
from semantic_config.principal import current_principal
from semantic_config.services import excel_import_service as service
from semantic_config.template_generator import build_blank_workbook

router = APIRouter(prefix="/api/semantic-config/import", tags=["import"])


@router.post("/excel")
async def import_excel(
    file: UploadFile | None = File(default=None),
    datasource_id: str | None = Form(default=None),
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    """SPEC 01：Excel 导入必须带 datasource_id；表格内不写数据源列。"""
    if file is None:
        raise BadRequestError("上传文件不能为空")
    if not datasource_id or not datasource_id.strip():
        raise BadRequestError("datasource_id 不能为空")
    content = await read_upload(file)
    return await service.import_excel(
        db,
        file.filename or "",
        content,
        principal,
        datasource_id=datasource_id.strip(),
    )


@router.get("/excel/template")
async def download_template(
    object: str | None = Query(default=None),
    principal: Principal = Depends(current_principal),
):
    """SPEC 01：下载空白模板。object 缺省/all = 全对象；也可指定单对象 sheet。"""
    try:
        wb = build_blank_workbook(object_name=object)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    filename = "semantic-config-template.xlsx"
    if object and object not in ("all",):
        filename = f"semantic-config-template-{object}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
