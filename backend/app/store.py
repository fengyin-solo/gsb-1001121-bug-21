"""内存数据仓库。

除了各业务模块的示例表，还承载班组交接域的全部数据：班组/班次/交接签字、
巡查任务、车辆任务、司机通知、班组待办、审计事件和鉴权会话缓存。

并发口径：FastAPI 的同步接口跑在线程池里，这里用一把可重入锁配合深拷贝快照
实现事务——交接签字要求鉴权缓存、任务转派、审计事件原子更新，任何一步抛错
都会整体回滚，只允许一个版本成为当前班次。
真实项目里这里会换成数据库访问层（事务/乐观锁），当前实现只依赖标准库。
"""
from __future__ import annotations

import copy
import threading
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator

from app.handover_seed import build_domain_tables
from app.seed import SEED_ROWS

# 迁移报告里的时间戳用固定值，保证重启/测试可复现；交接动作本身用真实时钟。
MIGRATED_AT = "2026-10-01T08:00:00"


class Store:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._migration_report: dict[str, Any] = {}
        self.reset()

    # ------------------------------------------------------------------ 基础

    def reset(self) -> dict[str, Any]:
        """重建全部表并执行一次存量迁移；返回本次迁移报告。"""
        with self._lock:
            self._tables: dict[str, list[dict[str, Any]]] = {
                name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
            }
            self._tables.update(build_domain_tables())
            self._migration_report = self._migrate_overlapping_shifts()
            return self._migration_report

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    @contextmanager
    def transaction(self) -> Iterator["Store"]:
        """事务区间：正常提交共享状态，异常时用深拷贝快照整体回滚。"""
        with self._lock:
            snapshot = copy.deepcopy(self._tables)
            try:
                yield self
            except Exception:
                self._tables = snapshot
                raise

    def next_id(self, table: str) -> int:
        return max((int(row.get("id", 0)) for row in self.rows(table)), default=0) + 1

    def module_names(self) -> list[str]:
        return sorted(name for name in self._tables if name in SEED_ROWS)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def find_by(self, table: str, **criteria: Any) -> dict[str, Any] | None:
        for row in self.rows(table):
            if all(row.get(key) == value for key, value in criteria.items()):
                return row
        return None

    @property
    def migration_report(self) -> dict[str, Any]:
        return self._migration_report

    # ------------------------------------------------------------ 存量迁移

    def _migrate_overlapping_shifts(self) -> dict[str, Any]:
        """把存量重叠班次按交接签字时刻拆分，并清理旧身份重复派单。

        - 有交接记录的班次：生效起点取交接签字时刻；被交接走的旧班次，
          结束点也收到交接签字时刻。当前班次只保留签字链上最后一条。
        - 同一车辆存在多条在途任务时，保留最早一条，其余标记为
          duplicate_closed（重复任务不删除，留下审计痕迹）。
        幂等：拆分完成后 raw_overlap 标记移除，重复任务关闭，二次执行不再产生事件。
        """
        report: dict[str, Any] = {"at": MIGRATED_AT, "splits": [], "deduped": [], "events": 0}
        shifts = self.rows("shifts")
        handovers = self.rows("handovers")

        by_crew: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for shift in shifts:
            by_crew[int(shift["crew_id"])].append(shift)

        for crew_id, crew_shifts in by_crew.items():
            crew_shifts.sort(key=lambda item: str(item.get("signed_at") or item.get("start_at")))
            current_id = crew_shifts[-1]["id"]
            for shift in crew_shifts:
                shift["is_current"] = shift["id"] == current_id

            if not any(shift.pop("raw_overlap", False) for shift in crew_shifts):
                continue

            # 接班班次的生效起点 = 交接签字时刻
            for shift in crew_shifts:
                handover_id = shift.get("handover_id")
                if handover_id is None:
                    continue
                handover = next(item for item in handovers if item["id"] == handover_id)
                signed_at = handover["signed_at"]
                if shift["start_at"] != signed_at:
                    report["splits"].append({
                        "shift_id": shift["id"], "crew_id": crew_id,
                        "field": "start_at", "from": shift["start_at"], "to": signed_at,
                        "handover_id": handover_id,
                    })
                    shift["start_at"] = signed_at

            # 交班班次的结束点 = 交接签字时刻
            for handover in handovers:
                if int(handover["crew_id"]) != crew_id:
                    continue
                shift = next(item for item in crew_shifts if item["id"] == handover["from_shift_id"])
                signed_at = handover["signed_at"]
                if shift.get("end_at") != signed_at:
                    report["splits"].append({
                        "shift_id": shift["id"], "crew_id": crew_id,
                        "field": "end_at", "from": shift.get("end_at"), "to": signed_at,
                        "handover_id": handover["id"],
                    })
                    shift["end_at"] = signed_at

        # 旧身份重复派单：同一车辆只允许一条在途任务
        active_by_vehicle: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for task in self.rows("vehicle_tasks"):
            if task.get("status") == "active":
                active_by_vehicle[int(task["vehicle_id"])].append(task)
        for vehicle_id, tasks in active_by_vehicle.items():
            if len(tasks) <= 1:
                continue
            tasks.sort(key=lambda item: str(item.get("issued_at") or ""))
            for duplicate in tasks[1:]:
                duplicate["status"] = "duplicate_closed"
                duplicate["closed_at"] = MIGRATED_AT
                report["deduped"].append({
                    "vehicle_task_id": duplicate["id"], "vehicle_id": vehicle_id,
                    "plate": duplicate.get("plate"), "issued_at": duplicate.get("issued_at"),
                    "kept_vehicle_task_id": tasks[0]["id"],
                })

        if report["splits"] or report["deduped"]:
            report["events"] = self._write_migration_audit(report)
        return report

    def _write_migration_audit(self, report: dict[str, Any]) -> int:
        events = self.rows("audit_events")
        next_id = max((int(row["id"]) for row in events), default=0) + 1
        for split in report["splits"]:
            events.append({
                "id": next_id, "crew_id": split["crew_id"], "shift_id": split["shift_id"],
                "handover_id": split.get("handover_id"), "actor": "system-migration",
                "action": "shift_split_by_handover",
                "target": f"shift:{split['shift_id']}", "created_at": MIGRATED_AT,
                "detail": {"field": split["field"], "from": split["from"], "to": split["to"]},
            })
            next_id += 1
        for item in report["deduped"]:
            events.append({
                "id": next_id, "crew_id": None, "shift_id": None, "handover_id": None,
                "actor": "system-migration", "action": "duplicate_vehicle_task_closed",
                "target": f"vehicle_task:{item['vehicle_task_id']}", "created_at": MIGRATED_AT,
                "detail": {"vehicle_id": item["vehicle_id"], "kept": item["kept_vehicle_task_id"]},
            })
            next_id += 1
        return len(report["splits"]) + len(report["deduped"])

    # ------------------------------------------------------------------ 看板

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in sorted(SEED_ROWS):
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


store = Store()
