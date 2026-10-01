"""巡查任务业务规则：列表/详情统一序列化口径，派单走鉴权并原子写回。

派单一次会同时落到四处：巡查详情（任务责任快照）、车辆任务、司机通知、
班组待办，外加一条审计事件——任何一步失败整体回滚。
"""
from __future__ import annotations

from typing import Any

from app.services import handover
from app.store import now_iso, store

MODULE_TABLE = "patrol_tasks"
STATUS_ORDER = ["待派单", "进行中", "已完成"]
ACTION_TARGETS = {"开始巡查": "进行中", "完成巡查": "已完成", "复核确认": "已完成"}

# 列表与详情共用这一份字段口径，杜绝两边各拼各的导致"对不上"
LIST_COLUMNS = ["task_no", "section", "plan_date", "status", "driver_name", "plate", "shift_label", "signed_at"]
COLUMN_LABELS = {
    "task_no": "巡查编号",
    "section": "巡查路段",
    "plan_date": "巡查日期",
    "status": "任务状态",
    "driver_name": "责任驾驶员",
    "plate": "巡查车辆",
    "shift_label": "当前班次",
    "signed_at": "班次签字时刻",
}


def serialize_task(task: dict[str, Any], *, include_history: bool = False) -> dict[str, Any]:
    """列表与详情唯一出口：详情只是在列表行的基础上多带责任链与交接信息。"""
    driver = store.find("drivers", int(task["driver_id"])) if task.get("driver_id") else None
    vehicle = store.find("vehicles", int(task["vehicle_id"])) if task.get("vehicle_id") else None
    shift = store.find("shifts", int(task["shift_id"])) if task.get("shift_id") else None
    handover_row = (
        store.find("handovers", int(task["last_handover_id"]))
        if task.get("last_handover_id") else None
    )
    crew = store.find("crews", int(task["crew_id"])) if task.get("crew_id") else None
    row = {
        "id": task["id"],
        "task_no": task["task_no"],
        "section": task.get("section"),
        "plan_date": task.get("plan_date"),
        "status": task.get("status"),
        "crew_id": task.get("crew_id"),
        "crew_name": crew["name"] if crew else None,
        "driver_id": task.get("driver_id"),
        "driver_name": driver["name"] if driver else None,
        "vehicle_id": task.get("vehicle_id"),
        "plate": vehicle["plate"] if vehicle else None,
        "shift_id": task.get("shift_id"),
        "shift_label": shift["label"] if shift else None,
        "shift_start_at": shift["start_at"] if shift else None,
        "signed_at": shift.get("signed_at") if shift else None,
        "assigned_at": task.get("assigned_at"),
        "assigned_by": task.get("assigned_by"),
        "issue": task.get("issue"),
        "measure": task.get("measure"),
        "last_handover": handover.serialize_handover(handover_row) if handover_row else None,
        "pending": task.get("status") != "已完成",
        "abnormal": task.get("status") == "进行中",
    }
    if include_history:
        row["responsibility"] = list(task.get("responsibility", []))
    return row


def list_tasks(
    *,
    keyword: str | None = None,
    status: str | None = None,
    crew_id: int | None = None,
    current_shift: bool = False,
    page: int = 1,
    size: int = 20,
) -> tuple[list[dict[str, Any]], int]:
    rows = store.rows(MODULE_TABLE)
    if crew_id is not None:
        rows = [row for row in rows if int(row["crew_id"]) == crew_id]
    if current_shift and crew_id is not None:
        shift = handover.current_shift(crew_id)
        rows = [row for row in rows if int(row.get("shift_id") or 0) == int(shift["id"])] if shift else []
    if keyword:
        rows = [row for row in rows if keyword in str(row.get("task_no", ""))]
    if status:
        rows = [row for row in rows if row.get("status") == status]
    total = len(rows)
    start = max(page - 1, 0) * size
    page_rows = sorted(rows, key=lambda item: int(item["id"]))[start:start + size]
    return [serialize_task(row) for row in page_rows], total


def get_task(task_id: int) -> dict[str, Any] | None:
    task = store.find(MODULE_TABLE, task_id)
    if task is None:
        return None
    return serialize_task(task, include_history=True)


def dispatch_task(operator: dict[str, Any], task_id: int, values: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """派单：鉴权（旧身份拦截在依赖层）→ 责任校验 → 四处原子写回。"""
    driver_id_raw = values.get("driver_id")
    vehicle_id_raw = values.get("vehicle_id")
    issue = str(values.get("issue") or "").strip()
    measure = str(values.get("measure") or "").strip()
    if driver_id_raw in (None, "") or vehicle_id_raw in (None, ""):
        return None, "派单必须指定驾驶员与巡查车辆"

    with store.transaction():
        task = store.find(MODULE_TABLE, task_id)
        if task is None:
            return None, f"巡查任务 {task_id} 不存在或已归档"
        crew_id = int(operator["crew"]["id"])
        if int(task["crew_id"]) != crew_id:
            return None, "该巡查任务不属于当前班组，不能派单"
        if task["status"] != "待派单":
            return None, f"任务当前状态为「{task['status']}」，不能重复派单"
        current = handover.current_shift(crew_id)
        if current is None:
            return None, "当前班组没有生效班次，不能派单"
        if int(task.get("shift_id") or 0) != int(current["id"]):
            return None, "任务不属于当前班次，需先完成交接转派后再派单"

        driver = store.find("drivers", int(driver_id_raw))
        vehicle = store.find("vehicles", int(vehicle_id_raw))
        if driver is None or int(driver["crew_id"]) != crew_id:
            return None, "所选驾驶员不属于当前班组"
        if vehicle is None or int(vehicle["crew_id"]) != crew_id:
            return None, "所选车辆不属于当前班组"

        # 服务端兜底：同一车辆只允许一条在途任务，旧身份重复派单在此截断
        for vehicle_task in store.rows("vehicle_tasks"):
            if (
                int(vehicle_task["vehicle_id"]) == int(vehicle["id"])
                and vehicle_task["status"] == "active"
            ):
                return None, f"车辆 {vehicle['plate']} 已有在途任务，不能重复派单"

        dispatched_at = now_iso()
        task.update({
            "status": "进行中",
            "shift_id": current["id"],
            "driver_id": driver["id"],
            "vehicle_id": vehicle["id"],
            "assigned_at": dispatched_at,
            "assigned_by": operator["leader"],
            "issue": issue or task.get("issue") or "",
            "measure": measure,
        })
        task["responsibility"].append({
            "shift_id": current["id"],
            "handover_id": None,
            "since": dispatched_at,
            "driver_name": driver["name"],
            "plate": vehicle["plate"],
            "note": "派单",
        })

        vehicle_task = {
            "id": store.next_id("vehicle_tasks"),
            "patrol_task_id": task["id"],
            "crew_id": crew_id,
            "shift_id": current["id"],
            "vehicle_id": vehicle["id"],
            "plate": vehicle["plate"],
            "driver_id": driver["id"],
            "driver_name": driver["name"],
            "status": "active",
            "issued_at": dispatched_at,
            "closed_at": None,
            "duplicate": False,
        }
        store.rows("vehicle_tasks").append(vehicle_task)

        store.rows("driver_notifications").append({
            "id": store.next_id("driver_notifications"),
            "crew_id": crew_id,
            "shift_id": current["id"],
            "driver_id": driver["id"],
            "driver_name": driver["name"],
            "type": "派单",
            "title": "巡查任务已派出",
            "content": f"{task['task_no']}（{task['section']}）已派给你，车辆 {vehicle['plate']}",
            "patrol_task_id": task["id"],
            "created_at": dispatched_at,
            "read": False,
        })

        store.rows("crew_todos").append({
            "id": store.next_id("crew_todos"),
            "crew_id": crew_id,
            "shift_id": current["id"],
            "kind": "patrol_dispatch",
            "title": "跟进在途巡查任务",
            "content": f"{task['task_no']} {task['section']} 已派给 {driver['name']}，待处置闭环",
            "patrol_task_id": task["id"],
            "status": "open",
            "created_at": dispatched_at,
        })

        handover._append_audit(
            crew_id=crew_id, shift_id=current["id"], handover_id=None,
            actor=operator["leader"], action="patrol_dispatch",
            target=f"patrol_task:{task['id']}", created_at=dispatched_at,
            detail={"task_no": task["task_no"], "vehicle_id": vehicle["id"],
                    "driver_id": driver["id"], "shift_id": current["id"],
                    "identity_version": operator["crew"]["version"]},
        )
        return serialize_task(task, include_history=True), "派单成功：巡查详情、司机通知、班组待办已同步"
