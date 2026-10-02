"""日常巡查接口：维护巡查记录，覆盖开始巡查、完成巡查、复核确认与派单。

写操作要求登录身份；派单必须由当前班次班长/值班员发起，旧班次残留身份会被
服务层拦截。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.schemas import ActionResult, DispatchPayload, EntryPayload, PageResult
from app.services.handover import handover_service
from app.services.identity import Identity, require_identity
from app.services.patrol import PatrolService

router = APIRouter(prefix="/api/patrol", tags=["日常巡查"])

service = PatrolService()

STATUSES = ["待巡查", "巡查中", "已完成", "已复核"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按巡查编号检索"),
    status: str | None = Query(default=None, description="待巡查、巡查中、已完成、已复核"),
    crew: str | None = Query(default=None, description="按责任班组过滤"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按巡查编号与状态过滤日常巡查列表；列表字段与详情接口完全一致。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(
        keyword=keyword, status_value=status, crew=crew, page=page, size=size
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出日常巡查清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "patrol", "total": total, "items": items}


@router.get("/vehicle-tasks", response_model=list[dict])
def vehicle_tasks(
    plate: str | None = Query(default=None, description="按车牌过滤"),
    driver_code: str | None = Query(default=None, description="按当班司机工号过滤"),
) -> list[dict[str, Any]]:
    """车辆任务看板：同一任务只按当前责任出现一次，旧身份不会再重复看到。"""
    return handover_service.vehicle_tasks(plate=plate, driver_code=driver_code)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict[str, Any]:
    """读取单条巡查记录明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"巡查记录 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload, identity: Identity = Depends(require_identity)) -> ActionResult:
    """登记一条巡查记录，缺字段时说明原因而不是静默丢弃。"""
    values = dict(payload.values)
    values.setdefault("巡查人员", identity.name)
    entry, missing = service.create_entry(values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="巡查记录已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条巡查记录执行开始巡查、完成巡查、复核确认；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/dispatch", response_model=ActionResult)
def dispatch_entry(
    entry_id: int,
    payload: DispatchPayload,
    identity: Identity = Depends(require_identity),
) -> ActionResult:
    """派单/转派：鉴权缓存校验当前班次身份，任务、通知、待办、审计原子提交。"""
    entry, message = service.dispatch(
        entry_id,
        identity,
        driver=payload.driver,
        vehicle_plate=payload.vehicle_plate,
        note=payload.note,
    )
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
