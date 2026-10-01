"""班组交接域的原始种子数据。

这里故意保留了一批"未治理"的存量数据，用来在启动时演示迁移：
- shifts 里存在跨班组的重叠班次（raw_overlap=True），需要按交接签字时刻拆分；
- vehicle_tasks 里存在同一车辆在同一班次的重复在途任务（旧身份重复派单遗留）。

迁移完成后这些标记会被清掉，审计事件里留下拆分/去重记录。
"""
from __future__ import annotations

from typing import Any

# 班组：version 是身份快照版本号，每完成一次交接签字原子 +1，
# 鉴权会话缓存里保存登录时刻的版本，版本落后即判定为旧身份。
CREWS: list[dict[str, Any]] = [
    {"id": 1, "code": "CREW-A", "name": "一路两班养护一组", "version": 2},
    {"id": 2, "code": "CREW-B", "name": "桥隧应急养护二组", "version": 1},
]

DRIVERS: list[dict[str, Any]] = [
    {"id": 1, "crew_id": 1, "name": "王建国", "phone": "13800001001"},
    {"id": 2, "crew_id": 1, "name": "李秀兰", "phone": "13800001002"},
    {"id": 3, "crew_id": 2, "name": "赵强", "phone": "13800002001"},
]

VEHICLES: list[dict[str, Any]] = [
    {"id": 1, "crew_id": 1, "plate": "沪A·D1001", "kind": "日常巡查车"},
    {"id": 2, "crew_id": 1, "plate": "沪A·D1002", "kind": "日常巡查车"},
    {"id": 3, "crew_id": 2, "plate": "沪A·Y2001", "kind": "应急巡查车"},
]

# 注意 CREW-A 的三条班次首尾重叠（19:00<20:00、07:30<08:00），
# 迁移时必须以 handover 签字时刻 20:00 / 次日 08:00 为界夹紧。
SHIFTS: list[dict[str, Any]] = [
    {
        "id": 1, "crew_id": 1, "label": "白班",
        "start_at": "2026-09-30T08:00:00", "end_at": "2026-09-30T20:00:00",
        "signed_at": "2026-09-30T08:00:00",
        "handover_id": None, "is_current": False, "source": "seed", "raw_overlap": True,
    },
    {
        "id": 2, "crew_id": 1, "label": "夜班",
        "start_at": "2026-09-30T19:00:00", "end_at": "2026-10-01T08:00:00",
        "signed_at": "2026-09-30T20:00:00",
        "handover_id": 1, "is_current": False, "source": "seed", "raw_overlap": True,
    },
    {
        "id": 3, "crew_id": 1, "label": "白班",
        "start_at": "2026-10-01T07:30:00", "end_at": None,
        "signed_at": "2026-10-01T08:00:00",
        "handover_id": 2, "is_current": True, "source": "seed", "raw_overlap": True,
    },
    {
        "id": 4, "crew_id": 2, "label": "白班",
        "start_at": "2026-09-30T08:00:00", "end_at": "2026-09-30T20:00:00",
        "signed_at": "2026-09-30T08:00:00",
        "handover_id": None, "is_current": False, "source": "seed", "raw_overlap": True,
    },
    {
        "id": 5, "crew_id": 2, "label": "夜班",
        "start_at": "2026-09-30T19:30:00", "end_at": None,
        "signed_at": "2026-09-30T20:00:00",
        "handover_id": 3, "is_current": True, "source": "seed", "raw_overlap": True,
    },
]

# 历史交接签字记录：迁移与历史任务归属都以它为准，迁移不修改这些记录。
HANDOVERS: list[dict[str, Any]] = [
    {
        "id": 1, "crew_id": 1, "from_shift_id": 1, "to_shift_id": 2,
        "signed_at": "2026-09-30T20:00:00",
        "from_leader": "周明", "to_leader": "吴岗",
        "remark": "白班交夜班，签字为准", "version_after": 1,
    },
    {
        "id": 2, "crew_id": 1, "from_shift_id": 2, "to_shift_id": 3,
        "signed_at": "2026-10-01T08:00:00",
        "from_leader": "吴岗", "to_leader": "周明",
        "remark": "夜班交白班，签字为准", "version_after": 2,
    },
    {
        "id": 3, "crew_id": 2, "from_shift_id": 4, "to_shift_id": 5,
        "signed_at": "2026-09-30T20:00:00",
        "from_leader": "孙丽", "to_leader": "马涛",
        "remark": "应急二班白班交夜班", "version_after": 1,
    },
]

# 巡查任务：T1 已完成（历史任务，交接时保留原签字记录不动）；
# T2 在夜班派出、尚未完成（交接时必须转派到当前班次）；
# T3/T4 待派单，用来演示派单按钮与旧身份拦截。
PATROL_TASKS: list[dict[str, Any]] = [
    {
        "id": 1, "task_no": "XCRW-20260930-001", "crew_id": 1,
        "section": "G204 国道 K12+000 至 K18+300",
        "plan_date": "2026-09-30", "status": "已完成",
        "shift_id": 1, "driver_id": 2, "vehicle_id": 1,
        "assigned_at": "2026-09-30T08:20:00", "assigned_by": "周明",
        "issue": "K15+200 护栏螺栓松动", "measure": "已现场紧固并复核",
        "last_handover_id": None,
        "responsibility": [
            {"shift_id": 1, "handover_id": None, "since": "2026-09-30T08:20:00",
             "driver_name": "李秀兰", "plate": "沪A·D1001", "note": "派单"},
        ],
    },
    {
        "id": 2, "task_no": "XCRW-20260930-002", "crew_id": 1,
        "section": "S32 省道 K3+800 至 K9+500",
        "plan_date": "2026-09-30", "status": "进行中",
        "shift_id": 2, "driver_id": 1, "vehicle_id": 2,
        "assigned_at": "2026-09-30T20:30:00", "assigned_by": "吴岗",
        "issue": "K6+100 路面坑槽", "measure": "待修补",
        "last_handover_id": None,
        "responsibility": [
            {"shift_id": 2, "handover_id": None, "since": "2026-09-30T20:30:00",
             "driver_name": "王建国", "plate": "沪A·D1002", "note": "派单"},
        ],
    },
    {
        "id": 3, "task_no": "XCRW-20261001-001", "crew_id": 1,
        "section": "G204 国道 K18+300 至 K24+000",
        "plan_date": "2026-10-01", "status": "待派单",
        "shift_id": 3, "driver_id": None, "vehicle_id": None,
        "assigned_at": None, "assigned_by": None,
        "issue": "", "measure": "",
        "last_handover_id": None, "responsibility": [],
    },
    {
        "id": 4, "task_no": "XCRW-20261001-002", "crew_id": 2,
        "section": "X108 县道 K0+000 至 K7+200",
        "plan_date": "2026-10-01", "status": "待派单",
        "shift_id": 5, "driver_id": None, "vehicle_id": None,
        "assigned_at": None, "assigned_by": None,
        "issue": "", "measure": "",
        "last_handover_id": None, "responsibility": [],
    },
]

# 车辆任务：VT2 是旧身份在夜班重复派单遗留的脏数据（同车两条在途），
# 迁移时保留较早一条、把重复条目标记为 duplicate_closed。
VEHICLE_TASKS: list[dict[str, Any]] = [
    {
        "id": 1, "patrol_task_id": 1, "crew_id": 1, "shift_id": 1,
        "vehicle_id": 1, "plate": "沪A·D1001", "driver_id": 2, "driver_name": "李秀兰",
        "status": "closed", "issued_at": "2026-09-30T08:20:00", "closed_at": "2026-09-30T17:40:00",
        "duplicate": False,
    },
    {
        "id": 2, "patrol_task_id": 2, "crew_id": 1, "shift_id": 2,
        "vehicle_id": 2, "plate": "沪A·D1002", "driver_id": 1, "driver_name": "王建国",
        "status": "active", "issued_at": "2026-09-30T20:30:00", "closed_at": None,
        "duplicate": False,
    },
    {
        "id": 3, "patrol_task_id": None, "crew_id": 1, "shift_id": 2,
        "vehicle_id": 2, "plate": "沪A·D1002", "driver_id": 1, "driver_name": "王建国",
        "status": "active", "issued_at": "2026-09-30T20:45:00", "closed_at": None,
        "duplicate": True,
    },
]

DRIVER_NOTIFICATIONS: list[dict[str, Any]] = [
    {
        "id": 1, "crew_id": 1, "shift_id": 2, "driver_id": 1, "driver_name": "王建国",
        "type": "派单", "title": "巡查任务已派出",
        "content": "XCRW-20260930-002（S32 省道 K3+800 至 K9+500）已派给你，车辆 沪A·D1002",
        "patrol_task_id": 2, "created_at": "2026-09-30T20:30:00", "read": True,
    },
]

CREW_TODOS: list[dict[str, Any]] = [
    {
        "id": 1, "crew_id": 1, "shift_id": 2,
        "kind": "patrol_dispatch", "title": "跟进在途巡查任务",
        "content": "XCRW-20260930-002 路面坑槽处置尚未完成",
        "patrol_task_id": 2, "status": "open",
        "created_at": "2026-09-30T20:30:00",
    },
]

AUDIT_EVENTS: list[dict[str, Any]] = [
    {
        "id": 1, "crew_id": 1, "shift_id": 1, "handover_id": None,
        "actor": "周明", "action": "patrol_dispatch",
        "target": "patrol_task:1", "created_at": "2026-09-30T08:20:00",
        "detail": {"task_no": "XCRW-20260930-001", "vehicle_id": 1, "driver_id": 2},
    },
    {
        "id": 2, "crew_id": 1, "shift_id": 2, "handover_id": None,
        "actor": "吴岗", "action": "patrol_dispatch",
        "target": "patrol_task:2", "created_at": "2026-09-30T20:30:00",
        "detail": {"task_no": "XCRW-20260930-002", "vehicle_id": 2, "driver_id": 1},
    },
]

# 鉴权会话缓存：登录时写入身份快照（班组 / 版本 / 当前班次），交接签字后整批吊销。
AUTH_SESSIONS: list[dict[str, Any]] = []


def build_domain_tables() -> dict[str, list[dict[str, Any]]]:
    """构造一份全新的交接域表（深拷贝种子，reset/测试会重复调用）。"""
    return {
        "crews": [dict(row) for row in CREWS],
        "drivers": [dict(row) for row in DRIVERS],
        "vehicles": [dict(row) for row in VEHICLES],
        "shifts": [dict(row) for row in SHIFTS],
        "handovers": [dict(row) for row in HANDOVERS],
        "patrol_tasks": [dict(row) for row in PATROL_TASKS],
        "vehicle_tasks": [dict(row) for row in VEHICLE_TASKS],
        "driver_notifications": [dict(row) for row in DRIVER_NOTIFICATIONS],
        "crew_todos": [dict(row) for row in CREW_TODOS],
        "audit_events": [dict(row) for row in AUDIT_EVENTS],
        "auth_sessions": [dict(row) for row in AUTH_SESSIONS],
    }
