# 市政道路桥梁养护管理平台

覆盖道路巡查、桥隧定检、路面病害、交安设施、绿化管养、除雪防汛及养护工程管理的市政道桥全要素养护后台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 路段管理 | `road_section` | 管养路段 | 路段编号、路段名称、起止桩号 |
| 日常巡查 | `patrol` | 巡查记录 | 巡查编号、巡查路段、巡查日期 |
| 路面病害 | `pavement` | 病害记录 | 病害编号、所属路段、病害类型 |
| 桥梁定检 | `bridge` | 检测记录 | 检测编号、桥梁名称、检测类型 |
| 桥梁档案 | `bridge_info` | 桥梁 | 桥梁编号、桥梁名称、桥型结构 |
| 隧道管养 | `tunnel` | 隧道 | 隧道编号、隧道名称、隧道长度 |
| 交安设施 | `traffic_facility` | 交安设施 | 设施编号、设施类型、所属路段 |
| 排水设施 | `drainage` | 排水设施 | 设施编号、设施类型、所属路段 |
| 绿化管养 | `green` | 绿化区域 | 区域编号、区域名称、植物品种 |
| 路灯照明 | `lighting` | 路灯设施 | 灯具编号、灯具类型、功率 |
| 除雪防滑 | `winter` | 除雪作业 | 作业编号、作业路段、作业日期 |
| 防汛应急 | `flood` | 防汛记录 | 记录编号、预警级别、影响路段 |
| 边坡防护 | `slope` | 边坡 | 边坡编号、所属路段、边坡类型 |
| 伸缩缝管理 | `expansion` | 伸缩缝 | 缝编号、所属桥梁、缝类型 |
| 支座维护 | `bearing` | 桥梁支座 | 支座编号、所属桥梁、支座类型 |
| 养护工程 | `project` | 养护工程 | 工程编号、工程名称、工程类型 |
| 养护车辆 | `vehicle` | 养护车辆 | 车辆编号、车辆类型、车牌号 |
| 养护材料 | `material` | 养护材料 | 材料编号、材料名称、材料类别 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 班组交接与巡查派单

围绕班组交接期间的三个咬合故障（列表与详情对不上、派单按钮点不动、旧身份重复
显示车辆任务），新增了交接域：身份快照、任务责任、接口鉴权。

- **当前班次以交接签字时刻为准**：`POST /api/handover/sign` 原子完成旧班次收口、
  新班次按签字时刻生效、未完成巡查/车辆任务转派、司机通知、班组待办结转、
  审计事件写入和旧会话吊销；历史任务（已完成）按原签字记录保留，不再改动。
- **身份快照与鉴权**：`POST /api/handover/auth/login` 固化「班组 + 版本号 + 当前
  班次」并返回 `X-Auth-Token`；派单、交接、待办等写接口强制鉴权，交接后版本推进，
  旧令牌统一返回 `401 STALE_IDENTITY`，旧身份继续派单直接拦截。
- **并发控制**：签字提交带 `expected_version` 乐观锁；同一班组并发交接只有一个
  版本成为当前班次，冲突返回 `409 HANDOVER_CONFLICT`，不产生半成品（事务回滚）。
- **派单原子写回四处**：巡查详情（任务责任链）、车辆任务、司机通知、班组待办，
  外加审计事件；同车重复在途派单在服务端被截断。
- **存量迁移**：启动时自动把重叠班次按原交接签字时刻夹紧拆分、把旧身份重复的
  在途车辆任务关闭留痕，迁移报告见 `GET /api/handover/migration`（幂等）。
- **列表/详情同口径**：`GET /api/patrol` 与 `GET /api/patrol/{id}` 共用同一个
  序列化函数，详情只是列表行的超集（多责任链与交接记录）；列定义由
  `GET /api/patrol/columns` 下发，前端不再各拼各的。

入口页面：侧栏「班组交接」（`/handover`，登录、签字、班次时间线、车辆任务、
通知、待办、审计、迁移报告）与「日常巡查」（`/patrol`，筛选、详情抽屉、派单弹窗）。

后端自检：`python3 test_handover_http.py`（需安装 `httpx`，FastAPI TestClient
覆盖登录/派单/401/409/422/迁移/写回校验）。
