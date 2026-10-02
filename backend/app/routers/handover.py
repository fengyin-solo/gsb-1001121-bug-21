"""班组交接接口：当前班次、签字交接、待办、司机通知、审计轨迹。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from app.schemas import ActionResult, HandoverPayload
from app.services.handover import ShiftError, handover_service
from app.services.identity import Identity, require_identity

router = APIRouter(prefix="/api/shifts", tags=["班组交接"])


@router.get("/current")
def current_shift(crew: str = Query(..., description="班组名称")) -> dict[str, object]:
    """读取班组当前班次（交接签字后立即指向新版本）。"""
    shift = handover_service.current_shift(crew)
    return {"crew": crew, "shift": shift}


@router.get("")
def list_shifts(crew: str | None = Query(default=None)) -> list[dict]:
    """班次版本序列；历史班次 end_at 即原签字/收尾时刻。"""
    return handover_service.list_shifts(crew)


@router.post("/handover", response_model=ActionResult)
def handover(payload: HandoverPayload, identity: Identity = Depends(require_identity)) -> ActionResult:
    """签字交接：并发时只有一个版本成为当前班次（expected_shift_id 乐观锁）。"""
    try:
        result = handover_service.handover(
            identity=identity,
            crew=payload.crew,
            expected_shift_id=payload.expected_shift_id,
            out_foreman=payload.out_foreman,
            in_foreman=payload.in_foreman,
            out_driver=payload.out_driver,
            in_driver=payload.in_driver,
            in_vehicle_plate=payload.in_vehicle_plate,
            note=payload.note,
        )
    except ShiftError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ActionResult(ok=True, message="交接签字完成，在途任务已转派", entry=result)


@router.get("/todos")
def list_todos(
    crew: str | None = Query(default=None),
    shift_id: int | None = Query(default=None),
) -> list[dict]:
    """班组待办：迁移、派单、交接产生的待处理项。"""
    return handover_service.list_todos(crew=crew, shift_id=shift_id)


@router.get("/notifications")
def list_notifications(
    identity: Identity = Depends(require_identity),
    crew: str | None = Query(default=None),
) -> list[dict]:
    """司机通知：默认按当前登录人过滤，也可按班组查看。"""
    return handover_service.list_notifications(user_code=identity.user_code, crew=crew)


@router.get("/audit")
def list_audit(identity: Identity = Depends(require_identity)) -> list[dict]:
    """审计事件：交接签字、派单转派、迁移拆分都在此留痕。"""
    from app.store import store

    rows = store.rows("audit_events")
    if identity.role not in {"班长", "值班员"}:
        rows = [row for row in rows if row.get("actor", {}).get("user_code") == identity.user_code]
    return sorted(rows, key=lambda row: int(row["id"]))
