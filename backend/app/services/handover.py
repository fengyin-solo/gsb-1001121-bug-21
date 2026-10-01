"""班组交接域：身份快照、班次、任务责任转派、鉴权会话缓存、审计事件。

所有写操作都在 store.transaction() 内完成：签字时 鉴权缓存吊销 + 版本推进、
巡查任务转派、车辆任务转派、司机通知、班组待办、审计事件 要么一起生效，
要么整体回滚。并发交接靠 expected_version 做乐观控制，只有一个版本能成为当前班次。
"""
from __future__ import annotations

import secrets
from typing import Any

from fastapi import Header

from app.store import now_iso, store


class HandoverError(Exception):
    """交接域业务异常：code 决定 HTTP 状态码，message 直接展示给前端。"""

    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


# ---------------------------------------------------------------- 读取辅助

def _crew(crew_id: int) -> dict[str, Any]:
    crew = store.find("crews", crew_id)
    if crew is None:
        raise HandoverError("CREW_NOT_FOUND", f"班组 {crew_id} 不存在", 404)
    return crew


def current_shift(crew_id: int) -> dict[str, Any] | None:
    """当前班次只认 is_current 标记——该标记在交接签字时刻原子翻转。"""
    for shift in store.rows("shifts"):
        if int(shift["crew_id"]) == crew_id and shift.get("is_current"):
            return shift
    return None


def latest_handover(crew_id: int) -> dict[str, Any] | None:
    crew_handovers = [
        row for row in store.rows("handovers") if int(row["crew_id"]) == crew_id
    ]
    return max(crew_handovers, key=lambda item: int(item["id"]), default=None)


def current_leader_name(crew_id: int) -> str | None:
    handover = latest_handover(crew_id)
    return handover["to_leader"] if handover else None


def _audit_event_id() -> int:
    return store.next_id("audit_events")


def _append_audit(
    *,
    crew_id: int,
    shift_id: int | None,
    handover_id: int | None,
    actor: str,
    action: str,
    target: str,
    detail: dict[str, Any],
    created_at: str,
) -> None:
    store.rows("audit_events").append({
        "id": _audit_event_id(),
        "crew_id": crew_id,
        "shift_id": shift_id,
        "handover_id": handover_id,
        "actor": actor,
        "action": action,
        "target": target,
        "detail": detail,
        "created_at": created_at,
    })


# ---------------------------------------------------------------- 序列化

def serialize_crew(crew: dict[str, Any]) -> dict[str, Any]:
    shift = current_shift(int(crew["id"]))
    return {
        "id": crew["id"],
        "code": crew["code"],
        "name": crew["name"],
        "version": crew["version"],
        "current_leader": current_leader_name(int(crew["id"])),
        "current_shift": serialize_shift(shift) if shift else None,
    }


def serialize_shift(shift: dict[str, Any]) -> dict[str, Any]:
    handover_id = shift.get("handover_id")
    handover = store.find("handovers", handover_id) if handover_id else None
    crew = store.find("crews", int(shift["crew_id"]))
    return {
        "id": shift["id"],
        "crew_id": shift["crew_id"],
        "crew_name": crew["name"] if crew else None,
        "label": shift["label"],
        "start_at": shift["start_at"],
        "end_at": shift.get("end_at"),
        "signed_at": shift.get("signed_at"),
        "is_current": bool(shift.get("is_current")),
        "source": shift.get("source", "handover"),
        "handover": serialize_handover(handover) if handover else None,
    }


def serialize_handover(handover: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": handover["id"],
        "crew_id": handover["crew_id"],
        "from_shift_id": handover["from_shift_id"],
        "to_shift_id": handover["to_shift_id"],
        "signed_at": handover["signed_at"],
        "from_leader": handover["from_leader"],
        "to_leader": handover["to_leader"],
        "remark": handover.get("remark"),
        "version_after": handover.get("version_after"),
    }


def serialize_session(session: dict[str, Any]) -> dict[str, Any]:
    crew = store.find("crews", int(session["crew_id"]))
    shift = store.find("shifts", int(session["current_shift_id"])) if session.get("current_shift_id") else None
    return {
        "token": session["token"],
        "leader": session["leader"],
        "crew_id": session["crew_id"],
        "crew_code": crew["code"] if crew else None,
        "crew_name": crew["name"] if crew else None,
        "version": session["version"],
        "current_shift_id": session.get("current_shift_id"),
        "shift_label": shift["label"] if shift else None,
        "signed_at": shift.get("signed_at") if shift else None,
        "issued_at": session["issued_at"],
        "revoked": bool(session.get("revoked")),
    }


# ---------------------------------------------------------------- 鉴权

def login(crew_id: int, leader: str) -> dict[str, Any]:
    """登录即固化身份快照：班组、版本号、当前班次一并写入会话缓存。"""
    leader = leader.strip()
    if not leader:
        raise HandoverError("LEADER_REQUIRED", "值班负责人姓名不能为空")
    with store.transaction():
        crew = _crew(crew_id)
        shift = current_shift(crew_id)
        if shift is None:
            raise HandoverError("NO_CURRENT_SHIFT", "该班组缺少当前班次，无法登录", 409)
        token = secrets.token_hex(16)
        session = {
            "id": store.next_id("auth_sessions"),
            "token": token,
            "crew_id": crew_id,
            "leader": leader,
            "version": crew["version"],
            "current_shift_id": shift["id"],
            "issued_at": now_iso(),
            "revoked": False,
        }
        store.rows("auth_sessions").append(session)
        _append_audit(
            crew_id=crew_id, shift_id=shift["id"], handover_id=None,
            actor=leader, action="auth_login", target=f"auth_session:{session['id']}",
            detail={"version": crew["version"]}, created_at=session["issued_at"],
        )
        return serialize_session(session)


def resolve_session(token: str | None) -> dict[str, Any]:
    """把请求头里的令牌解析成操作人；旧身份/已吊销会话一律 401 拦截。"""
    if not token:
        raise HandoverError("NO_SESSION", "缺少 X-Auth-Token，请先登录", 401)
    with store.transaction() as tx:
        session = tx.find_by("auth_sessions", token=token)
        if session is None:
            raise HandoverError("NO_SESSION", "登录态不存在或已失效，请重新登录", 401)
        crew = tx.find("crews", int(session["crew_id"]))
        if crew is None:
            raise HandoverError("CREW_NOT_FOUND", "会话对应班组已不存在", 401)
        stale = bool(session.get("revoked")) or int(session["version"]) != int(crew["version"])
        if stale:
            # 懒标记，保证旧令牌即便绕过批量吊销也无法继续派单
            session["revoked"] = True
            raise HandoverError(
                "STALE_IDENTITY",
                "班组已完成交接，旧身份令牌已失效，请由当前班次重新登录后再操作",
                401,
            )
        shift = current_shift(int(session["crew_id"]))
        return {
            "token": token,
            "leader": session["leader"],
            "crew": crew,
            "shift": shift,
            "session": session,
        }


def require_auth(x_auth_token: str | None = Header(default=None)) -> dict[str, Any]:
    """FastAPI 依赖：保护派单、交接、待办等写接口。"""
    return resolve_session(x_auth_token)


# ---------------------------------------------------------------- 交接签字

def sign_handover(operator: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    to_leader = str(payload.get("to_leader") or "").strip()
    if not to_leader:
        raise HandoverError("TO_LEADER_REQUIRED", "接班负责人必须签字")

    with store.transaction():
        crew = _crew(int(operator["crew"]["id"]))
        old_shift = current_shift(int(crew["id"]))
        if old_shift is None:
            raise HandoverError("NO_CURRENT_SHIFT", "缺少当前班次，无法交接", 409)

        # 乐观并发：同一班组并发交接时，只有版本匹配的一方能提交
        expected_version = payload.get("expected_version")
        if expected_version is not None and int(expected_version) != int(crew["version"]):
            raise HandoverError(
                "HANDOVER_CONFLICT",
                f"并发交接冲突：班组版本已变为 {crew['version']}，请刷新后以最新班次重试",
                409,
            )

        signed_at = str(payload.get("signed_at") or now_iso())
        if old_shift.get("start_at") and signed_at < str(old_shift["start_at"]):
            raise HandoverError(
                "SIGNED_AT_BEFORE_SHIFT_START",
                "签字时刻不能早于当前班次的生效时刻，否则会产生新的重叠班次",
                422,
            )
        label = str(payload.get("label") or ("夜班" if old_shift["label"] == "白班" else "白班"))
        remark = str(payload.get("remark") or "")
        driver_id = payload.get("driver_id")
        driver_map = payload.get("driver_map") or {}

        # 1) 旧班次收口、新班次以签字时刻生效
        old_shift["end_at"] = signed_at
        old_shift["is_current"] = False

        new_shift = {
            "id": store.next_id("shifts"),
            "crew_id": crew["id"],
            "label": label,
            "start_at": signed_at,
            "end_at": None,
            "signed_at": signed_at,
            "handover_id": None,
            "is_current": True,
            "source": "handover",
        }
        store.rows("shifts").append(new_shift)

        handover = {
            "id": store.next_id("handovers"),
            "crew_id": crew["id"],
            "from_shift_id": old_shift["id"],
            "to_shift_id": new_shift["id"],
            "signed_at": signed_at,
            "from_leader": operator["leader"],
            "to_leader": to_leader,
            "remark": remark,
            "version_after": int(crew["version"]) + 1,
        }
        store.rows("handovers").append(handover)
        new_shift["handover_id"] = handover["id"]

        transferred_patrol: list[dict[str, Any]] = []
        transferred_vehicle: list[dict[str, Any]] = []

        # 2) 巡查任务责任转派：历史任务（已完成）保留原签字记录，原样不动
        for task in store.rows("patrol_tasks"):
            if int(task["crew_id"]) != crew["id"] or task["status"] == "已完成":
                continue
            task["shift_id"] = new_shift["id"]
            task["last_handover_id"] = handover["id"]
            if task["status"] == "进行中":
                driver = store.find("drivers", int(task["driver_id"])) if task.get("driver_id") else None
                vehicle = store.find("vehicles", int(task["vehicle_id"])) if task.get("vehicle_id") else None
                task["responsibility"].append({
                    "shift_id": new_shift["id"],
                    "handover_id": handover["id"],
                    "since": signed_at,
                    "driver_name": driver["name"] if driver else None,
                    "plate": vehicle["plate"] if vehicle else None,
                    "note": "班组交接自动转派",
                })
            transferred_patrol.append(task)
            _append_audit(
                crew_id=crew["id"], shift_id=new_shift["id"], handover_id=handover["id"],
                actor=operator["leader"], action="patrol_task_transfer",
                target=f"patrol_task:{task['id']}", created_at=signed_at,
                detail={"task_no": task["task_no"], "from_shift_id": old_shift["id"],
                        "to_shift_id": new_shift["id"], "status": task["status"]},
            )

        # 3) 车辆任务转派（同班组可换驾驶员），并核对驾驶员归属
        def resolve_driver(task_row: dict[str, Any]) -> dict[str, Any] | None:
            target_id = driver_map.get(str(task_row["id"]), driver_id)
            if target_id is None:
                return store.find("drivers", int(task_row["driver_id"])) if task_row.get("driver_id") else None
            driver_row = store.find("drivers", int(target_id))
            if driver_row is None or int(driver_row["crew_id"]) != crew["id"]:
                raise HandoverError(
                    "DRIVER_NOT_IN_CREW",
                    f"驾驶员 {target_id} 不属于班组 {crew['code']}，不能接收转派",
                )
            return driver_row

        # 提交了统一接收驾驶员，即使当前没有在途车辆任务也要先校验，避免错误参数被静默吞掉
        if driver_id is not None:
            target_driver = store.find("drivers", int(driver_id))
            if target_driver is None or int(target_driver["crew_id"]) != crew["id"]:
                raise HandoverError(
                    "DRIVER_NOT_IN_CREW",
                    f"驾驶员 {driver_id} 不属于班组 {crew['code']}，不能接收转派",
                )
        for driver_target in driver_map.values():
            mapped = store.find("drivers", int(driver_target))
            if mapped is None or int(mapped["crew_id"]) != crew["id"]:
                raise HandoverError(
                    "DRIVER_NOT_IN_CREW",
                    f"驾驶员 {driver_target} 不属于班组 {crew['code']}，不能接收转派",
                )

        for task_row in store.rows("vehicle_tasks"):
            if int(task_row["crew_id"]) != crew["id"] or task_row["status"] != "active":
                continue
            new_driver = resolve_driver(task_row)
            task_row["shift_id"] = new_shift["id"]
            if new_driver is not None:
                task_row["driver_id"] = new_driver["id"]
                task_row["driver_name"] = new_driver["name"]
            transferred_vehicle.append(task_row)
            _append_audit(
                crew_id=crew["id"], shift_id=new_shift["id"], handover_id=handover["id"],
                actor=operator["leader"], action="vehicle_task_transfer",
                target=f"vehicle_task:{task_row['id']}", created_at=signed_at,
                detail={"plate": task_row["plate"], "driver_id": task_row["driver_id"],
                        "from_shift_id": old_shift["id"], "to_shift_id": new_shift["id"]},
            )

        # 4) 司机通知：每条转派的在途车辆任务一条，新驾驶员必须知晓
        notify_id = store.next_id("driver_notifications")
        for task_row in transferred_vehicle:
            patrol_task = (
                store.find("patrol_tasks", int(task_row["patrol_task_id"]))
                if task_row.get("patrol_task_id") else None
            )
            task_no = patrol_task["task_no"] if patrol_task else "在途车辆任务"
            store.rows("driver_notifications").append({
                "id": notify_id,
                "crew_id": crew["id"],
                "shift_id": new_shift["id"],
                "driver_id": task_row["driver_id"],
                "driver_name": task_row["driver_name"],
                "type": "任务转派",
                "title": "车辆任务已随班组交接转至当前班次",
                "content": f"{task_no}（{task_row['plate']}）已转至「{label}」，请继续跟进处置",
                "patrol_task_id": patrol_task["id"] if patrol_task else None,
                "created_at": signed_at,
                "read": False,
            })
            notify_id += 1

        # 5) 班组待办：旧待办随签字关闭，新班次生成跟进待办。
        # 必须遍历快照，否则新 append 的 open 待办会再次进循环造成死循环。
        for todo in list(store.rows("crew_todos")):
            if int(todo["crew_id"]) == crew["id"] and todo["status"] == "open":
                todo["status"] = "transferred"
                todo["closed_at"] = signed_at
                new_todo = {
                    "id": store.next_id("crew_todos"),
                    "crew_id": crew["id"],
                    "shift_id": new_shift["id"],
                    "kind": todo["kind"],
                    "title": f"交接转入：{todo['title']}",
                    "content": todo["content"],
                    "patrol_task_id": todo.get("patrol_task_id"),
                    "status": "open",
                    "created_at": signed_at,
                }
                store.rows("crew_todos").append(new_todo)
        if transferred_patrol:
            pending = sum(1 for item in transferred_patrol if item["status"] == "待派单")
            ongoing = sum(1 for item in transferred_patrol if item["status"] == "进行中")
            store.rows("crew_todos").append({
                "id": store.next_id("crew_todos"),
                "crew_id": crew["id"],
                "shift_id": new_shift["id"],
                "kind": "handover_followup",
                "title": f"{label}交接待跟进任务",
                "content": f"交接转入巡查任务 {len(transferred_patrol)} 条：待派单 {pending} 条、进行中 {ongoing} 条",
                "patrol_task_id": None,
                "status": "open",
                "created_at": signed_at,
            })

        # 6) 审计：交接签字 + 批量吊销旧会话
        _append_audit(
            crew_id=crew["id"], shift_id=new_shift["id"], handover_id=handover["id"],
            actor=operator["leader"], action="handover_signed",
            target=f"handover:{handover['id']}", created_at=signed_at,
            detail={
                "from_shift_id": old_shift["id"], "to_shift_id": new_shift["id"],
                "to_leader": to_leader, "patrol_transferred": len(transferred_patrol),
                "vehicle_transferred": len(transferred_vehicle),
            },
        )
        revoked_tokens: list[str] = []
        for session_row in store.rows("auth_sessions"):
            if int(session_row["crew_id"]) == crew["id"] and not session_row.get("revoked"):
                session_row["revoked"] = True
                revoked_tokens.append(session_row["token"])
        _append_audit(
            crew_id=crew["id"], shift_id=new_shift["id"], handover_id=handover["id"],
            actor="system", action="auth_sessions_revoked",
            target=f"crew:{crew['id']}", created_at=signed_at,
            detail={"count": len(revoked_tokens)},
        )

        # 7) 身份快照版本最后推进，与上面所有写入同事务提交
        crew["version"] = int(crew["version"]) + 1

        return {
            "handover": serialize_handover(handover),
            "old_shift": serialize_shift(old_shift),
            "new_shift": serialize_shift(new_shift),
            "crew": serialize_crew(crew),
            "transferred": {
                "patrol_task_ids": [item["id"] for item in transferred_patrol],
                "vehicle_task_ids": [item["id"] for item in transferred_vehicle],
            },
            "revoked_session_count": len(revoked_tokens),
            "signed_at": signed_at,
        }


# ---------------------------------------------------------------- 列表查询

def list_shifts(crew_id: int | None = None) -> list[dict[str, Any]]:
    rows = store.rows("shifts")
    if crew_id is not None:
        rows = [row for row in rows if int(row["crew_id"]) == crew_id]
    rows = sorted(rows, key=lambda item: str(item.get("signed_at") or ""))
    return [serialize_shift(row) for row in rows]


def list_handovers(crew_id: int | None = None) -> list[dict[str, Any]]:
    rows = store.rows("handovers")
    if crew_id is not None:
        rows = [row for row in rows if int(row["crew_id"]) == crew_id]
    rows = sorted(rows, key=lambda item: int(item["id"]))
    return [serialize_handover(row) for row in rows]


def list_vehicle_tasks(
    operator: dict[str, Any], *, scope: str = "current", include_closed: bool = False
) -> list[dict[str, Any]]:
    """车辆任务列表默认只看当前班组当前班次——服务端不再按旧身份重复显示。"""
    crew_id = int(operator["crew"]["id"])
    shift_id = int(operator["shift"]["id"]) if operator.get("shift") else None
    rows = [row for row in store.rows("vehicle_tasks") if int(row["crew_id"]) == crew_id]
    if scope == "current":
        rows = [row for row in rows if int(row["shift_id"]) == shift_id]
    if not include_closed:
        rows = [row for row in rows if row["status"] == "active"]
    rows = sorted(rows, key=lambda item: int(item["id"]))
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["shift"] = serialize_shift(store.find("shifts", int(row["shift_id"])))
        result.append(item)
    return result


def list_notifications(operator: dict[str, Any], *, driver_id: int | None = None) -> list[dict[str, Any]]:
    crew_id = int(operator["crew"]["id"])
    rows = [
        row for row in store.rows("driver_notifications") if int(row["crew_id"]) == crew_id
    ]
    if driver_id is not None:
        rows = [row for row in rows if int(row.get("driver_id") or 0) == driver_id]
    return sorted(rows, key=lambda item: int(item["id"]), reverse=True)


def list_todos(operator: dict[str, Any], *, status_value: str | None = None) -> list[dict[str, Any]]:
    crew_id = int(operator["crew"]["id"])
    rows = [row for row in store.rows("crew_todos") if int(row["crew_id"]) == crew_id]
    if status_value:
        rows = [row for row in rows if row.get("status") == status_value]
    return sorted(rows, key=lambda item: int(item["id"]), reverse=True)


def list_audit(operator: dict[str, Any] | None = None, *, crew_id: int | None = None) -> list[dict[str, Any]]:
    if operator is not None:
        crew_id = int(operator["crew"]["id"])
    rows = store.rows("audit_events")
    if crew_id is not None:
        rows = [row for row in rows if row.get("crew_id") == crew_id]
    return sorted(rows, key=lambda item: int(item["id"]), reverse=True)


def options() -> dict[str, Any]:
    """登录/派单/交接表单需要的班组、班次、驾驶员、车辆基础数据。"""
    return {
        "crews": [serialize_crew(row) for row in store.rows("crews")],
        "drivers": [dict(row) for row in store.rows("drivers")],
        "vehicles": [dict(row) for row in store.rows("vehicles")],
    }
