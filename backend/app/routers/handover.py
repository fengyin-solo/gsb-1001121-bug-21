"""班组交接接口：登录鉴权、班次与签字、车辆任务、司机通知、班组待办、审计。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from app.schemas import ActionResult, HandoverPayload, LoginPayload
from app.services import handover
from app.store import store

router = APIRouter(prefix="/api/handover", tags=["班组交接"])

AuthOperator = dict[str, Any]


@router.get("/options")
def handover_options() -> dict[str, Any]:
    """班组、驾驶员、车辆基础数据，供登录/派单/交接表单使用。"""
    return handover.options()


@router.post("/auth/login")
def login(payload: LoginPayload) -> dict[str, Any]:
    """登录固化身份快照（班组版本 + 当前班次），返回后续接口要带的 X-Auth-Token。"""
    return handover.login(payload.crew_id, payload.leader)


@router.get("/auth/me")
def me(operator: AuthOperator = Depends(handover.require_auth)) -> dict[str, Any]:
    """返回当前会话快照；旧身份令牌在此被 401 拦截。"""
    return handover.serialize_session(operator["session"])


@router.get("/shifts")
def list_shifts(
    crew_id: int | None = Query(default=None, description="按班组过滤"),
) -> dict[str, Any]:
    """班次时间线：当前班次以交接签字时刻翻转。"""
    return {"items": handover.list_shifts(crew_id)}


@router.get("/handovers")
def list_handovers(
    crew_id: int | None = Query(default=None, description="按班组过滤"),
) -> dict[str, Any]:
    """历史交接签字记录：历史任务按这些原始签字保留归属。"""
    return {"items": handover.list_handovers(crew_id)}


@router.post("/sign", response_model=ActionResult)
def sign_handover(
    payload: HandoverPayload,
    operator: AuthOperator = Depends(handover.require_auth),
) -> ActionResult:
    """交接签字：版本乐观锁 + 鉴权缓存吊销 + 任务/通知/待办/审计原子提交。"""
    result = handover.sign_handover(
        operator,
        {
            "to_leader": payload.to_leader,
            "label": payload.label,
            "remark": payload.remark,
            "driver_id": payload.driver_id,
            "driver_map": payload.driver_map,
            "expected_version": payload.expected_version,
            "signed_at": payload.signed_at,
        },
    )
    return ActionResult(ok=True, message="交接签字完成，当前班次已按签字时刻切换", entry=result)


@router.get("/vehicle-tasks")
def vehicle_tasks(
    scope: str = Query(default="current", description="current=当前班次，crew=班组全部班次"),
    include_closed: bool = Query(default=False, description="是否包含已关闭/重复任务"),
    operator: AuthOperator = Depends(handover.require_auth),
) -> dict[str, Any]:
    """车辆任务：默认只显示当前班组当前班次的在途任务，旧身份不再重复显示。"""
    items = handover.list_vehicle_tasks(operator, scope=scope, include_closed=include_closed)
    return {"items": items, "total": len(items)}


@router.get("/driver-notifications")
def driver_notifications(
    driver_id: int | None = Query(default=None, description="按驾驶员过滤"),
    operator: AuthOperator = Depends(handover.require_auth),
) -> dict[str, Any]:
    """司机通知：派单与交接转派都会写入。"""
    items = handover.list_notifications(operator, driver_id=driver_id)
    return {"items": items, "total": len(items)}


@router.get("/crew-todos")
def crew_todos(
    status: str | None = Query(default=None, description="open/transferred"),
    operator: AuthOperator = Depends(handover.require_auth),
) -> dict[str, Any]:
    """班组待办：交接后旧待办关闭、新班次生成跟进待办。"""
    items = handover.list_todos(operator, status_value=status)
    return {"items": items, "total": len(items)}


@router.get("/audit")
def audit_events(
    operator: AuthOperator = Depends(handover.require_auth),
) -> dict[str, Any]:
    """审计事件：派单、任务转派、会话吊销、交接签字全部留痕。"""
    items = handover.list_audit(operator)
    return {"items": items, "total": len(items)}


@router.get("/migration")
def migration_report() -> dict[str, Any]:
    """启动迁移报告：存量重叠班次拆分与重复车辆任务关闭明细。"""
    return store.migration_report
