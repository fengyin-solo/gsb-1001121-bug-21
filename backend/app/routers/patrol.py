"""日常巡查任务接口：列表/详情同口径，派单走鉴权并原子写回四处。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.schemas import ActionResult, DispatchPayload, PageResult
from app.services import handover, patrol

router = APIRouter(prefix="/api/patrol", tags=["日常巡查"])

STATUSES = ["待派单", "进行中", "已完成"]


@router.get("/columns")
def list_columns() -> dict[str, Any]:
    """列表列定义由后端给：前端表格和详情抽屉渲染同一份口径，避免两边对不上。"""
    return {
        "columns": [
            {"key": key, "label": patrol.COLUMN_LABELS[key]} for key in patrol.LIST_COLUMNS
        ],
        "statuses": STATUSES,
    }


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按巡查编号检索"),
    status: str | None = Query(default=None, description="待派单、进行中、已完成"),
    crew_id: int | None = Query(default=None, description="按班组过滤"),
    current_shift: bool = Query(default=False, description="只看该班组当前班次任务"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """巡查任务列表：与详情共用序列化函数，字段口径一致。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    if status and status not in STATUSES:
        raise HTTPException(status_code=400, detail=f"任务状态仅支持：{'、'.join(STATUSES)}")
    items, total = patrol.list_tasks(
        keyword=keyword, status=status, crew_id=crew_id,
        current_shift=current_shift, page=page, size=size,
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出巡查任务全量清单。"""
    items, total = patrol.list_tasks(page=1, size=10000)
    return {"module": "patrol", "total": total, "items": items}


@router.get("/{task_id}", response_model=dict)
def get_entry(task_id: int) -> dict[str, Any]:
    """巡查任务详情：列表行的超集（额外带责任链与交接记录），不会再出现对不上。"""
    entry = patrol.get_task(task_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"巡查任务 {task_id} 不存在或已归档")
    return entry


@router.post("/{task_id}/dispatch", response_model=ActionResult)
def dispatch_entry(
    task_id: int,
    payload: DispatchPayload,
    operator: dict[str, Any] = Depends(handover.require_auth),
) -> ActionResult:
    """派单：旧身份令牌在鉴权依赖处直接 401；派单结果原子写回。"""
    entry, message = patrol.dispatch_task(
        operator, task_id,
        {"driver_id": payload.driver_id, "vehicle_id": payload.vehicle_id,
         "issue": payload.issue, "measure": payload.measure},
    )
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
