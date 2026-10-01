"""HTTP 层端到端验证：FastAPI 路由、X-Auth-Token、401/409 状态码、写回结果。"""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
fails = []


def check(name, cond, extra=""):
    print(("PASS" if cond else "FAIL"), name, extra)
    if not cond:
        fails.append(name)


H = {}

# 0. 启动迁移报告
r = client.get('/api/handover/migration')
report = r.json()
check("migration-http", r.status_code == 200 and report['events'] == 4,
      f"splits={len(report['splits'])} deduped={len(report['deduped'])}")

# 1. 派单无令牌 401
r = client.post('/api/patrol/3/dispatch', json={"driver_id": 1, "vehicle_id": 1})
check("dispatch-401", r.status_code == 401 and r.json()['code'] == 'NO_SESSION')

# 2. 登录 CREW-A
r = client.post('/api/handover/auth/login', json={"crew_id": 1, "leader": "周明"})
check("login", r.status_code == 200)
token = r.json()['token']
H['a'] = {'X-Auth-Token': token}
check("login-version", r.json()['version'] == 2)

# 3. 列表/详情口径
r = client.get('/api/patrol?crew_id=1&size=100')
items = r.json()['items']
ok = True
for row in items:
    d = client.get(f"/api/patrol/{row['id']}").json()
    for key in ('task_no', 'section', 'status', 'driver_name', 'plate', 'shift_label', 'signed_at'):
        if row.get(key) != d.get(key):
            ok = False
check("list-detail-http", ok)

# columns 接口
r = client.get('/api/patrol/columns')
check("columns", [c['key'] for c in r.json()['columns']] ==
      ['task_no', 'section', 'plan_date', 'status', 'driver_name', 'plate', 'shift_label', 'signed_at'])

# 4. 派单 T3 成功
r = client.post('/api/patrol/3/dispatch', json={"driver_id": 1, "vehicle_id": 1}, headers=H['a'])
body = r.json()
check("dispatch-ok-http", body['ok'], body.get('message'))
check("dispatch-entry", body['entry']['status'] == '进行中'
      and body['entry']['driver_name'] == '王建国' and body['entry']['plate'] == '沪A·D1001')

# 5. 重复派单同车（无第二条待派单，直接验证活动冲突——伪造不了，测状态拦截）
r = client.post('/api/patrol/3/dispatch', json={"driver_id": 2, "vehicle_id": 2}, headers=H['a'])
check("redispatch-ok-false", r.status_code == 200 and r.json()['ok'] is False)

# 6. 伪造令牌 401
r = client.get('/api/handover/audit', headers={'X-Auth-Token': 'deadbeef'})
check("bogus-token-401", r.status_code == 401 and r.json()['code'] == 'NO_SESSION')

# 7. 交接签字（显式指定合法签字时刻，避免依赖容器时钟）
r = client.post('/api/handover/sign',
                json={"to_leader": "吴岗", "label": "夜班",
                      "signed_at": "2026-10-01T20:00:00", "expected_version": 2},
                headers=H['a'])
sign = r.json()
check("sign-ok", sign['ok'], sign.get('message'))
check("sign-transfers",
      len(sign['entry']['transferred']['patrol_task_ids']) >= 2
      and sign['entry']['transferred']['vehicle_task_ids'])
check("sign-revoked", sign['entry']['revoked_session_count'] >= 1)

# 8. 旧令牌访问各写接口全部 401
for method, path, payload in [
    ('POST', '/api/patrol/3/dispatch', {"driver_id": 1, "vehicle_id": 1}),
    ('GET', '/api/handover/vehicle-tasks', None),
    ('GET', '/api/handover/crew-todos', None),
    ('POST', '/api/handover/sign', {"to_leader": "x"}),
]:
    r = client.request(method, path, json=payload, headers=H['a'])
    check(f"stale-401:{path}", r.status_code == 401 and r.json()['code'] == 'STALE_IDENTITY',
          f"{r.status_code}")

# 9. 新身份登录，旧班组任务现在只显示新班次
r = client.post('/api/handover/auth/login', json={"crew_id": 1, "leader": "吴岗"})
check("new-login", r.status_code == 200 and r.json()['version'] == 3)
H['a2'] = {'X-Auth-Token': r.json()['token']}
r = client.get('/api/handover/vehicle-tasks', headers=H['a2'])
vt = r.json()['items']
new_shift = sign['entry']['new_shift']['id']
check("vt-current-only", all(item['shift_id'] == new_shift for item in vt) and vt,
      f"{[(i['id'], i['shift_id']) for i in vt]}")

# 10. 通知 + 待办
r = client.get('/api/handover/driver-notifications', headers=H['a2'])
notes = r.json()['items']
check("notifications", any(n['type'] == '任务转派' for n in notes)
      and any(n['type'] == '派单' for n in notes))
r = client.get('/api/handover/crew-todos?status=open', headers=H['a2'])
todos = r.json()['items']
check("todos", any(t['kind'] == 'handover_followup' and t['shift_id'] == new_shift for t in todos))

# 11. 审计事件
r = client.get('/api/handover/audit', headers=H['a2'])
actions = {e['action'] for e in r.json()['items']}
check("audit-actions",
      {'handover_signed', 'patrol_task_transfer', 'vehicle_task_transfer',
       'auth_sessions_revoked', 'patrol_dispatch'} <= actions)

# 12. 历史任务保留原签字
d = client.get('/api/patrol/1').json()
check("history-kept", d['shift_id'] == 1 and d['last_handover'] is None
      and d['status'] == '已完成')
d2 = client.get('/api/patrol/2').json()
check("T2-chain", d2['shift_id'] == new_shift and d2['last_handover'] is not None
      and len(d2['responsibility']) == 2)

# 13. 并发交接的安全顺序：两台终端各自登录；第一单提交成功后，
# 第二单在鉴权依赖层就以 STALE_IDENTITY 401 被拦（不会产生第二个当前班次）。
# 真正同时提交时的 409 HANDOVER_CONFLICT 由服务层多线程测试覆盖。
r1 = client.post('/api/handover/auth/login', json={"crew_id": 2, "leader": "马涛"})
r2 = client.post('/api/handover/auth/login', json={"crew_id": 2, "leader": "马涛-备机"})
H['b1'] = {'X-Auth-Token': r1.json()['token']}
H['b2'] = {'X-Auth-Token': r2.json()['token']}
ra = client.post('/api/handover/sign',
                 json={"to_leader": "甲", "expected_version": 1,
                       "signed_at": "2026-10-01T08:00:00"}, headers=H['b1'])
rb = client.post('/api/handover/sign',
                 json={"to_leader": "乙", "expected_version": 1,
                       "signed_at": "2026-10-01T08:00:00"}, headers=H['b2'])
check("sequential-handover", ra.status_code == 200, f"{ra.status_code}")
check("late-handover-401", rb.status_code == 401 and rb.json()['code'] == 'STALE_IDENTITY',
      f"{rb.status_code}")
check("both-revoked",
      client.get('/api/handover/audit', headers=H['b1']).status_code == 401
      and client.get('/api/handover/audit', headers=H['b2']).status_code == 401)

# 13b. 确定性 409：登录拿到最新身份后，用落后的 expected_version 提交（他人已抢先交接）
r = client.post('/api/handover/auth/login', json={"crew_id": 2, "leader": "孙丽"})
H['b3'] = {'X-Auth-Token': r.json()['token']}
stale_version = r.json()['version'] - 1
r = client.post('/api/handover/sign',
                json={"to_leader": "周明", "expected_version": stale_version}, headers=H['b3'])
check("optimistic-409-http", r.status_code == 409 and r.json()['code'] == 'HANDOVER_CONFLICT',
      f"{r.status_code}")
# 409 不得产生半成品：会话仍有效（版本未再推进）
check("409-session-still-valid",
      client.get('/api/handover/auth/me', headers=H['b3']).status_code == 200)

# 13c. 签字时刻早于当前班次生效时间 -> 422 拒绝（防止制造新的重叠）
r = client.post('/api/handover/sign',
                json={"to_leader": "马涛", "signed_at": "2020-01-01T00:00:00"},
                headers=H['b3'])
check("early-sign-rejected",
      r.status_code == 422 and r.json()['code'] == 'SIGNED_AT_BEFORE_SHIFT_START',
      f"{r.status_code}")

# 13d. 转派给不属于本班组的驾驶员 -> 业务失败，版本/班次/会话全部回滚不变
version_before = client.get('/api/handover/auth/me', headers=H['b3']).json()['version']
shifts_before = len(client.get('/api/handover/shifts?crew_id=2').json()['items'])
r = client.post('/api/handover/sign',
                json={"to_leader": "马涛", "driver_id": 999,
                      "signed_at": "2026-10-01T20:00:00"}, headers=H['b3'])
check("bad-driver-rejected", r.status_code == 400 and r.json()['code'] == 'DRIVER_NOT_IN_CREW',
      f"{r.status_code}")
check("atomic-rollback-http",
      client.get('/api/handover/auth/me', headers=H['b3']).json()['version'] == version_before
      and len(client.get('/api/handover/shifts?crew_id=2').json()['items']) == shifts_before)

# CREW-B 只允许一个当前班次
r = client.get('/api/handover/shifts?crew_id=2')
currents = [s for s in r.json()['items'] if s['is_current']]
check("single-current-http", len(currents) == 1)

# 14. CREW-A 班次时间线不再重叠
r = client.get('/api/handover/shifts?crew_id=1')
shifts = r.json()['items']
check("no-overlap",
      all(shifts[i]['end_at'] == shifts[i + 1]['start_at'] for i in range(len(shifts) - 1)),
      str([(s['start_at'], s['end_at']) for s in shifts]))

print()
print("FAILURES:", fails if fails else "NONE")
raise SystemExit(1 if fails else 0)
