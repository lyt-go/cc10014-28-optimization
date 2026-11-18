<template>
  <section class="page" data-module="table_compare">
    <header class="page-head">
      <div>
        <h2>表级比对</h2>
        <p class="page-desc">分批读取左右表批次记录生成差异报告，保留字段级差异、采样比例与重检结果。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" data-testid="open-create" @click="openCreate">登记比对任务</button>
        <button class="btn" type="button" data-testid="export" @click="exportRows">导出作业清单</button>
      </div>
    </header>

    <h3 class="section-title">比对任务列表</h3>

    <!-- 列表视图：过滤、搜索定位、检索、排序、翻页分页 -->
    <div>
      <form class="filter-bar" @submit.prevent="reload(1)">
        <label class="filter-item"><span>任务编号</span>
          <input v-model="filters['任务编号']" data-testid="filter-任务编号" placeholder="按任务编号检索" /></label>
        <label class="filter-item"><span>左表名称</span>
          <input v-model="filters['左表名称']" data-testid="filter-左表名称" placeholder="按左表检索" /></label>
        <label class="filter-item"><span>比对批次</span>
          <input v-model="filters['比对批次']" data-testid="filter-比对批次" placeholder="按批次检索" /></label>
        <label class="filter-item"><span>搜索定位</span>
          <input v-model="keyword" data-testid="search-keyword" placeholder="任务编号/左右表/批次关键字" /></label>
        <label class="filter-item"><span>状态</span>
          <select v-model="status" data-testid="filter-status">
            <option value="">全部</option>
            <option v-for="s in statuses" :key="s" :value="s">{{ s }}</option>
          </select>
        </label>
        <button class="btn" data-testid="query" type="submit">查询</button>
        <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
      </form>

      <table class="data-table">
        <thead>
          <tr>
            <th v-for="col in sortableColumns" :key="col" @click="toggleSort(col)" style="cursor:pointer">
              {{ col }}
              <span v-if="sortBy === col">{{ sortOrder === 'asc' ? '▲' : '▼' }}</span>
            </th>
            <th>报告状态</th><th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
            <td v-for="col in columns" :key="col">{{ row[col] ?? '—' }}</td>
            <td>
              <button v-if="row.has_report" class="link" type="button"
                      :data-testid="`report-${row.id}`" @click="openReport(row.id)">
                {{ row.result_mode === 'exact' ? '精确报告' : '估算报告' }}（{{ row.diff_count }} 处差异<span v-if="row.pending_recheck"> · 待重检 {{ row.pending_recheck }}</span>）
              </button>
              <span v-else>未生成</span>
            </td>
            <td class="row-actions">
              <button v-for="action in actions" :key="action" class="link" type="button"
                      :data-testid="`action-${row.id}-${action}`" @click="runAction(action, row)">
                {{ action }}
              </button>
            </td>
          </tr>
          <tr v-if="!rows.length">
            <td :colspan="columns.length + 2" class="empty-state">暂无比对任务，可先登记比对任务</td>
          </tr>
        </tbody>
      </table>

      <footer class="page-foot">
        <span>共 {{ total }} 条比对任务 · 第 {{ page }} / {{ totalPages }} 页</span>
        <span class="pager">
          <button class="btn" type="button" data-testid="prev-page" :disabled="page <= 1" @click="reload(page - 1)">上一页</button>
          <button class="btn" type="button" data-testid="next-page" :disabled="page >= totalPages" @click="reload(page + 1)">下一页</button>
          <label>每页
            <select v-model.number="size" data-testid="page-size" @change="reload(1)">
              <option :value="5">5</option><option :value="10">10</option><option :value="20">20</option>
            </select>
          </label>
        </span>
      </footer>
    </div>

    <!-- 时间线视图：保留既有甘特条与日志交互 -->
    <h3 class="section-title">作业时间线</h3>
    <div>
      <div class="filter-bar">
        <label><span>开始日期</span><input type="date" v-model="dateFrom" /></label>
        <label><span>结束日期</span><input type="date" v-model="dateTo" /></label>
        <button class="btn" type="button" @click="applyTimelineFilter">查询</button>
      </div>
      <div class="timeline-wrap">
        <div v-for="item in timelineItems" :key="item.id" class="timeline-row">
          <span class="timeline-label">{{ item.name || item.label }}</span>
          <div class="timeline-track">
            <div class="timeline-bar" :class="`status-${item.status}`"
                 :style="{ left: barLeft(item) + '%', width: barWidth(item) + '%' }"
                 :data-testid="`bar-${item.id}`" :title="`${item.start} ~ ${item.end}`"
                 @click="selectItem(item)">&nbsp;</div>
          </div>
          <span style="font-size:12px;color:var(--muted);margin-left:8px;">{{ item.start }} ~ {{ item.end }}</span>
        </div>
        <p v-if="!timelineItems.length" style="text-align:center;color:var(--muted)">暂无作业记录</p>
      </div>
      <div v-if="selectedItem" class="timeline-log" data-testid="timeline-log">
        <strong>{{ selectedItem.name || selectedItem.label }}</strong> — 状态：{{ selectedItem.status }}
        <div style="margin-top:4px;">{{ selectedItem.logs || '暂无日志' }}</div>
      </div>
    </div>

    <div v-if="errorMessage" class="page-foot"><span class="error-text" data-testid="error-message">{{ errorMessage }}</span></div>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记比对任务</h3>
        <label v-for="field in createFields" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`"
                 :required="requiredFields.includes(field)" :placeholder="`请输入${field}`" />
        </label>
        <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" type="submit" data-testid="submit-create">提交</button>
        </div>
      </form>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { request } from '@/api/client'

type Row = Record<string, string | number | boolean | null>

const ENDPOINT = '/api/table_compare'
const columns = ['任务编号', '左表名称', '右表名称', '比对字段', '比对方式', '差异阈值', '采样比例', '比对批次']
const sortableColumns = [...columns]
const requiredFields = ['任务编号', '左表名称', '右表名称']
const createFields = columns
const actions = ['启动比对', '确认完成', '取消比对']
const statuses = ['草稿', '比对中', '已完成', '已取消']

const router = useRouter()
const rows = ref<Row[]>([])
const total = ref(0)
const page = ref(1)
const size = ref(10)
const filters = ref<Record<string, string>>({})
const keyword = ref('')
const status = ref('')
const sortBy = ref('')
const sortOrder = ref<'asc' | 'desc'>('asc')
const errorMessage = ref('')
const showCreate = ref(false)
const createValues = ref<Record<string, string>>({})

const timelineItems = ref<any[]>([])
const allTimelineItems = ref<any[]>([])
const selectedItem = ref<any>(null)
const dateFrom = ref('')
const dateTo = ref('')

const totalPages = computed(() => Math.max(1, Math.ceil(total.value / size.value)))

async function reload(targetPage = page.value) {
  page.value = Math.min(Math.max(1, targetPage), totalPages.value || 1)
  errorMessage.value = ''
  const params = new URLSearchParams()
  Object.entries(filters.value).forEach(([key, value]) => {
    if (value.trim()) params.set(key, value.trim())
  })
  if (keyword.value.trim()) params.set('keyword', keyword.value.trim())
  if (status.value) params.set('status', status.value)
  if (sortBy.value) {
    params.set('sort_by', sortBy.value)
    params.set('sort_order', sortOrder.value)
  }
  params.set('page', String(page.value))
  params.set('size', String(size.value))
  try {
    const res = await request(`${ENDPOINT}?${params.toString()}`)
    if (!res.ok) throw new Error('比对任务列表读取失败')
    const payload = await res.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? 0
    if (page.value > totalPages.value) await reload(totalPages.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '比对任务列表读取失败'
  }
}

function resetFilters() {
  filters.value = {}
  keyword.value = ''
  status.value = ''
  sortBy.value = ''
  sortOrder.value = 'asc'
  void reload(1)
}

function toggleSort(column: string) {
  if (sortBy.value === column) {
    sortOrder.value = sortOrder.value === 'asc' ? 'desc' : 'asc'
  } else {
    sortBy.value = column
    sortOrder.value = 'asc'
  }
  void reload(1)
}

function openReport(id: number | string | boolean | null) {
  void router.push(`/table_compare/${Number(id)}/report`)
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const res = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    const payload = await res.json()
    if (!res.ok || !payload.ok) throw new Error(payload.message || '操作失败')
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '比对任务操作失败（差异报告事务已回滚）'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(createFields.map((field) => [field, '']))
  showCreate.value = true
}

async function submitCreate() {
  errorMessage.value = ''
  try {
    const res = await request(ENDPOINT, { method: 'POST', body: JSON.stringify({ values: createValues.value }) })
    const payload = await res.json()
    if (!res.ok || !payload.ok) throw new Error(payload.message || '登记失败')
    showCreate.value = false
    await reload(1)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  }
}

function exportRows() {
  const params = new URLSearchParams()
  if (keyword.value.trim()) params.set('keyword', keyword.value.trim())
  if (status.value) params.set('status', status.value)
  window.open(`${ENDPOINT}/export?${params.toString()}`, '_blank')
}

async function loadTimeline() {
  try {
    const res = await request(`${ENDPOINT}/timeline`)
    const data = await res.json()
    allTimelineItems.value = data.items ?? []
    applyTimelineFilter()
  } catch {
    errorMessage.value = '时间线加载失败'
  }
}

function applyTimelineFilter() {
  let items = allTimelineItems.value
  if (dateFrom.value) items = items.filter((i: any) => i.start >= dateFrom.value)
  if (dateTo.value) items = items.filter((i: any) => i.end <= dateTo.value)
  timelineItems.value = items
  selectedItem.value = null
}

function selectItem(item: any) {
  selectedItem.value = selectedItem.value?.id === item.id ? null : item
}

function barLeft(item: any) {
  const all = allTimelineItems.value.filter((i) => i.start && i.end)
  if (!all.length) return 0
  const min = Math.min(...all.map((i) => new Date(i.start).getTime()))
  const max = Math.max(...all.map((i) => new Date(i.end).getTime()))
  const range = max - min || 86400000
  return ((new Date(item.start).getTime() - min) / range) * 100
}

function barWidth(item: any) {
  const all = allTimelineItems.value.filter((i) => i.start && i.end)
  if (!all.length) return 20
  const min = Math.min(...all.map((i) => new Date(i.start).getTime()))
  const max = Math.max(...all.map((i) => new Date(i.end).getTime()))
  const range = max - min || 86400000
  return Math.max(5, ((new Date(item.end).getTime() - new Date(item.start).getTime()) / range) * 100)
}

onMounted(() => {
  void reload(1)
  void loadTimeline()
})
</script>

<style scoped>
.section-title { font-size: 15px; margin: 18px 0 10px; }
.pager { display: flex; gap: 8px; align-items: center; }
.pager select { padding: 4px; }
</style>
