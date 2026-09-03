from __future__ import annotations

import aiosqlite
from fastapi import APIRouter, Depends, Query

from semantic_config.db import get_db
from semantic_config.models.metric_formula_lib import FormulaCreate, FormulaResponse, FormulaUpdate
from semantic_config.pagination import Page
from semantic_config.services import metric_formula_lib_service as service

router = APIRouter(prefix="/api/semantic-config/metric-formula-lib", tags=["metric-formula-lib"])


@router.post("", response_model=FormulaResponse)
async def create_formula(payload: FormulaCreate, db: aiosqlite.Connection = Depends(get_db, scope="function")):
    return await service.create(db, payload)


@router.put("/{fid}", response_model=FormulaResponse)
async def update_formula(fid: int, payload: FormulaUpdate, db: aiosqlite.Connection = Depends(get_db, scope="function")):
    return await service.update(db, fid, payload)


@router.get("/dataset/{dataset_id}", response_model=list[FormulaResponse])
async def list_formulas_by_dataset(dataset_id: int, db: aiosqlite.Connection = Depends(get_db, scope="function")):
    return await service.list_by_dataset(db, dataset_id)


@router.get("/{fid}", response_model=FormulaResponse)
async def get_formula(fid: int, db: aiosqlite.Connection = Depends(get_db, scope="function")):
    return await service.get_one(db, fid)


@router.get("", response_model=Page[FormulaResponse])
async def list_formula(
    datasource_id: str | None = Query(default=None),
    domain_id: int | None = Query(default=None),
    metric_id: int | None = Query(default=None),
    dataset_id: int | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1),
    db: aiosqlite.Connection = Depends(get_db, scope="function"),
):
    return await service.list_page(db, datasource_id, domain_id, metric_id, dataset_id, page, size)


@router.delete("/dataset/{dataset_id}")
async def delete_formulas_by_dataset(dataset_id: int, db: aiosqlite.Connection = Depends(get_db, scope="function")):
    return await service.delete_by_dataset(db, dataset_id)


@router.delete("/{fid}")
async def delete_formula(fid: int, db: aiosqlite.Connection = Depends(get_db, scope="function")):
    return await service.delete(db, fid)
