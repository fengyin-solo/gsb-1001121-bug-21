"""班组交接领域：班次版本、签字交接、存量重叠迁移、司机通知与班组待办。

口径约定
--------
* 当前班次以「交接签字时刻」为准：新班次从签字那一刻起 current，旧班次同一刻
  收尾，签字之前完成的任务仍挂在旧班次的历史记录上。
* 历史任务按原签字记录保留：已闭环（已完成/已复核）的巡查不转派，保留
  ``signed_shift_id`` 指向的原签字班次。
* 存量重叠班次迁移：同一班组时间窗重叠的两个旧班次，按各自签字时刻裁剪为
  不重叠的两段，在途任务只向「最后一个签字版本」转派一次。
* 交接动作在单个 store 事务里完成：鉴权缓存失效、任务转派、审计事件、司机
  通知、班组待办原子提交。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import HTTPException, status

from app.services.identity import Identity, auth_service
from app.store import store

SHIFT_TABLE = "shifts"
HANDOVER_TABLE = "shift_handovers"
AUDIT_TABLE = "audit_events"
TODO_TABLE = "shift_todos"
NOTIFICATION_TABLE = "driver_notifications"

PATROL_TABLE = "patrol"

# 已闭环的状态：历史任务保留原签字记录，不参与转派。
CLOSED_STATUSES = {"已完成", "已复核"}
OPEN_STATUSES = {"待巡查", "巡查中"}

BOOTSTRAP_FLAG_KEY = "handover_bootstrapped"


class ShiftError(Exception):
    """交接前置条件不满足，调用方转成可读的 4xx。"""


class HandoverService:
    def _now(self) -> datetime:
        return datetime.utcnow()

    def _iso(self, moment: datetime | None = None) -> str:
        return (moment or self._now()).isoformat(timespec="seconds")

    # ------------------------------------------------------------------
    # 班次查询
    # ------------------------------------------------------------------
    def list_shifts(self, crew: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(SHIFT_TABLE)
        if crew:
            rows = [row for row in rows if row.get("crew") == crew]
        return sorted(rows, key=lambda row: int(row["id"]))

    def get_shift(self, shift_id: int) -> dict[str, Any] | None:
        return store.find(SHIFT_TABLE, shift_id)

    def current_shift(self, crew: str) -> dict[str, Any] | None:
        for row in store.rows(SHIFT_TABLE):
            if row.get("crew") == crew and row.get("is_current"):
                return row
        return None

    # ------------------------------------------------------------------
    # 审计 / 待办 / 通知
    # ------------------------------------------------------------------
    def _append_audit(self, event_type: str, payload: dict[str, Any], actor: dict[str, Any] | None) -> None:
        store.rows(AUDIT_TABLE).append({
            "id": store.next_id(AUDIT_TABLE),
            "event": event_type,
            "payload": payload,
            "actor": actor,
            "created_at": self._iso(),
        })

    def _append_todo(
        self,
        *,
        crew: str,
        shift_id: int,
        title: str,
        source: str,
        ref_id: int | None = None,
        assignee: str | None = None,
    ) -> dict[str, Any]:
        todo = {
            "id": store.next_id(TODO_TABLE),
            "crew": crew,
            "shift_id": shift_id,
            "title": title,
            "source": source,
            "ref_id": ref_id,
            "assignee": assignee,
            "status": "待处理",
            "created_at": self._iso(),
            "closed_at": None,
        }
        store.rows(TODO_TABLE).append(todo)
        return todo

    def _notify_driver(
        self,
        *,
        crew: str,
        shift_id: int,
        user_code: str | None,
        driver: str,
        vehicle_plate: str,
        kind: str,
        title: str,
        content: str,
        ref_id: int | None,
    ) -> dict[str, Any]:
        notice = {
            "id": store.next_id(NOTIFICATION_TABLE),
            "crew": crew,
            "shift_id": shift_id,
            "user_code": user_code,
            "driver": driver,
            "vehicle_plate": vehicle_plate,
            "kind": kind,
            "title": title,
            "content": content,
            "ref_id": ref_id,
            "read": False,
            "created_at": self._iso(),
        }
        store.rows(NOTIFICATION_TABLE).append(notice)
        return notice

    def list_todos(self, *, crew: str | None = None, shift_id: int | None = None) -> list[dict[str, Any]]:
        rows = store.rows(TODO_TABLE)
        if crew:
            rows = [row for row in rows if row.get("crew") == crew]
        if shift_id is not None:
            rows = [row for row in rows if row.get("shift_id") == shift_id]
        return sorted(rows, key=lambda row: int(row["id"]))

    def list_notifications(self, *, user_code: str | None = None, crew: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(NOTIFICATION_TABLE)
        if user_code:
            rows = [row for row in rows if row.get("user_code") == user_code]
        if crew:
            rows = [row for row in rows if row.get("crew") == crew]
        return sorted(rows, key=lambda row: int(row["id"]))

    # ------------------------------------------------------------------
    # 巡查任务的转派与快照
    # ------------------------------------------------------------------
    def _transfer_open_tasks(
        self,
        *,
        crew: str,
        from_shift_id: int | None,
        to_shift_id: int,
        driver: str,
        driver_code: str | None,
        vehicle_plate: str,
        reason: str,
    ) -> list[dict[str, Any]]:
        """把在途任务责任转到新班次；已闭环任务保留原签字记录不动。

        每个任务同时记录「转派链」：历史责任可追溯，当前责任只有一个版本。
        """
        transferred: list[dict[str, Any]] = []
        for task in store.rows(PATROL_TABLE):
            if task.get("crew") != crew:
                continue
            if task.get("status") in CLOSED_STATUSES:
                continue
            responsible = task.get("responsible_shift_id")
            if from_shift_id is not None and responsible != from_shift_id:
                continue
            task.setdefault("transfer_chain", []).append({
                "from_shift_id": responsible,
                "to_shift_id": to_shift_id,
                "from_driver": task.get("responsible_driver"),
                "to_driver": driver,
                "reason": reason,
                "at": self._iso(),
            })
            task["responsible_shift_id"] = to_shift_id
            task["responsible_driver"] = driver
            task["responsible_driver_code"] = driver_code
            task["responsible_vehicle_plate"] = vehicle_plate
            transferred.append(task)
        return transferred

    # ------------------------------------------------------------------
    # 交接签字（原子）
    # ------------------------------------------------------------------
    def handover(
        self,
        *,
        identity: Identity,
        crew: str,
        expected_shift_id: int,
        out_foreman: str,
        in_foreman: str,
        out_driver: str | None,
        in_driver: str,
        in_vehicle_plate: str,
        note: str | None,
    ) -> dict[str, Any]:
        # 越权（跨班组/角色不符）优先拦截，避免把别人的交接当参数错误。
        if identity.crew != crew:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="只能对本人所属班组发起交接签字",
            )
        if identity.role not in {"班长", "值班员"}:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="仅当班班长/值班员可以签字交接",
            )
        in_driver_user = self._find_user_by_name(crew, in_driver)
        if in_driver_user is None:
            raise ShiftError(f"接班司机「{in_driver}」不属于班组 {crew}，无法完成交接")
        in_foreman_user = self._find_user_by_name(crew, in_foreman)
        if in_foreman_user is None:
            raise ShiftError(f"接班班长「{in_foreman}」不属于班组 {crew}，无法完成交接")
        if in_foreman_user["角色"] not in {"班长", "值班员"}:
            raise ShiftError(f"接班班长「{in_foreman}」角色为{in_foreman_user['角色']}，无权接班")

        with store.transaction():
            current = self.current_shift(crew)
            if current is None:
                raise ShiftError(f"班组 {crew} 没有当前班次，无可交接的班次")
            if current["id"] != expected_shift_id:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"班次已被其他交接更新：前端版本 {expected_shift_id}，"
                        f"当前版本 {current['id']}，请刷新后重试"
                    ),
                )
            if current["foreman"] not in {identity.name, out_foreman}:
                raise ShiftError(
                    f"交班班长对不上：当前班次签字人是「{current['foreman']}」，"
                    f"申请交的是「{out_foreman}」"
                )

            signed_at = self._now()

            # 1) 旧班次收尾；新班次以签字时刻生效，版本号自增。
            current["is_current"] = False
            current["end_at"] = self._iso(signed_at)
            new_shift = {
                "id": store.next_id(SHIFT_TABLE),
                "crew": crew,
                "version": int(current["version"]) + 1,
                "foreman": in_foreman,
                "foreman_code": in_foreman_user["工号"],
                "driver": in_driver,
                "driver_code": in_driver_user["工号"],
                "vehicle_plate": in_vehicle_plate,
                "started_at": self._iso(signed_at),
                "end_at": None,
                "is_current": True,
                "split_from_id": None,
                "source": "handover",
            }
            store.rows(SHIFT_TABLE).append(new_shift)

            # 2) 在途任务责任转派；闭环任务保留原签字记录。
            transferred = self._transfer_open_tasks(
                crew=crew,
                from_shift_id=current["id"],
                to_shift_id=new_shift["id"],
                driver=in_driver,
                driver_code=in_driver_user["工号"],
                vehicle_plate=in_vehicle_plate,
                reason="班组交接签字转派",
            )

            # 3) 交接结果回写到巡查详情（在途任务带交接回写字样）。
            for task in transferred:
                task["交接备注"] = (
                    f"{self._iso(signed_at)} 已交接：{out_foreman} → {in_foreman}，"
                    f"司机 {in_driver}（{in_vehicle_plate}）接班"
                )

            # 4) 鉴权缓存原子更新：旧班长/旧司机令牌当场失效（按工号比对）。
            revoked_codes = [str(current["foreman_code"])]
            if current.get("driver_code") and current["driver_code"] != in_driver_user["工号"]:
                revoked_codes.append(str(current["driver_code"]))
            revoked = auth_service.revoke_sessions(
                crew=crew, user_codes=revoked_codes, reason="班组交接签字，旧班次身份失效"
            )

            # 5) 司机通知 + 班组待办。
            notice = self._notify_driver(
                crew=crew,
                shift_id=new_shift["id"],
                user_code=in_driver_user["工号"],
                driver=in_driver,
                vehicle_plate=in_vehicle_plate,
                kind="handover",
                title="交接接班通知",
                content=(
                    f"{crew} 已于 {self._iso(signed_at)} 完成交接签字，"
                    f"你作为接班司机承接 {len(transferred)} 条在途巡查任务，车辆 {in_vehicle_plate}"
                ),
                ref_id=None,
            )
            for task in transferred:
                self._append_todo(
                    crew=crew,
                    shift_id=new_shift["id"],
                    title=f"承接在途巡查 {task.get('巡查编号')}（{task.get('巡查路段') or ''}）",
                    source="handover",
                    ref_id=int(task["id"]),
                    assignee=in_driver,
                )
            self._append_todo(
                crew=crew,
                shift_id=new_shift["id"],
                title=f"完成 {crew} 班次交接签字（{out_foreman} → {in_foreman}）",
                source="handover",
                assignee=in_foreman,
            )

            # 6) 审计事件（与上述写入同一事务提交）。
            record = {
                "id": store.next_id(HANDOVER_TABLE),
                "crew": crew,
                "from_shift_id": current["id"],
                "to_shift_id": new_shift["id"],
                "out_foreman": out_foreman,
                "in_foreman": in_foreman,
                "out_driver": current.get("driver"),
                "in_driver": in_driver,
                "in_vehicle_plate": in_vehicle_plate,
                "note": note,
                "signed_at": self._iso(signed_at),
                "transferred_task_ids": [int(task["id"]) for task in transferred],
            }
            store.rows(HANDOVER_TABLE).append(record)
            self._append_audit(
                "shift.handover",
                {
                    **record,
                    "revoked_sessions": revoked,
                    "notification_id": notice["id"],
                },
                actor=identity.as_dict(),
            )

            return {
                "handover": record,
                "current_shift": new_shift,
                "closed_shift_id": current["id"],
                "transferred": [self._task_brief(task) for task in transferred],
                "revoked_sessions": revoked,
                "notification": notice,
            }

    def _find_user_by_name(self, crew: str, name: str) -> dict[str, Any] | None:
        for user in store.rows("users"):
            if user.get("班组") == crew and user.get("姓名") == name and user.get("active", True):
                return user
        return None

    def _task_brief(self, task: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": int(task["id"]),
            "巡查编号": task.get("巡查编号"),
            "status": task.get("status"),
            "responsible_shift_id": task.get("responsible_shift_id"),
            "responsible_driver": task.get("responsible_driver"),
            "responsible_vehicle_plate": task.get("responsible_vehicle_plate"),
        }

    # ------------------------------------------------------------------
    # 车辆任务看板：按当前责任去重，修掉「旧身份重复显示车辆任务」
    # ------------------------------------------------------------------
    def vehicle_tasks(self, *, plate: str | None = None, driver_code: str | None = None) -> list[dict[str, Any]]:
        """一条巡查任务只出现一次，归属于它当前的责任班次/司机/车辆。

        不再按签字快照里出现过的每个司机/班次把同一条任务展开成多行。
        """
        result: list[dict[str, Any]] = []
        for task in store.rows(PATROL_TABLE):
            if not task.get("dispatched"):
                continue
            if plate and task.get("responsible_vehicle_plate") != plate:
                continue
            if driver_code and task.get("responsible_driver_code") != driver_code:
                continue
            result.append({
                "任务id": int(task["id"]),
                "巡查编号": task.get("巡查编号"),
                "巡查路段": task.get("巡查路段"),
                "status": task.get("status"),
                "车辆": task.get("responsible_vehicle_plate"),
                "当班司机": task.get("responsible_driver"),
                "责任班次": task.get("responsible_shift_id"),
                "原签字班次": task.get("signed_shift_id"),
                "派单时间": task.get("dispatched_at"),
                "历史签字保留": task.get("status") in CLOSED_STATUSES,
            })
        return sorted(result, key=lambda row: -int(row["任务id"]))

    # ------------------------------------------------------------------
    # 存量数据引导与重叠班次迁移
    # ------------------------------------------------------------------
    def bootstrap_if_needed(self) -> dict[str, Any] | None:
        """首次启动时补齐人员/班次，并拆分存量重叠班次。幂等。"""
        with store.transaction():
            if store.find("meta", 1) is not None and store.find("meta", 1).get("key") == BOOTSTRAP_FLAG_KEY:
                return None
            report = self._bootstrap()
            store.rows("meta").append({
                "id": store.next_id("meta"),
                "key": BOOTSTRAP_FLAG_KEY,
                "value": self._iso(),
            })
            return report

    def _ensure_user(self, code: str, name: str, crew: str, role: str) -> dict[str, Any]:
        existing = self.find_user(code)
        if existing:
            return existing
        user = {"id": store.next_id("users"), "工号": code, "姓名": name, "班组": crew, "角色": role, "active": True}
        store.rows("users").append(user)
        return user

    def find_user(self, code: str) -> dict[str, Any] | None:
        for user in store.rows("users"):
            if user.get("工号") == code:
                return user
        return None

    def _append_shift(
        self,
        *,
        crew: str,
        version: int,
        foreman: str,
        foreman_code: str,
        driver: str,
        driver_code: str,
        vehicle_plate: str,
        started_at: str,
        end_at: str | None,
        is_current: bool,
        source: str,
        split_from_id: int | None = None,
    ) -> dict[str, Any]:
        shift = {
            "id": store.next_id(SHIFT_TABLE),
            "crew": crew,
            "version": version,
            "foreman": foreman,
            "foreman_code": foreman_code,
            "driver": driver,
            "driver_code": driver_code,
            "vehicle_plate": vehicle_plate,
            "started_at": started_at,
            "end_at": end_at,
            "is_current": is_current,
            "split_from_id": split_from_id,
            "source": source,
        }
        store.rows(SHIFT_TABLE).append(shift)
        return shift

    def _bootstrap(self) -> dict[str, Any]:
        # ---- 人员骨架（两个班组，覆盖班长/司机/值班员）----
        self._ensure_user("P01", "张建国", "巡查一班", "班长")
        self._ensure_user("P02", "李卫东", "巡查一班", "司机")
        self._ensure_user("P03", "王海涛", "巡查一班", "司机")
        self._ensure_user("P04", "赵守夜", "巡查一班", "值班员")
        self._ensure_user("P05", "陈大康", "巡查一班", "班长")
        self._ensure_user("Q01", "孙立群", "巡查二班", "班长")
        self._ensure_user("Q02", "周明亮", "巡查二班", "司机")

        split = self._migrate_overlapping_legacy_shifts()
        self._enrich_patrol_seed()
        self._ensure_current_shift()
        return {"split_shifts": split, "patrol_tasks": len(store.rows(PATROL_TABLE))}

    def _migrate_overlapping_legacy_shifts(self) -> list[dict[str, Any]]:
        """构造并修复存量重叠：巡查一班两个旧班次时间窗重叠。

        旧记录（迁移前）：
          A：09-01 08:00 ~ 09-03 20:00，张建国/李卫东（白班记录，被重复登记）
          B：09-02 20:00 ~ None，王海涛，作为「当前班次」被错误保留
        迁移后按签字时刻裁剪：
          A：09-01 08:00 ~ 09-02 20:00（收尾）
          B：09-02 20:00 起为当前班次（split_from A）；在途任务只归 B。
        """
        if self.current_shift("巡查一班") is not None:
            return []

        zhang = self.find_user("P01")
        li = self.find_user("P02")
        wang = self.find_user("P03")
        cut_a = "2026-09-01T08:00:00"
        cut_b = "2026-09-02T20:00:00"

        shift_a = self._append_shift(
            crew="巡查一班", version=1, foreman=zhang["姓名"], foreman_code=zhang["工号"],
            driver=li["姓名"], driver_code=li["工号"], vehicle_plate="京A·1001",
            started_at=cut_a, end_at=None, is_current=True, source="legacy_overlap",
        )
        shift_b = self._append_shift(
            crew="巡查一班", version=2, foreman=zhang["姓名"], foreman_code=zhang["工号"],
            driver=wang["姓名"], driver_code=wang["工号"], vehicle_plate="京A·1002",
            started_at=cut_b, end_at=None, is_current=True, source="legacy_overlap",
            split_from_id=shift_a["id"],
        )

        # 迁移拆分：A 在 B 签字时刻收尾，重叠段 [09-02 20:00, 09-03 20:00] 归 B。
        shift_a["end_at"] = cut_b
        shift_b["is_current"] = True
        shift_a["is_current"] = False

        self._append_audit(
            "migration.shift_split",
            {
                "crew": "巡查一班",
                "kept_shift_id": shift_a["id"],
                "current_shift_id": shift_b["id"],
                "overlap_window": ["2026-09-02T20:00:00", "2026-09-03T20:00:00"],
                "rule": "当前班次以交接签字时刻为准，重叠段归最后签字版本",
            },
            actor={"name": "系统迁移", "role": "system"},
        )
        self._append_todo(
            crew="巡查一班",
            shift_id=shift_b["id"],
            title="存量重叠班次已迁移拆分（白班/夜班记录重叠）",
            source="migration",
        )
        return [{"kept_shift_id": shift_a["id"], "current_shift_id": shift_b["id"]}]

    def _enrich_patrol_seed(self) -> None:
        """给既有巡查样例补上身份快照与责任字段，演示历史保留与在途转派。"""
        rows = store.rows(PATROL_TABLE)
        current = self.current_shift("巡查一班")
        legacy = self.find_user("P02")
        by_id = {int(row["id"]): row for row in rows}

        snapshots = {
            1: ("待巡查", current, legacy, True),       # 在途：迁移后归当前班次
            2: ("巡查中", current, self.find_user("P03"), True),  # 在途且已派车
            3: ("已完成", self.get_shift(1), legacy, False),      # 历史：保留原签字班次
        }
        for task_id, (status_value, shift, driver_user, dispatched) in snapshots.items():
            task = by_id.get(task_id)
            if task is None or shift is None:
                continue
            task["status"] = status_value
            task["pending"] = status_value not in CLOSED_STATUSES
            task["巡查状态"] = status_value
            task["crew"] = shift["crew"]
            task["signed_shift_id"] = int(shift["id"])
            task["signed_at"] = shift["started_at"]
            task["signed_by"] = shift["foreman"]
            if status_value in CLOSED_STATUSES:
                # 历史任务：责任即原签字记录，不随交接迁移。
                task["responsible_shift_id"] = int(shift["id"])
                task["responsible_driver"] = driver_user["姓名"] if driver_user else shift["driver"]
                task["responsible_driver_code"] = driver_user["工号"] if driver_user else shift["driver_code"]
                task["responsible_vehicle_plate"] = shift["vehicle_plate"]
                task["transfer_chain"] = []
                task["dispatched"] = False
            else:
                # 在途任务：责任指向迁移后的当前班次（最后签字版本）。
                task["responsible_shift_id"] = int(current["id"])
                task["responsible_driver"] = driver_user["姓名"]
                task["responsible_driver_code"] = driver_user["工号"]
                task["responsible_vehicle_plate"] = current["vehicle_plate"]
                task["transfer_chain"] = [{
                    "from_shift_id": 1,
                    "to_shift_id": int(current["id"]),
                    "from_driver": "李卫东",
                    "to_driver": driver_user["姓名"],
                    "reason": "存量重叠班次迁移：在途任务转入签字当前版本",
                    "at": current["started_at"],
                }]
                task["dispatched"] = dispatched
                if dispatched:
                    task["dispatched_at"] = current["started_at"]
                    task["dispatched_by"] = "张建国"

    def _ensure_current_shift(self) -> None:
        sun = self.find_user("Q01")
        zhou = self.find_user("Q02")
        if self.current_shift("巡查二班") is None:
            self._append_shift(
                crew="巡查二班", version=1, foreman=sun["姓名"], foreman_code=sun["工号"],
                driver=zhou["姓名"], driver_code=zhou["工号"], vehicle_plate="京A·2001",
                started_at="2026-09-03T08:00:00", end_at=None, is_current=True,
                source="bootstrap",
            )


handover_service = HandoverService()
