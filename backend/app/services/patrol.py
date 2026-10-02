"""日常巡查业务规则：状态流转、字段校验、筛选口径与派单都收在这里。

列表与详情统一走 :meth:`PatrolService.snapshot`，保证两条接口字段口径一致；
``巡查状态`` 与内部 ``status`` 在每次流转时同步，不再出现列表显示一个状态、
详情又是另一个状态的情况。
"""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status as http_status

from app.services.handover import (
    CLOSED_STATUSES,
    handover_service,
)
from app.services.identity import Identity
from app.store import store

MODULE = "patrol"
REQUIRED_FIELDS = ["巡查编号", "巡查路段", "巡查日期"]
STATUS_ORDER = ["待巡查", "巡查中", "已完成", "已复核"]
ACTION_RULES = {"开始巡查": "巡查中", "完成巡查": "已完成", "复核确认": "已复核"}
NEGATIVE_ACTIONS = []

# 列表/详情共同对外的字段：两处口径必须一致。
SNAPSHOT_FIELDS = [
    "id", "巡查编号", "巡查路段", "巡查日期", "巡查人员", "巡查车辆",
    "发现问题", "处置措施", "巡查状态", "status", "pending", "abnormal",
    "crew", "signed_shift_id", "signed_at", "signed_by",
    "responsible_shift_id", "responsible_driver", "responsible_vehicle_plate",
    "dispatched", "dispatched_at", "dispatched_by", "交接备注", "transfer_chain",
]


class PatrolService:
    # ---- 读：列表与详情同一口径 ---------------------------------------
    def snapshot(self, row: dict[str, Any]) -> dict[str, Any]:
        # 巡查状态 与 status 永远同源，避免列表/详情各显示各的。
        row["巡查状态"] = row.get("status")
        return {field: row.get(field) for field in SNAPSHOT_FIELDS}

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status_value: str | None = None,
        page: int = 1,
        size: int = 20,
        crew: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("巡查编号", ""))]
        if status_value:
            rows = [row for row in rows if row.get("status") == status_value]
        if crew:
            rows = [row for row in rows if row.get("crew") == crew]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [self.snapshot(dict(row)) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = store.find(MODULE, entry_id)
        return self.snapshot(dict(row)) if row else None

    # ---- 写：登记 / 状态流转 ------------------------------------------
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": store.next_id(MODULE)}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        for optional in ("巡查人员", "巡查车辆", "发现问题", "处置措施"):
            entry[optional] = values.get(optional)
        entry["status"] = STATUS_ORDER[0]
        entry["巡查状态"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return self.snapshot(dict(entry)), []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"巡查记录 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于日常巡查可执行范围"
        target = ACTION_RULES[action]
        current_index = STATUS_ORDER.index(entry["status"]) if entry.get("status") in STATUS_ORDER else -1
        target_index = STATUS_ORDER.index(target)
        if target_index <= current_index:
            return None, f"巡查记录已处于「{entry['status']}」，不能再执行「{action}」"
        with store.transaction():
            entry["status"] = target
            entry["巡查状态"] = target
            entry["pending"] = target != STATUS_ORDER[-1]
            entry["abnormal"] = action in NEGATIVE_ACTIONS
            result = dict(entry)
        return self.snapshot(result), f"巡查记录已{action}"

    # ---- 派单：当前班次身份 + 原子更新 --------------------------------
    def dispatch(
        self,
        entry_id: int,
        identity: Identity,
        *,
        driver: str | None = None,
        vehicle_plate: str | None = None,
        note: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        if identity.role not in {"班长", "值班员"}:
            raise HTTPException(
                status_code=http_status.HTTP_403_FORBIDDEN,
                detail="只有当班班长/值班员可以派单",
            )
        with store.transaction():
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"巡查记录 {entry_id} 不存在或已归档"
            if entry.get("crew") and entry["crew"] != identity.crew:
                raise HTTPException(
                    status_code=http_status.HTTP_403_FORBIDDEN,
                    detail=f"巡查 {entry_id} 属于 {entry['crew']}，{identity.crew} 无权派单",
                )
            if entry.get("status") in CLOSED_STATUSES:
                return None, "任务已闭环，不能再派单（历史记录按原签字保留）"

            current = handover_service.current_shift(identity.crew)
            if current is None:
                return None, f"{identity.crew} 尚无当前班次，无法派单"
            # 身份必须仍在当前班次名单上；已交班的人即使重新登录也不能再派单。
            on_duty = {
                current.get("foreman_code"),
                current.get("driver_code"),
            }
            if identity.user_code not in on_duty and identity.role != "值班员":
                raise HTTPException(
                    status_code=http_status.HTTP_409_CONFLICT,
                    detail=(
                        f"{identity.name} 已不在 {identity.crew} 当前班次"
                        f"（班次 {current['id']}），旧身份不得继续派单"
                    ),
                )
            if entry.get("responsible_shift_id") and int(entry["responsible_shift_id"]) != int(current["id"]):
                # 旧班次身份（即使令牌还在）不能再给已转交的任务派单。
                raise HTTPException(
                    status_code=http_status.HTTP_409_CONFLICT,
                    detail=(
                        f"巡查 {entry_id} 的责任已转交给班次 {entry['responsible_shift_id']}，"
                        f"当前班次为 {current['id']}，旧班次身份不得继续派单"
                    ),
                )

            driver_name = (driver or current["driver"]).strip()
            plate = (vehicle_plate or current["vehicle_plate"]).strip()
            driver_user = handover_service._find_user_by_name(identity.crew, driver_name)
            if driver_user is None:
                return None, f"司机「{driver_name}」不属于 {identity.crew}"

            before_driver = entry.get("responsible_driver")
            entry["crew"] = identity.crew
            entry["responsible_shift_id"] = int(current["id"])
            entry["responsible_driver"] = driver_name
            entry["responsible_driver_code"] = driver_user["工号"]
            entry["responsible_vehicle_plate"] = plate
            entry["dispatched"] = True
            entry["dispatched_at"] = handover_service._iso()
            entry["dispatched_by"] = identity.name
            entry.setdefault("signed_shift_id", int(current["id"]))
            entry.setdefault("signed_at", current["started_at"])
            entry.setdefault("signed_by", current["foreman"])

            kind = "redispatch" if before_driver and before_driver != driver_name else "dispatch"
            title = "巡查任务转派通知" if kind == "redispatch" else "巡查派单通知"
            notice = handover_service._notify_driver(
                crew=identity.crew,
                shift_id=int(current["id"]),
                user_code=driver_user["工号"],
                driver=driver_name,
                vehicle_plate=plate,
                kind=kind,
                title=title,
                content=(
                    f"巡查 {entry.get('巡查编号')}（{entry.get('巡查路段') or ''}）已派给你，"
                    f"车辆 {plate}，派单人 {identity.name}"
                    + (f"，原司机 {before_driver}" if kind == "redispatch" else "")
                ),
                ref_id=int(entry["id"]),
            )
            handover_service._append_todo(
                crew=identity.crew,
                shift_id=int(current["id"]),
                title=f"{'转派' if kind == 'redispatch' else '派单'}巡查 {entry.get('巡查编号')} → {driver_name}",
                source=kind,
                ref_id=int(entry["id"]),
                assignee=driver_name,
            )
            handover_service._append_audit(
                "patrol.dispatch",
                {
                    "task_id": int(entry["id"]),
                    "shift_id": int(current["id"]),
                    "driver": driver_name,
                    "vehicle_plate": plate,
                    "kind": kind,
                    "notification_id": notice["id"],
                    "note": note,
                },
                actor=identity.as_dict(),
            )
            return self.snapshot(dict(entry)), (
                f"巡查 {entry.get('巡查编号')} 已转派给 {driver_name}" if kind == "redispatch"
                else f"巡查 {entry.get('巡查编号')} 已派单给 {driver_name}"
            )
