<template>
  <section class="page" data-module="handover">
    <header class="page-head">
      <div>
        <h2>班组交接中心</h2>
        <p class="page-desc">
          当前班次以交接签字时刻为准；签字后旧身份令牌立即吊销，在途任务原子转派，
          结果回写巡查详情、司机通知与班组待办。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" @click="refreshAll">刷新</button>
        <button v-if="session.loggedIn" class="btn ghost" type="button" @click="session.logout()">
          退出登录
        </button>
      </div>
    </header>

    <!-- 登录：固化身份快照（班组版本 + 当前班次） -->
    <div v-if="!session.loggedIn" class="panel">
      <h3>值班登录</h3>
      <form class="filter-bar" @submit.prevent="submitLogin">
        <label class="filter-item">
          <span>班组</span>
          <select v-model.number="loginForm.crew_id">
            <option v-for="crew in session.crews" :key="crew.id" :value="crew.id">
              {{ crew.name }}（{{ crew.code }}）
            </option>
          </select>
        </label>
        <label class="filter-item">
          <span>当前班次值班负责人签字</span>
          <input v-model="loginForm.leader" placeholder="输入姓名后登录" />
        </label>
        <button class="btn primary" type="submit" :disabled="logging">{{ logging ? '登录中…' : '登录' }}</button>
      </form>
      <p v-if="loginError" class="error-text">{{ loginError }}</p>
      <p v-if="session.staleMessage" class="stale-text">{{ session.staleMessage }}</p>
      <div class="current-shifts">
        <div v-for="crew in session.crews" :key="crew.id" class="shift-chip">
          <strong>{{ crew.name }}</strong>
          <template v-if="crew.current_shift">
            {{ crew.current_shift.label }} · 签字 {{ crew.current_shift.signed_at }} · v{{ crew.version }}
            · 负责人 {{ crew.current_leader }}
          </template>
          <em v-else>无当前班次</em>
        </div>
      </div>
    </div>

    <template v-else>
      <div class="identity-banner">
        当前身份：<strong>{{ session.session?.leader }}</strong>
        · {{ session.session?.crew_name }}
        · {{ session.session?.shift_label }}（签字 {{ session.session?.signed_at }}）
        · 身份快照版本 v{{ session.session?.version }}
      </div>

      <div class="grid-2">
        <!-- 交接签字 -->
        <div class="panel">
          <h3>交接签字</h3>
          <p class="page-desc">
            并发保护基线版本 v{{ identityVersion }}：若期间他人已交接，本次提交将收到 409 而不会覆盖。
          </p>
          <form class="handover-form" @submit.prevent="submitHandover">
            <label class="filter-item column">
              <span>接班负责人签字 *</span>
              <input v-model="handoverForm.to_leader" placeholder="接班值班负责人姓名" />
            </label>
            <label class="filter-item column">
              <span>新班次名称（留空按白班/夜班轮换）</span>
              <input v-model="handoverForm.label" placeholder="如：夜班" />
            </label>
            <label class="filter-item column">
              <span>在途车辆任务统一转给（可选）</span>
              <select v-model="handoverForm.driver_id">
                <option :value="null">保持原驾驶员</option>
                <option v-for="driver in myDrivers" :key="driver.id" :value="driver.id">
                  {{ driver.name }}
                </option>
              </select>
            </label>
            <label class="filter-item column">
              <span>交接备注</span>
              <textarea v-model="handoverForm.remark" rows="2"></textarea>
            </label>
            <p v-if="handoverError" class="error-text">{{ handoverError }}</p>
            <p v-if="handoverResult" class="success-text">{{ handoverResult }}</p>
            <button class="btn primary" type="submit" :disabled="signing">
              {{ signing ? '签字提交中…' : '确认交接签字' }}
            </button>
          </form>
        </div>

        <!-- 迁移报告 -->
        <div class="panel">
          <h3>存量数据迁移（启动时执行）</h3>
          <p class="page-desc">重叠班次按原交接签字时刻拆分；旧身份重复在途车辆任务关闭留痕。</p>
          <div v-if="migration" class="migration">
            <p>迁移时刻：{{ migration.at }}，审计事件 {{ migration.events }} 条</p>
            <div v-for="item in migration.splits" :key="`${item.shift_id}-${item.field}`" class="migration-row">
              班次 #{{ item.shift_id }} {{ item.field === 'start_at' ? '生效起点' : '结束点' }}
              改为签字时刻 {{ item.to }}（原为 {{ item.from || '空' }}）
            </div>
            <div v-for="item in migration.deduped" :key="item.vehicle_task_id" class="migration-row warn">
              {{ item.plate }} 重复在途任务 #{{ item.vehicle_task_id }} 已关闭，保留 #{{ item.kept_vehicle_task_id }}
            </div>
            <p v-if="!migration.splits.length && !migration.deduped.length" class="page-desc">
              无重叠班次、无重复派单。
            </p>
          </div>
        </div>
      </div>

      <!-- 明细页签 -->
      <nav class="tabs">
        <button
          v-for="tab in tabs"
          :key="tab.key"
          class="tab"
          :class="{ active: activeTab === tab.key }"
          type="button"
          @click="activeTab = tab.key"
        >
          {{ tab.label }}
        </button>
      </nav>

      <div class="panel">
        <table v-if="activeTab === 'shifts'" class="data-table">
          <thead>
            <tr><th>ID</th><th>班组</th><th>班次</th><th>生效(签字)</th><th>结束</th><th>当前</th><th>来源</th></tr>
          </thead>
          <tbody>
            <tr v-for="item in shifts" :key="item.id">
              <td>{{ item.id }}</td><td>{{ item.crew_name }}</td><td>{{ item.label }}</td>
              <td>{{ item.start_at }}<em v-if="item.signed_at !== item.start_at">（签 {{ item.signed_at }}）</em></td>
              <td>{{ item.end_at || '—' }}</td>
              <td>{{ item.is_current ? '是' : '否' }}</td><td>{{ item.source }}</td>
            </tr>
          </tbody>
        </table>

        <table v-else-if="activeTab === 'handovers'" class="data-table">
          <thead>
            <tr><th>ID</th><th>签字时刻</th><th>交班</th><th>接班</th><th>班次</th><th>版本</th><th>备注</th></tr>
          </thead>
          <tbody>
            <tr v-for="item in handovers" :key="item.id">
              <td>{{ item.id }}</td><td>{{ item.signed_at }}</td>
              <td>{{ item.from_leader }}</td><td>{{ item.to_leader }}</td>
              <td>#{{ item.from_shift_id }} → #{{ item.to_shift_id }}</td>
              <td>v{{ item.version_after }}</td><td>{{ item.remark || '—' }}</td>
            </tr>
          </tbody>
        </table>

        <table v-else-if="activeTab === 'vehicles'" class="data-table">
          <thead>
            <tr><th>ID</th><th>车辆</th><th>驾驶员</th><th>班次</th><th>状态</th><th>派出</th></tr>
          </thead>
          <tbody>
            <tr v-for="item in vehicleTasks" :key="item.id">
              <td>{{ item.id }}</td><td>{{ item.plate }}</td><td>{{ item.driver_name }}</td>
              <td>#{{ item.shift_id }} {{ item.shift?.label }}</td>
              <td :class="{ warn: item.status !== 'active' }">{{ item.status }}</td>
              <td>{{ item.issued_at }}</td>
            </tr>
            <tr v-if="!vehicleTasks.length"><td colspan="6" class="empty-state">当前班次无在途车辆任务</td></tr>
          </tbody>
        </table>

        <table v-else-if="activeTab === 'notifications'" class="data-table">
          <thead>
            <tr><th>驾驶员</th><th>类型</th><th>标题</th><th>内容</th><th>时间</th></tr>
          </thead>
          <tbody>
            <tr v-for="item in notifications" :key="item.id">
              <td>{{ item.driver_name }}</td><td>{{ item.type }}</td><td>{{ item.title }}</td>
              <td>{{ item.content }}</td><td>{{ item.created_at }}</td>
            </tr>
            <tr v-if="!notifications.length"><td colspan="5" class="empty-state">暂无司机通知</td></tr>
          </tbody>
        </table>

        <table v-else-if="activeTab === 'todos'" class="data-table">
          <thead>
            <tr><th>类型</th><th>标题</th><th>内容</th><th>班次</th><th>状态</th><th>时间</th></tr>
          </thead>
          <tbody>
            <tr v-for="item in todos" :key="item.id">
              <td>{{ item.kind }}</td><td>{{ item.title }}</td><td>{{ item.content }}</td>
              <td>#{{ item.shift_id }}</td><td>{{ item.status }}</td><td>{{ item.created_at }}</td>
            </tr>
            <tr v-if="!todos.length"><td colspan="6" class="empty-state">暂无班组待办</td></tr>
          </tbody>
        </table>

        <table v-else class="data-table">
          <thead>
            <tr><th>时刻</th><th>操作人</th><th>动作</th><th>对象</th><th>明细</th></tr>
          </thead>
          <tbody>
            <tr v-for="item in audit" :key="item.id">
              <td>{{ item.created_at }}</td><td>{{ item.actor }}</td><td>{{ item.action }}</td>
              <td>{{ item.target }}</td><td>{{ formatDetail(item.detail) }}</td>
            </tr>
            <tr v-if="!audit.length"><td colspan="5" class="empty-state">暂无审计事件</td></tr>
          </tbody>
        </table>
      </div>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import { ApiError, fetchJson } from '@/api/client'
import { useSessionStore } from '@/stores/session'

type Shift = {
  id: number; crew_name: string; label: string; start_at: string; signed_at: string
  end_at: string | null; is_current: boolean; source: string
}
type HandoverRow = {
  id: number; signed_at: string; from_leader: string; to_leader: string
  from_shift_id: number; to_shift_id: number; version_after: number; remark: string | null
}
type VehicleTaskRow = {
  id: number; plate: string; driver_name: string; shift_id: number; status: string
  issued_at: string; shift?: { label: string }
}
type MessageRow = { id: number; driver_name: string; type: string; title: string; content: string; created_at: string }
type TodoRow = { id: number; kind: string; title: string; content: string; shift_id: number; status: string; created_at: string }
type AuditRow = { id: number; created_at: string; actor: string; action: string; target: string; detail: Record<string, unknown> | null }
type MigrationSplit = {
  shift_id: number; crew_id: number; field: string; from: string | null; to: string
  handover_id: number
}
type MigrationDedupe = {
  vehicle_task_id: number; vehicle_id: number; plate: string; issued_at: string
  kept_vehicle_task_id: number
}
type Migration = {
  at: string; events: number
  splits: MigrationSplit[]; deduped: MigrationDedupe[]
}
type Option = { id: number; name: string; crew_id: number }

const session = useSessionStore()
const tabs = [
  { key: 'shifts', label: '班次时间线' },
  { key: 'handovers', label: '交接签字记录' },
  { key: 'vehicles', label: '车辆任务（当前班次）' },
  { key: 'notifications', label: '司机通知' },
  { key: 'todos', label: '班组待办' },
  { key: 'audit', label: '审计事件' },
] as const

const activeTab = ref<(typeof tabs)[number]['key']>('shifts')
const logging = ref(false)
const signing = ref(false)
const loginError = ref('')
const handoverError = ref('')
const handoverResult = ref('')
const loginForm = reactive({ crew_id: 1, leader: '' })
const handoverForm = reactive({ to_leader: '', label: '', remark: '', driver_id: null as number | null })

const shifts = ref<Shift[]>([])
const handovers = ref<HandoverRow[]>([])
const vehicleTasks = ref<VehicleTaskRow[]>([])
const notifications = ref<MessageRow[]>([])
const todos = ref<TodoRow[]>([])
const audit = ref<AuditRow[]>([])
const migration = ref<Migration | null>(null)

const identityVersion = computed(() => session.identityVersion)
const myCrewId = computed(() => session.session?.crew_id ?? null)
const allDrivers = ref<Option[]>([])
const myDrivers = computed<Option[]>(() =>
  allDrivers.value.filter((driver) => driver.crew_id === myCrewId.value),
)

async function loadOptions() {
  const payload = await fetchJson<{ crews: Array<Record<string, unknown>>; drivers: Option[]; vehicles: unknown[] }>(
    '/api/handover/options',
  )
  session.crews = payload.crews as never
  allDrivers.value = payload.drivers
  if (!loginForm.crew_id) {
    loginForm.crew_id = (payload.crews[0]?.id as number) ?? 0
  }
}

async function submitLogin() {
  loginError.value = ''
  logging.value = true
  try {
    await session.login(loginForm.crew_id, loginForm.leader)
    await refreshAll()
  } catch (error) {
    loginError.value = error instanceof Error ? error.message : '登录失败'
  } finally {
    logging.value = false
  }
}

async function submitHandover() {
  handoverError.value = ''
  handoverResult.value = ''
  signing.value = true
  try {
    const payload = await fetchJson<{ ok: boolean; message: string; entry: { transferred: { patrol_task_ids: number[]; vehicle_task_ids: number[] }; revoked_session_count: number; new_shift: { id: number } } }>(
      '/api/handover/sign',
      {
        method: 'POST',
        body: JSON.stringify({
          to_leader: handoverForm.to_leader,
          label: handoverForm.label || null,
          remark: handoverForm.remark || null,
          driver_id: handoverForm.driver_id,
          expected_version: identityVersion.value,
        }),
      },
    )
    handoverResult.value = `${payload.message}：转出巡查任务 ${payload.entry.transferred.patrol_task_ids.length} 条、车辆任务 ${payload.entry.transferred.vehicle_task_ids.length} 条，旧会话吊销 ${payload.entry.revoked_session_count} 个。请由新班次负责人重新登录。`
    session.logout()
  } catch (error) {
    if (error instanceof ApiError && error.conflict) {
      handoverError.value = `${error.message}（并发冲突，页面数据已刷新）`
    } else {
      handoverError.value = error instanceof Error ? error.message : '交接签字失败'
    }
  } finally {
    signing.value = false
    await loadOptions()
    await loadTimeline()
    if (session.loggedIn) await loadScoped()
  }
}

async function loadTimeline() {
  const [shiftPayload, handoverPayload, migrationPayload] = await Promise.all([
    fetchJson<{ items: Shift[] }>('/api/handover/shifts'),
    fetchJson<{ items: HandoverRow[] }>('/api/handover/handovers'),
    fetchJson<Migration>('/api/handover/migration'),
  ])
  shifts.value = shiftPayload.items
  handovers.value = handoverPayload.items
  migration.value = migrationPayload
}

async function loadScoped() {
  if (!session.loggedIn) return
  const [vt, note, todo, auditPayload] = await Promise.all([
    fetchJson<{ items: VehicleTaskRow[] }>('/api/handover/vehicle-tasks'),
    fetchJson<{ items: MessageRow[] }>('/api/handover/driver-notifications'),
    fetchJson<{ items: TodoRow[] }>('/api/handover/crew-todos'),
    fetchJson<{ items: AuditRow[] }>('/api/handover/audit'),
  ])
  vehicleTasks.value = vt.items
  notifications.value = note.items
  todos.value = todo.items
  audit.value = auditPayload.items
}

async function refreshAll() {
  await loadOptions()
  await loadTimeline()
  await loadScoped()
}

function formatDetail(detail: Record<string, unknown> | null) {
  return detail ? JSON.stringify(detail, null, 0) : '—'
}

onMounted(async () => {
  session.bindStaleHandler()
  await session.restore()
  await refreshAll()
})
</script>

<style scoped>
.panel { background: #fff; border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; margin-bottom: 12px; }
.panel h3 { margin: 0 0 10px; font-size: 15px; }
.identity-banner { background: #eef5ff; border: 1px solid #b9d3f7; border-radius: 8px; padding: 8px 12px; font-size: 13px; margin-bottom: 12px; }
.grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.handover-form { display: flex; flex-direction: column; gap: 10px; }
.filter-item.column { display: flex; flex-direction: column; }
.filter-item.column span { margin-bottom: 4px; }
.current-shifts { display: flex; flex-direction: column; gap: 6px; margin-top: 10px; font-size: 13px; }
.shift-chip { background: #f8fafc; border: 1px solid var(--border); border-radius: 6px; padding: 6px 10px; }
.shift-chip em { color: var(--muted); font-style: normal; margin-left: 6px; }
.stale-text { color: #b42318; }
.success-text { color: #147d3c; }
.migration-row { font-size: 12px; padding: 4px 0; border-bottom: 1px dashed var(--border); }
.migration-row.warn { color: #b42318; }
.tabs { display: flex; gap: 6px; margin-bottom: 8px; flex-wrap: wrap; }
.tab { border: 1px solid var(--border); background: #fff; border-radius: 6px 6px 0 0; padding: 6px 12px; cursor: pointer; font-size: 13px; }
.tab.active { background: var(--brand); color: #fff; border-color: var(--brand); }
.warn { color: #b42318; }
</style>
