<template>
  <section class="page" data-module="patrol">
    <header class="page-head">
      <div>
        <h2>日常巡查任务</h2>
        <p class="page-desc">
          巡查任务以班组当前班次为准；派单结果同步写入巡查详情、司机通知与班组待办。
        </p>
      </div>
      <div class="page-actions">
        <label class="filter-item inline">
          <span>班组</span>
          <select v-model.number="crewId" @change="reload">
            <option v-for="crew in session.crews" :key="crew.id" :value="crew.id">
              {{ crew.name }}（{{ crew.code }}）
            </option>
          </select>
        </label>
        <button class="btn" type="button" @click="reload">刷新</button>
      </div>
    </header>

    <div v-if="!session.loggedIn" class="login-banner">
      当前未登录：列表可查看，但派单需要由当前班次值班负责人登录后进行。
      <RouterLink class="link" to="/handover">去班组交接登录</RouterLink>
    </div>
    <div v-else class="shift-banner">
      已登录 <strong>{{ session.session?.crew_name }}</strong>
      · {{ session.session?.shift_label }}
      · 签字时刻 {{ session.session?.signed_at }}
      · 身份版本 v{{ identityVersion }}
    </div>
    <div v-if="session.staleMessage" class="stale-banner">
      {{ session.staleMessage }}
      <RouterLink class="link" to="/handover">由当前班次重新登录</RouterLink>
    </div>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">任务总数</span>
        <strong class="stat-value">{{ total }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">待派单</span>
        <strong class="stat-value">{{ statusCount['待派单'] ?? 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">进行中</span>
        <strong class="stat-value">{{ statusCount['进行中'] ?? 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">已完成</span>
        <strong class="stat-value">{{ statusCount['已完成'] ?? 0 }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>巡查编号</span>
        <input v-model="keyword" placeholder="按巡查编号检索" />
      </label>
      <label class="filter-item">
        <span>任务状态</span>
        <select v-model="status">
          <option value="">全部</option>
          <option v-for="item in statuses" :key="item" :value="item">{{ item }}</option>
        </select>
      </label>
      <label class="filter-item check">
        <input v-model="currentShiftOnly" type="checkbox" @change="reload" />
        <span>只看当前班次</span>
      </label>
      <button class="btn" type="submit">查询</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column.key">{{ column.label }}</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column.key">
            <button v-if="column.key === 'task_no'" class="link" type="button" @click="openDetail(row)">
              {{ row[column.key] ?? '—' }}
            </button>
            <template v-else>{{ row[column.key] || '—' }}</template>
          </td>
          <td class="row-actions">
            <button class="link" type="button" @click="openDetail(row)">详情</button>
            <button
              v-if="row.status === '待派单'"
              class="link"
              type="button"
              @click="openDispatch(row)"
            >
              派单
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无巡查任务</td>
        </tr>
      </tbody>
    </table>

    <!-- 派单弹窗：此前点不动就是因为只有占位入口，现在真正调鉴权派单接口 -->
    <div v-if="dispatchTarget" class="modal-mask" @click.self="closeDispatch">
      <div class="modal">
        <h3>巡查派单 · {{ dispatchTarget.task_no }}</h3>
        <p class="page-desc">{{ dispatchTarget.section }} · {{ dispatchTarget.plan_date }}</p>
        <form class="modal-form" @submit.prevent="submitDispatch">
          <label class="filter-item">
            <span>驾驶员</span>
            <select v-model.number="dispatchForm.driver_id">
              <option :value="null">请选择本班驾驶员</option>
              <option v-for="driver in crewDrivers" :key="driver.id" :value="driver.id">
                {{ driver.name }}（{{ driver.phone }}）
              </option>
            </select>
          </label>
          <label class="filter-item">
            <span>巡查车辆</span>
            <select v-model.number="dispatchForm.vehicle_id">
              <option :value="null">请选择本班车辆</option>
              <option v-for="vehicle in crewVehicles" :key="vehicle.id" :value="vehicle.id">
                {{ vehicle.plate }} · {{ vehicle.kind }}
              </option>
            </select>
          </label>
          <label class="filter-item column">
            <span>发现问题</span>
            <textarea v-model="dispatchForm.issue" rows="2"></textarea>
          </label>
          <label class="filter-item column">
            <span>处置措施</span>
            <textarea v-model="dispatchForm.measure" rows="2"></textarea>
          </label>
          <p v-if="dispatchError" class="error-text">{{ dispatchError }}</p>
          <div class="modal-actions">
            <button class="btn ghost" type="button" @click="closeDispatch">取消</button>
            <button class="btn primary" type="submit" :disabled="dispatching">
              {{ dispatching ? '派单中…' : '确认派单' }}
            </button>
          </div>
        </form>
      </div>
    </div>

    <!-- 详情抽屉：字段与列表同源，并展示责任链/交接记录 -->
    <aside v-if="detail" class="drawer">
      <header class="drawer-head">
        <h3>{{ detail.task_no }}</h3>
        <button class="btn ghost" type="button" @click="detail = null">关闭</button>
      </header>
      <dl class="detail-grid">
        <template v-for="column in columns" :key="column.key">
          <dt>{{ column.label }}</dt>
          <dd>{{ detail[column.key] || '—' }}</dd>
        </template>
        <dt>所属班组</dt>
        <dd>{{ detail.crew_name }}</dd>
        <dt>发现问题</dt>
        <dd>{{ detail.issue || '—' }}</dd>
        <dt>处置措施</dt>
        <dd>{{ detail.measure || '—' }}</dd>
        <dt>派单人</dt>
        <dd>{{ detail.assigned_by || '—' }} <em v-if="detail.assigned_at">（{{ detail.assigned_at }}）</em></dd>
        <dt>最近交接</dt>
        <dd v-if="detail.last_handover">
          {{ detail.last_handover.from_leader }} → {{ detail.last_handover.to_leader }}
          （{{ detail.last_handover.signed_at }}）
        </dd>
        <dd v-else>无（历史任务保留原签字记录）</dd>
      </dl>

      <h4>任务责任链</h4>
      <ul class="history-list">
        <li v-for="(item, index) in detail.responsibility ?? []" :key="index">
          <span class="history-time">{{ item.since }}</span>
          {{ item.driver_name || '待派驾驶员' }} · {{ item.plate || '待派车辆' }}
          <em>{{ item.note }}</em>
        </li>
        <li v-if="!(detail.responsibility?.length)" class="empty-state">尚未派单</li>
      </ul>

      <div class="drawer-actions">
        <button
          v-if="detail.status === '待派单'"
          class="btn primary"
          type="button"
          @click="openDispatch(detail)"
        >
          派单
        </button>
      </div>
    </aside>

    <footer class="page-foot">
      <span>共 {{ total }} 条巡查任务</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import { ApiError, fetchJson, request } from '@/api/client'
import { useSessionStore } from '@/stores/session'

type ResponsibilityItem = {
  shift_id: number | null
  handover_id: number | null
  since: string
  driver_name: string | null
  plate: string | null
  note: string
}
type LastHandover = {
  id: number
  signed_at: string
  from_leader: string
  to_leader: string
}
type TaskRow = {
  id: number
  task_no: string
  section: string
  plan_date: string
  status: string
  crew_id: number
  crew_name?: string | null
  driver_id: number | null
  driver_name?: string | null
  vehicle_id: number | null
  plate?: string | null
  shift_id?: number | null
  shift_label?: string | null
  signed_at?: string | null
  assigned_at?: string | null
  assigned_by?: string | null
  issue?: string
  measure?: string
  last_handover?: LastHandover | null
  responsibility?: ResponsibilityItem[]
  [key: string]: string | number | null | LastHandover | ResponsibilityItem[] | undefined
}
type Column = { key: string; label: string }
type Option = { id: number; name: string; phone?: string; plate?: string; kind?: string; crew_id: number }
type CrewOption = {
  id: number
  code: string
  name: string
  version: number
  current_leader: string | null
  current_shift: { id: number; label: string; signed_at: string } | null
}

const ENDPOINT = '/api/patrol'
const session = useSessionStore()

const columns = ref<Column[]>([
  { key: 'task_no', label: '巡查编号' },
  { key: 'section', label: '巡查路段' },
  { key: 'plan_date', label: '巡查日期' },
  { key: 'status', label: '任务状态' },
  { key: 'driver_name', label: '责任驾驶员' },
  { key: 'plate', label: '巡查车辆' },
  { key: 'shift_label', label: '当前班次' },
  { key: 'signed_at', label: '班次签字时刻' },
])
const statuses = ['待派单', '进行中', '已完成']

const rows = ref<TaskRow[]>([])
const total = ref(0)
const errorMessage = ref('')
const keyword = ref('')
const status = ref('')
const currentShiftOnly = ref(false)
const crewId = ref<number | null>(null)
const detail = ref<TaskRow | null>(null)

const dispatchTarget = ref<TaskRow | null>(null)
const dispatchError = ref('')
const dispatching = ref(false)
const dispatchForm = reactive({ driver_id: null as number | null, vehicle_id: null as number | null, issue: '', measure: '' })

const identityVersion = computed(() => session.identityVersion)
const statusCount = computed<Record<string, number>>(() => {
  const counter: Record<string, number> = {}
  for (const row of rows.value) counter[row.status] = (counter[row.status] ?? 0) + 1
  return counter
})
const crewDrivers = computed<Option[]>(() =>
  optionsCache.value.drivers.filter((item) => item.crew_id === crewId.value),
)
const crewVehicles = computed<Option[]>(() =>
  optionsCache.value.vehicles.filter((item: Option) => item.crew_id === crewId.value),
)
const optionsCache = ref<{ drivers: Option[]; vehicles: Option[] }>({ drivers: [], vehicles: [] })

async function loadColumns() {
  const payload = await fetchJson<{ columns: Column[] }>('/api/patrol/columns')
  if (payload.columns?.length) columns.value = payload.columns
}

async function loadOptions() {
  const payload = await fetchJson<{ crews: CrewOption[]; drivers: Option[]; vehicles: Option[] }>('/api/handover/options')
  optionsCache.value = { drivers: payload.drivers, vehicles: payload.vehicles }
  // 直接从巡查页进入（未经过交接中心）时也要回填班组选项
  if (!session.crews.length) session.crews = payload.crews
  if (crewId.value === null && payload.crews.length) crewId.value = payload.crews[0].id
}

async function reload() {
  errorMessage.value = ''
  try {
    const params = new URLSearchParams()
    if (keyword.value) params.set('keyword', keyword.value)
    if (status.value) params.set('status', status.value)
    if (crewId.value !== null) params.set('crew_id', String(crewId.value))
    if (currentShiftOnly.value) params.set('current_shift', 'true')
    params.set('size', '100')
    const payload = await fetchJson<{ items: TaskRow[]; total: number }>(`${ENDPOINT}?${params}`)
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '巡查任务列表读取失败'
  }
}

async function openDetail(row: TaskRow) {
  errorMessage.value = ''
  detail.value = row
  try {
    // 详情与列表同接口口径，只是多带 responsibility 责任链
    detail.value = await fetchJson<TaskRow>(`${ENDPOINT}/${row.id}`)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '巡查详情读取失败'
  }
}

function openDispatch(row: TaskRow) {
  if (!session.loggedIn) {
    errorMessage.value = '请先由当前班次值班负责人登录后再派单'
    return
  }
  dispatchTarget.value = row
  dispatchError.value = ''
  Object.assign(dispatchForm, { driver_id: null, vehicle_id: null, issue: '', measure: '' })
}

function closeDispatch() {
  dispatchTarget.value = null
}

async function submitDispatch() {
  if (!dispatchTarget.value) return
  if (dispatchForm.driver_id === null || dispatchForm.vehicle_id === null) {
    dispatchError.value = '驾驶员与车辆都必须选择'
    return
  }
  dispatchError.value = ''
  dispatching.value = true
  try {
    const response = await request(`${ENDPOINT}/${dispatchTarget.value.id}/dispatch`, {
      method: 'POST',
      body: JSON.stringify({ ...dispatchForm }),
    })
    const payload = (await response.json()) as { ok: boolean; message: string; entry: TaskRow }
    if (!payload.ok) {
      dispatchError.value = payload.message
      return
    }
    dispatchTarget.value = null
    detail.value = null
    await reload()
  } catch (error) {
    if (error instanceof ApiError) {
      dispatchError.value = error.conflict ? `${error.message}（请刷新班次后重试）` : error.message
    } else {
      dispatchError.value = error instanceof Error ? error.message : '派单失败'
    }
  } finally {
    dispatching.value = false
  }
}

onMounted(async () => {
  await Promise.all([loadColumns(), loadOptions()])
  if (crewId.value === null && session.crews.length) crewId.value = session.crews[0].id
  await reload()
})
</script>

<style scoped>
.inline { display: flex; align-items: center; gap: 6px; }
.inline select { padding: 4px 6px; }
.login-banner, .shift-banner, .stale-banner {
  border-radius: 8px; padding: 8px 12px; font-size: 13px; margin-bottom: 10px;
}
.login-banner { background: #fff8e1; border: 1px solid #f0d98c; }
.shift-banner { background: #eef5ff; border: 1px solid #b9d3f7; }
.stale-banner { background: #fdecea; border: 1px solid #f0a9a2; color: #b42318; }
.modal-mask {
  position: fixed; inset: 0; background: rgba(15, 23, 42, 0.45);
  display: flex; align-items: center; justify-content: center; z-index: 50;
}
.modal { background: #fff; border-radius: 10px; padding: 18px 20px; width: 460px; }
.modal h3 { margin: 0 0 4px; }
.modal-form { display: flex; flex-direction: column; gap: 10px; margin-top: 12px; }
.filter-item.column span { margin-bottom: 4px; }
.filter-item.check { display: flex; flex-direction: row; align-items: center; gap: 6px; }
.modal-actions { display: flex; justify-content: flex-end; gap: 8px; }
.drawer {
  position: fixed; top: 0; right: 0; width: 420px; height: 100vh; z-index: 40;
  background: #fff; border-left: 1px solid var(--border); padding: 16px 18px;
  overflow-y: auto; box-shadow: -8px 0 24px rgba(15, 23, 42, 0.12);
}
.drawer-head { display: flex; justify-content: space-between; align-items: center; }
.detail-grid { display: grid; grid-template-columns: 110px 1fr; gap: 6px 10px; font-size: 13px; }
.detail-grid dt { color: var(--muted); }
.detail-grid dd { margin: 0; }
.detail-grid em { color: var(--muted); font-style: normal; }
.history-list { list-style: none; padding: 0; margin: 8px 0 0; font-size: 13px; }
.history-list li { padding: 6px 0; border-bottom: 1px dashed var(--border); }
.history-time { color: var(--muted); margin-right: 8px; }
.history-list em { color: var(--muted); font-style: normal; margin-left: 6px; }
.drawer-actions { margin-top: 16px; }
</style>
