<template>
  <section class="page" data-module="handover">
    <header class="page-head">
      <div>
        <h2>班组交接</h2>
        <p class="page-desc">
          当前班次以交接签字时刻为准；在途任务原子转派，历史任务按原签字记录保留。
        </p>
      </div>
    </header>

    <!-- 登录 -->
    <article v-if="!session.isLoggedIn" class="panel">
      <h3>登录（演示环境按工号免密）</h3>
      <form class="filter-bar" @submit.prevent="login">
        <label class="filter-item">
          <span>工号</span>
          <select v-model="loginCode">
            <option value="P01">P01 张建国（巡查一班·班长）</option>
            <option value="P02">P02 李卫东（巡查一班·司机）</option>
            <option value="P03">P03 王海涛（巡查一班·司机）</option>
            <option value="P04">P04 赵守夜（巡查一班·值班员）</option>
            <option value="P05">P05 陈大康（巡查一班·班长）</option>
            <option value="Q01">Q01 孙立群（巡查二班·班长）</option>
            <option value="Q02">Q02 周明亮（巡查二班·司机）</option>
          </select>
        </label>
        <button class="btn primary" type="submit">登录</button>
      </form>
      <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>
    </article>

    <template v-else>
      <div class="stat-row">
        <article class="stat-card">
          <span class="stat-label">当前身份</span>
          <strong class="stat-value">{{ session.name }} · {{ session.role }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">当前班次</span>
          <strong class="stat-value">
            #{{ current?.id ?? '—' }} v{{ current?.version ?? '—' }} {{ current?.driver ?? '' }}
          </strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">当班车辆</span>
          <strong class="stat-value">{{ current?.vehicle_plate ?? '—' }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">生效时刻</span>
          <strong class="stat-value small-value">{{ current?.started_at ?? '—' }}</strong>
        </article>
      </div>

      <div class="page-actions" style="margin-bottom: 12px">
        <button class="btn" type="button" @click="refresh">刷新</button>
        <button class="btn ghost" type="button" @click="logout">注销</button>
      </div>

      <!-- 签字交接 -->
      <article class="panel">
        <h3>交接签字</h3>
        <form class="filter-bar" @submit.prevent="submitHandover">
          <label class="filter-item">
            <span>班组</span>
            <input v-model="handoverForm.crew" />
          </label>
          <label class="filter-item">
            <span>交班班长</span>
            <input v-model="handoverForm.out_foreman" />
          </label>
          <label class="filter-item">
            <span>接班班长</span>
            <input v-model="handoverForm.in_foreman" />
          </label>
          <label class="filter-item">
            <span>接班司机</span>
            <input v-model="handoverForm.in_driver" />
          </label>
          <label class="filter-item">
            <span>接班车辆</span>
            <input v-model="handoverForm.in_vehicle_plate" />
          </label>
          <input type="hidden" :value="handoverForm.expected_shift_id" />
          <button class="btn primary" type="submit" :disabled="!session.canOperate">签字交接</button>
        </form>
        <p class="muted-text">
          版本号乐观锁：两人同时交接时只有一个版本成为当前班次，另一个会收到 409 并要求刷新。
          <span v-if="!session.canOperate" class="error-text">当前角色无交接权限。</span>
        </p>
      </article>

      <p v-if="message" class="success-text">{{ message }}</p>
      <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>

      <div class="two-col">
        <!-- 车辆任务看板（去重后） -->
        <article class="panel">
          <h3>车辆当前任务（一条任务只出现一次）</h3>
          <table class="data-table">
            <thead>
              <tr><th>编号</th><th>司机</th><th>车辆</th><th>责任班次</th><th>状态</th></tr>
            </thead>
            <tbody>
              <tr v-for="task in vehicleTasks" :key="task.任务id">
                <td>{{ task.巡查编号 }}</td>
                <td>{{ task.当班司机 }}<small v-if="task.历史签字保留" class="muted-text">（历史保留）</small></td>
                <td>{{ task.车辆 }}</td>
                <td>#{{ task.责任班次 }}</td>
                <td>{{ task.status }}</td>
              </tr>
              <tr v-if="!vehicleTasks.length"><td colspan="5" class="empty-state">暂无已派单任务</td></tr>
            </tbody>
          </table>
        </article>

        <!-- 班组待办 -->
        <article class="panel">
          <h3>班组待办</h3>
          <table class="data-table">
            <thead>
              <tr><th>事项</th><th>来源</th><th>承接人</th></tr>
            </thead>
            <tbody>
              <tr v-for="todo in todos" :key="todo.id">
                <td>{{ todo.title }}</td>
                <td>{{ todo.source }}</td>
                <td>{{ todo.assignee ?? '—' }}</td>
              </tr>
              <tr v-if="!todos.length"><td colspan="3" class="empty-state">暂无待办</td></tr>
            </tbody>
          </table>
        </article>
      </div>

      <!-- 司机通知 -->
      <article class="panel">
        <h3>司机通知（{{ session.name }}）</h3>
        <table class="data-table">
          <thead>
            <tr><th>类型</th><th>标题</th><th>内容</th><th>时间</th></tr>
          </thead>
          <tbody>
            <tr v-for="notice in notifications" :key="notice.id">
              <td>{{ notice.kind }}</td>
              <td>{{ notice.title }}</td>
              <td>{{ notice.content }}</td>
              <td>{{ notice.created_at }}</td>
            </tr>
            <tr v-if="!notifications.length"><td colspan="4" class="empty-state">暂无通知</td></tr>
          </tbody>
        </table>
      </article>

      <!-- 班次版本 -->
      <article class="panel">
        <h3>班次版本与签字记录</h3>
        <table class="data-table">
          <thead>
            <tr><th>#</th><th>版本</th><th>班长</th><th>司机</th><th>车辆</th><th>生效</th><th>收尾</th><th>当前</th><th>来源</th></tr>
          </thead>
          <tbody>
            <tr v-for="shift in shifts" :key="shift.id">
              <td>{{ shift.id }}</td>
              <td>v{{ shift.version }}</td>
              <td>{{ shift.foreman }}</td>
              <td>{{ shift.driver }}</td>
              <td>{{ shift.vehicle_plate }}</td>
              <td>{{ shift.started_at }}</td>
              <td>{{ shift.end_at ?? '—' }}</td>
              <td>{{ shift.is_current ? '是' : '' }}</td>
              <td>{{ shift.source }}{{ shift.split_from_id ? `（拆自#${shift.split_from_id}）` : '' }}</td>
            </tr>
          </tbody>
        </table>
      </article>

      <!-- 审计 -->
      <article class="panel">
        <h3>审计事件</h3>
        <table class="data-table">
          <thead>
            <tr><th>#</th><th>事件</th><th>操作人</th><th>时间</th></tr>
          </thead>
          <tbody>
            <tr v-for="event in audits" :key="event.id">
              <td>{{ event.id }}</td>
              <td>{{ event.event }}</td>
              <td>{{ event.actor?.name ?? '—' }}（{{ event.actor?.role ?? '' }}）</td>
              <td>{{ event.created_at }}</td>
            </tr>
          </tbody>
        </table>
      </article>
    </template>
  </section>
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'

import { requestJson } from '@/api/client'
import { useSessionStore, type CurrentShift } from '@/stores/session'

const session = useSessionStore()
const loginCode = ref('P01')
const errorMessage = ref('')
const message = ref('')

const current = ref<NonNullable<CurrentShift> | null>(null)
const shifts = ref<Array<Record<string, any>>>([])
const vehicleTasks = ref<Array<Record<string, any>>>([])
const todos = ref<Array<Record<string, any>>>([])
const notifications = ref<Array<Record<string, any>>>([])
const audits = ref<Array<Record<string, any>>>([])

const handoverForm = ref({
  crew: '巡查一班',
  expected_shift_id: 0,
  out_foreman: '张建国',
  in_foreman: '赵守夜',
  in_driver: '李卫东',
  in_vehicle_plate: '京A·1003',
})

async function login() {
  errorMessage.value = ''
  const { response, data } = await requestJson<{
    token: string
    user: { 工号: string; 姓名: string; 班组: string; 角色: string }
    shift: CurrentShift
    detail?: string
  }>('/api/auth/login', { method: 'POST', body: JSON.stringify({ user_code: loginCode.value }) })
  if (!response.ok) {
    errorMessage.value = data.detail ?? '登录失败'
    return
  }
  session.setSession(data)
  if (data.shift) {
    handoverForm.value.crew = data.shift.crew
    handoverForm.value.expected_shift_id = data.shift.id
    handoverForm.value.out_foreman = data.shift.foreman
  }
  await refresh()
}

async function logout() {
  await requestJson('/api/auth/logout', { method: 'POST' }).catch(() => undefined)
  session.clear()
}

async function refresh() {
  errorMessage.value = ''
  if (!session.crew) {
    return
  }
  const [cur, list, tasks, todoList, noticeList, auditList] = await Promise.all([
    requestJson<{ shift: CurrentShift }>(`/api/shifts/current?crew=${encodeURIComponent(session.crew)}`),
    requestJson<Array<Record<string, any>>>('/api/shifts'),
    requestJson<{ items: Array<Record<string, any>> }>('/api/vehicle/tasks'),
    requestJson<Array<Record<string, any>>>(`/api/shifts/todos?crew=${encodeURIComponent(session.crew)}`),
    requestJson<Array<Record<string, any>>>('/api/shifts/notifications'),
    requestJson<Array<Record<string, any>>>('/api/shifts/audit').catch(() => ({ response: { ok: false } as Response, data: [] as Array<Record<string, any>> })),
  ])
  if (cur.response.ok) {
    current.value = cur.data.shift as NonNullable<CurrentShift>
    session.setCurrentShift(cur.data.shift)
    if (cur.data.shift) {
      handoverForm.value.expected_shift_id = cur.data.shift.id
    }
  }
  if (list.response.ok) {
    shifts.value = list.data.filter((shift) => shift && shift.crew === session.crew)
  }
  if (tasks.response.ok) {
    vehicleTasks.value = tasks.data.items
  }
  if (todoList.response.ok) {
    todos.value = todoList.data
  }
  if (noticeList.response.ok) {
    notifications.value = noticeList.data
  }
  audits.value = auditList.data ?? []
}

async function submitHandover() {
  errorMessage.value = ''
  message.value = ''
  const { response, data } = await requestJson<{ message?: string; detail?: string; entry?: any }>(
    '/api/shifts/handover',
    { method: 'POST', body: JSON.stringify(handoverForm.value) },
  )
  if (!response.ok) {
    if (response.status === 409) {
      errorMessage.value = data.detail ?? '班次已被其他交接更新，请刷新后重试'
    } else if (response.status === 401) {
      errorMessage.value = data.detail ?? '身份已失效，请重新登录'
      session.clear()
    } else {
      errorMessage.value = data.detail ?? '交接失败'
    }
    return
  }
  message.value = `${data.message ?? '交接完成'}，转派任务 ${data.entry?.transferred?.length ?? 0} 条，失效旧会话 ${data.entry?.revoked_sessions ?? 0} 个`
  await refresh()
}

function onUnauthorized() {
  errorMessage.value = '登录态已失效（通常是交接后旧令牌被回收），请重新登录'
  session.clear()
}

onMounted(() => {
  window.addEventListener('patrol:unauthorized', onUnauthorized)
  if (session.isLoggedIn) {
    void refresh()
  }
})
onBeforeUnmount(() => window.removeEventListener('patrol:unauthorized', onUnauthorized))
</script>
