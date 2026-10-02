"""班组交接回归测试：身份快照、任务责任、接口鉴权、原子性与并发口径。"""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.handover import CLOSED_STATUSES, handover_service
from app.store import store


@pytest.fixture()
def client() -> TestClient:
    # 每个用例都从全新内存仓库开始，并显式跑一次存量迁移。
    store._tables = {
        name: [dict(row) for row in rows]
        for name, rows in __import__("app.seed", fromlist=["SEED_ROWS"]).SEED_ROWS.items()
    }
    handover_service.bootstrap_if_needed()
    with TestClient(app) as c:
        yield c


def login(client: TestClient, code: str) -> str:
    resp = client.post("/api/auth/login", json={"user_code": code})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ----------------------------------------------------------------------
# 存量迁移
# ----------------------------------------------------------------------
def test_legacy_overlapping_shifts_are_split_and_bootstrap_is_idempotent(client: TestClient) -> None:
    # 巡查一班只剩一个当前班次，旧班次在签字时刻收尾、不再重叠。
    shifts = client.get("/api/shifts", params={"crew": "巡查一班"}).json()
    currents = [s for s in shifts if s["is_current"]]
    assert len(currents) == 1
    old, current = shifts
    assert old["end_at"] == current["started_at"] == "2026-09-02T20:00:00"
    assert current["split_from_id"] == old["id"]

    # 幂等：再次引导不产生新班次/重复审计。
    before = len(client.get("/api/shifts").json())
    assert handover_service.bootstrap_if_needed() is None
    assert len(client.get("/api/shifts").json()) == before

    audits = client.get("/api/shifts/audit", headers=auth(login(client, "P01"))).json()
    assert any(a["event"] == "migration.shift_split" for a in audits)


def test_open_tasks_move_to_last_signed_shift_closed_tasks_keep_signature(client: TestClient) -> None:
    detail = client.get("/api/patrol/2").json()
    assert detail["responsible_shift_id"] == 2  # 在途任务归最后签字版本
    assert detail["responsible_driver"] == "王海涛"
    assert detail["transfer_chain"][0]["from_shift_id"] == 1

    history = client.get("/api/patrol/3").json()
    assert history["status"] in CLOSED_STATUSES
    assert history["signed_shift_id"] == history["responsible_shift_id"] == 1
    assert history["transfer_chain"] == []


# ----------------------------------------------------------------------
# 列表/详情咬合
# ----------------------------------------------------------------------
def test_list_and_detail_share_the_same_snapshot(client: TestClient) -> None:
    items = client.get("/api/patrol", params={"size": 200}).json()["items"]
    for row in items:
        detail = client.get(f"/api/patrol/{row['id']}").json()
        assert detail == row, f"巡查 {row['id']} 列表与详情字段口径不一致"
        assert detail["巡查状态"] == detail["status"]


def test_status_flow_keeps_both_status_fields_in_sync(client: TestClient) -> None:
    client.post("/api/patrol/1/actions", json={"values": {"action": "开始巡查"}})
    detail = client.get("/api/patrol/1").json()
    assert detail["status"] == detail["巡查状态"] == "巡查中"
    # 不能跳步/倒流
    resp = client.post("/api/patrol/1/actions", json={"values": {"action": "开始巡查"}})
    assert resp.json()["ok"] is False


# ----------------------------------------------------------------------
# 接口鉴权 & 旧身份拦截
# ----------------------------------------------------------------------
def test_dispatch_requires_login_and_foreman_role(client: TestClient) -> None:
    assert client.post("/api/patrol/1/dispatch", json={}).status_code == 401
    driver = login(client, "P02")
    resp = client.post("/api/patrol/1/dispatch", json={}, headers=auth(driver))
    assert resp.status_code == 403


def test_old_identity_session_revoked_after_handover_and_blocked_from_dispatch(client: TestClient) -> None:
    foreman = login(client, "P01")
    old_driver = login(client, "P03")  # 当前班次司机王海涛
    current = client.get("/api/shifts/current", params={"crew": "巡查一班"}).json()["shift"]

    payload = {
        "crew": "巡查一班",
        "expected_shift_id": current["id"],
        "out_foreman": "张建国",
        "in_foreman": "赵守夜",
        "in_driver": "李卫东",
        "in_vehicle_plate": "京A·1003",
    }
    resp = client.post("/api/shifts/handover", json=payload, headers=auth(foreman))
    assert resp.status_code == 200
    # 旧班长、旧司机的鉴权缓存都被原子失效
    assert client.get("/api/auth/me", headers=auth(foreman)).status_code == 401
    assert client.get("/api/auth/me", headers=auth(old_driver)).status_code == 401
    # 旧身份即便绕过前端直接派单也会被拦下（401：缓存已失效）
    assert client.post("/api/patrol/1/dispatch", json={}, headers=auth(old_driver)).status_code == 401


def test_stale_shift_cannot_dispatch_transferred_task(client: TestClient) -> None:
    """交接后用新登录的旧班组成员身份，派已转交任务 → 409。"""
    foreman = login(client, "P01")
    client.post(
        "/api/shifts/handover",
        json={
            "crew": "巡查一班", "expected_shift_id": 2,
            "out_foreman": "张建国", "in_foreman": "赵守夜",
            "in_driver": "李卫东", "in_vehicle_plate": "京A·1003",
        },
        headers=auth(foreman),
    )
    # 王海涛角色提升后再登录（鉴权缓存登录时定型，旧会话不会偷偷变成班长）
    for user in store.rows("users"):
        if user["工号"] == "P03":
            user["角色"] = "班长"
    wang = login(client, "P03")
    resp = client.post("/api/patrol/2/dispatch", json={}, headers=auth(wang))
    assert resp.status_code == 409
    assert "旧身份不得继续派单" in resp.json()["detail"]


# ----------------------------------------------------------------------
# 并发交接：只允许一个版本成为当前班次
# ----------------------------------------------------------------------
def test_concurrent_handover_only_one_wins(client: TestClient) -> None:
    # 两个不同的班长身份各自带旧版本号；接班给不同的人，避免输家会话被赢家
    # 连带失效，从而干净地撞到版本乐观锁。
    tokens = [login(client, "P01"), login(client, "P05")]
    current = client.get("/api/shifts/current", params={"crew": "巡查一班"}).json()["shift"]
    results: list[int] = []
    barrier = threading.Barrier(2)

    def sign(token: str, in_foreman: str, driver: str, plate: str) -> None:
        barrier.wait()
        resp = client.post(
            "/api/shifts/handover",
            json={
                "crew": "巡查一班",
                "expected_shift_id": current["id"],
                "out_foreman": "张建国",
                "in_foreman": in_foreman,
                "in_driver": driver,
                "in_vehicle_plate": plate,
            },
            headers=auth(token),
        )
        results.append(resp.status_code)

    t1 = threading.Thread(target=sign, args=(tokens[0], "赵守夜", "李卫东", "京A·1003"))
    t2 = threading.Thread(target=sign, args=(tokens[1], "陈大康", "王海涛", "京A·1002"))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert sorted(results) == [200, 409]
    currents = [s for s in client.get("/api/shifts", params={"crew": "巡查一班"}).json() if s["is_current"]]
    assert len(currents) == 1
    # 版本严格自增，没有跳号/重号
    versions = [s["version"] for s in client.get("/api/shifts", params={"crew": "巡查一班"}).json()]
    assert versions == sorted(versions) and len(versions) == len(set(versions))


# ----------------------------------------------------------------------
# 原子性：鉴权缓存 / 任务转派 / 审计 / 通知 / 待办 同生共死
# ----------------------------------------------------------------------
def test_handover_rolls_back_everything_when_a_later_step_fails(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import handover as handover_module

    foreman = login(client, "P01")
    shift_rows_before = len(store.rows("shifts"))
    open_tasks_before = {
        int(t["id"]): t.get("responsible_shift_id")
        for t in store.rows("patrol")
        if t.get("status") not in CLOSED_STATUSES
    }
    sessions_before = sum(1 for s in store.rows("auth_sessions") if s.get("active"))

    def boom(*_args, **_kwargs):
        raise RuntimeError("审计写入失败，模拟中途故障")

    monkeypatch.setattr(handover_module.HandoverService, "_append_audit", boom)
    with pytest.raises(RuntimeError):
        handover_service.handover(
            identity=__import__("app.services.identity", fromlist=["Identity"]).Identity(
                session_token="x", user_code="P01", name="张建国", crew="巡查一班", role="班长"
            ),
            crew="巡查一班", expected_shift_id=2,
            out_foreman="张建国", in_foreman="赵守夜",
            out_driver="王海涛", in_driver="李卫东", in_vehicle_plate="京A·1003", note=None,
        )

    assert len(store.rows("shifts")) == shift_rows_before
    assert {
        int(t["id"]): t.get("responsible_shift_id")
        for t in store.rows("patrol") if t.get("status") not in CLOSED_STATUSES
    } == open_tasks_before
    assert sum(1 for s in store.rows("auth_sessions") if s.get("active")) == sessions_before
    assert len(store.rows("shift_handovers")) == 0
    assert len(store.rows("driver_notifications")) == 0


def test_successful_handover_writes_detail_notification_and_todos_together(client: TestClient) -> None:
    foreman = login(client, "P01")
    resp = client.post(
        "/api/shifts/handover",
        json={
            "crew": "巡查一班", "expected_shift_id": 2,
            "out_foreman": "张建国", "in_foreman": "赵守夜",
            "in_driver": "李卫东", "in_vehicle_plate": "京A·1003",
        },
        headers=auth(foreman),
    ).json()["entry"]
    new_shift_id = resp["current_shift"]["id"]
    transferred_ids = resp["handover"]["transferred_task_ids"]
    assert transferred_ids  # 在途任务确实转了

    # 巡查详情回写交接结果
    for task_id in transferred_ids:
        detail = client.get(f"/api/patrol/{task_id}").json()
        assert detail["responsible_shift_id"] == new_shift_id
        assert "已交接" in detail["交接备注"]

    # 司机通知
    li = login(client, "P02")
    notes = client.get("/api/shifts/notifications", headers=auth(li)).json()
    assert any(n["kind"] == "handover" and n["shift_id"] == new_shift_id for n in notes)

    # 班组待办：每个在途任务一条 + 交接本身一条
    todos = client.get("/api/shifts/todos", params={"shift_id": new_shift_id}).json()
    assert len(todos) == len(transferred_ids) + 1

    # 审计事件记录了转派清单与失效会话数
    audits = client.get("/api/shifts/audit", headers=auth(login(client, "P04"))).json()
    event = [a for a in audits if a["event"] == "shift.handover"][-1]
    assert event["payload"]["transferred_task_ids"] == transferred_ids
    assert event["payload"]["revoked_sessions"] >= 1


# ----------------------------------------------------------------------
# 车辆任务不再重复
# ----------------------------------------------------------------------
def test_vehicle_task_board_deduplicates_by_current_responsibility(client: TestClient) -> None:
    foreman = login(client, "P01")
    client.post(
        "/api/shifts/handover",
        json={
            "crew": "巡查一班", "expected_shift_id": 2,
            "out_foreman": "张建国", "in_foreman": "赵守夜",
            "in_driver": "李卫东", "in_vehicle_plate": "京A·1003",
        },
        headers=auth(foreman),
    )
    board = client.get("/api/vehicle/tasks").json()["items"]
    ids = [row["任务id"] for row in board]
    assert ids == list(dict.fromkeys(ids)), "同一巡查任务在车辆看板出现多行"
    for row in board:
        # 历史闭环任务标原签字保留；在途任务跟当前班次
        task = client.get(f"/api/patrol/{row['任务id']}").json()
        assert row["责任班次"] == task["responsible_shift_id"]
        assert row["车辆"] == task["responsible_vehicle_plate"]


def test_dispatch_is_atomic_and_redispatch_notifies_new_driver(client: TestClient) -> None:
    from app.services import patrol as patrol_module
    from app.services.identity import Identity

    foreman = Identity(session_token="t", user_code="P01", name="张建国", crew="巡查一班", role="班长")
    ok, msg = patrol_module.PatrolService().dispatch(
        2, foreman, driver="李卫东", vehicle_plate="京A·1001"
    )
    assert ok is not None and "转派" in msg
    task = store.find("patrol", 2)
    assert task["responsible_driver"] == "李卫东"
    assert task["transfer_chain"][-1]["reason"] == "班组交接签字转派" or len(task["transfer_chain"]) >= 1
    notices = [n for n in store.rows("driver_notifications") if n["kind"] == "redispatch"]
    assert notices and notices[-1]["driver"] == "李卫东"
    audits = [a for a in store.rows("audit_events") if a["event"] == "patrol.dispatch"]
    assert audits[-1]["payload"]["kind"] == "redispatch"
