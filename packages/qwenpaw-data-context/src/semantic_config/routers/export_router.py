from __future__ import annotations

from io import BytesIO

from qwenpaw_data_auth import Principal
from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from semantic_config.db import get_db
from semantic_config.principal import current_principal
from semantic_config.services import excel_export_service as service

router = APIRouter(prefix="/api/semantic-config/export", tags=["export"])


@router.get("/excel")
async def export_excel(
    datasource_id: str | None = Query(default=None),
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    """SPEC 01：导出当前数据源正式语义。sheet/列与导入契约一致，不含数据源列。"""
    content = await service.export_excel(db, datasource_id or "", principal)
    buf = BytesIO(content)
    filename = f"semantic-config-{datasource_id or 'export'}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
