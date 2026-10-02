<template>
  <section class="page" data-module="patrol">
    <header class="page-head">
      <div>
        <h2>日常巡查管理</h2>
        <p class="page-desc">列表与详情同一口径；派单/转派只认当前班次身份，交接后旧身份会被拦截。</p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" @click="exportRows">导出日常巡查清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>责任班次/司机</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>
            <span v-if="row.responsible_shift_id">
              #{{ row.responsible_shift_id }} · {{ row.responsible_driver ?? '—' }}
              <small v-if="row.dispatched" class="muted-text">（{{ row.responsible_vehicle_plate }}）</small>
            </span>
            <span v-else>—</span>
          </td>
          <td class="row-actions">
            <button class="link" type="button" @click="openDetail(row)">详情</button>
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button class="link" type="button" :disabled="!session.canOperate" @click="openDispatch(row)">
              {{ row.dispatched ? '转派' : '派单' }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 2" class="empty-state">暂无日常巡查数据</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条日常巡查记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <!-- 详情抽屉：字段与列表同源，交接回写与转派链在这里可见 -->
    <div v-if="detail" class="drawer-mask" @click.self="detail = null">
      <div class="drawer">
        <header class="drawer-head">
          <h3>巡查详情 {{ detail.巡查编号 }}</h3>
          <button class="btn ghost" type="button" @click="detail = null">关闭</button>
        </header>
        <dl class="detail-grid">
          <template v-for="item in detailItems" :key="item.key">
            <dt>{{ item.label }}</dt>
            <dd>{{ item.value ?? '—' }}</dd>
          </template>
        </dl>
        <h4>交接回写</h4>
        <p class="muted-text">{{ detail.交接备注 || '暂无交接记录' }}</p>
        <h4>转派链（历史签字保留）</h4>
        <ul v-if="detail.transfer_chain?.length" class="chain-list">
          <li v-for="(link, index) in detail.transfer_chain" :key="index">
            班次 {{ link.from_shift_id ?? '—' }}（{{ link.from_driver ?? '—' }}）
            → 班次 {{ link.to_shift_id }}（{{ link.to_driver }}）· {{ link.reason }} · {{ link.at }}
          </li>
        </ul>
        <p v-else class="muted-text">无转派记录，按原签字班次保留</p>
      </div>
    </div>

    <!-- 派单/转派弹层 -->
    <div v-if="dispatchTarget" class="drawer-mask" @click.self="dispatchTarget = null">
      <div class="drawer drawer-sm">
        <header class="drawer-head">
          <h3>{{ dispatchTarget.dispatched ? '转派' : '派单' }} · {{ dispatchTarget.巡查编号 }}</h3>
          <button class="btn ghost" type="button" @click="dispatchTarget = null">取消</button>
        </header>
        <p class="muted-text">
          留空则取当前班次默认司机/车辆。当前班次：{{ session.shiftLabel }}
        </p>
        <label class="filter-item">
          <span>司机姓名</span>
          <input v-model="dispatchForm.driver" placeholder="默认当前班次司机" />
        </label>
        <label class="filter-item">
          <span>车牌号</span>
          <input v-model="dispatchForm.vehicle_plate" placeholder="默认当前班次车辆" />
        </label>
        <div class="page-actions">
          <button class="btn primary" type="button" :disabled="submitting" @click="submitDispatch">
            确认{{ dispatchTarget.dispatched ? '转派' : '派单' }}
          </button>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request, requestJson } from '@/api/client'
import { useSessionStore } from '@/stores/session'

type Row = Record<string, string | number | null> & {
  id: number
  transfer_chain?: Array<Record<string, string | number | null>>
}

const ENDPOINT = '/api/patrol'
const columns = ['巡查编号', '巡查路段', '巡查日期', '巡查人员', '巡查车辆', '发现问题', '处置措施', '巡查状态']
const actions = ['开始巡查', '完成巡查', '复核确认']
const stats = ref([
  { label: '巡查任务', value: 0 },
  { label: '待处理', value: 0 },
  { label: '已派单', value: 0 },
])

const session = useSessionStore()
const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

const detail = ref<Row | null>(null)
const dispatchTarget = ref<Row | null>(null)
const dispatchForm = ref({ driver: '', vehicle_plate: '' })
const submitting = ref(false)

const detailItems = computed(() => {
  if (!detail.value) {
    return []
  }
  const row = detail.value
  return [
    { key: '巡查路段', label: '巡查路段', value: row.巡查路段 },
    { key: '巡查日期', label: '巡查日期', value: row.巡查日期 },
    { key: '巡查人员', label: '巡查人员', value: row.巡查人员 },
    { key: '巡查车辆', label: '巡查车辆', value: row.巡查车辆 },
    { key: '发现问题', label: '发现问题', value: row.发现问题 },
    { key: '处置措施', label: '处置措施', value: row.处置措施 },
    { key: '巡查状态', label: '巡查状态', value: row.巡查状态 },
    { key: 'signed_shift_id', label: '原签字班次', value: `#${row.signed_shift_id ?? '—'}（签字人 ${row.signed_by ?? '—'}）` },
    { key: 'responsible', label: '当前责任', value: `#${row.responsible_shift_id ?? '—'} ${row.responsible_driver ?? ''} ${row.responsible_vehicle_plate ?? ''}` },
    { key: 'dispatched_at', label: '派单时间', value: row.dispatched_at },
  ]
})

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

async function openDetail(row: Row) {
  errorMessage.value = ''
  const { response, data } = await requestJson<Row>(`${ENDPOINT}/${row.id}`)
  if (!response.ok) {
    errorMessage.value = (data as { detail?: string }).detail ?? '详情读取失败'
    return
  }
  detail.value = data
}

function openDispatch(row: Row) {
  if (!session.isLoggedIn) {
    errorMessage.value = '请先在「班组交接」页登录后再派单'
    return
  }
  if (!session.canOperate) {
    errorMessage.value = '只有当前班次班长/值班员可以派单'
    return
  }
  dispatchTarget.value = row
  dispatchForm.value = { driver: '', vehicle_plate: '' }
}

async function submitDispatch() {
  if (!dispatchTarget.value) {
    return
  }
  submitting.value = true
  errorMessage.value = ''
  try {
    const { response, data } = await requestJson<{ message?: string; detail?: string }>(
      `${ENDPOINT}/${dispatchTarget.value.id}/dispatch`,
      {
        method: 'POST',
        body: JSON.stringify({
          driver: dispatchForm.value.driver || null,
          vehicle_plate: dispatchForm.value.vehicle_plate || null,
        }),
      },
    )
    if (!response.ok) {
      throw new Error(data.detail || data.message || '派单未生效')
    }
    dispatchTarget.value = null
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '派单失败'
  } finally {
    submitting.value = false
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    // 后端约定信封是 { values: { action } }；之前直接发 { action } 导致按钮点不动。
    const { response, data } = await requestJson<{ ok?: boolean; message?: string }>(
      `${ENDPOINT}/${row.id}/actions`,
      { method: 'POST', body: JSON.stringify({ values: { action } }) },
    )
    if (!response.ok || data.ok === false) {
      throw new Error(data.message || '日常巡查动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '日常巡查操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('巡查记录列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    stats.value = [
      { label: '巡查任务', value: total.value },
      { label: '待处理', value: rows.value.filter((row) => row.status === '待巡查' || row.status === '巡查中').length },
      { label: '已派单', value: rows.value.filter((row) => row.dispatched).length },
    ]
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '日常巡查列表读取失败'
  }
}

onMounted(reload)
</script>
