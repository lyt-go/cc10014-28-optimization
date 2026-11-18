<template>
  <section class="page" data-module="table_compare_report">
    <header class="page-head">
      <div>
        <h2>表级比对差异报告</h2>
        <p class="page-desc">报告保留字段级差异、采样比例与重检结果；采样与重检批次一致落库，冲突时全量精确结果优先。</p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" data-testid="back-list" @click="goBack">返回表级比对列表</button>
      </div>
    </header>

    <p v-if="errorMessage" class="error-text" data-testid="report-error">{{ errorMessage }}</p>
    <template v-else-if="report">
      <div class="stat-row">
        <article class="stat-card"><span class="stat-label">结果口径</span>
          <strong class="stat-value" data-testid="result-mode">{{ modeLabel }}</strong></article>
        <article class="stat-card"><span class="stat-label">差异总数</span>
          <strong class="stat-value" data-testid="diff-count">{{ report.diff_count }}</strong></article>
        <article class="stat-card"><span class="stat-label">采样比例</span>
          <strong class="stat-value">{{ Math.round(report.sample_ratio * 100) }}%</strong></article>
        <article class="stat-card"><span class="stat-label">待重检字段</span>
          <strong class="stat-value" data-testid="pending-count">{{ report.recheck_batch?.pending_fields?.length ?? 0 }}</strong></article>
      </div>

      <dl class="report-meta">
        <dt>任务编号</dt><dd>{{ report.task_no }}</dd>
        <dt>左/右表</dt><dd>{{ report.left_table }} ⇄ {{ report.right_table }}</dd>
        <dt>比对批次</dt><dd>{{ report.batch_no || '—' }}</dd>
        <dt>主键字段</dt><dd>{{ report.key_field }}</dd>
        <dt>扫描批次大小</dt><dd>{{ report.recheck_batch?.batch_size }} 行/批</dd>
        <dt>冲突处理次序</dt><dd>{{ report.conflict_policy === 'legacy' ? '兼容旧版：差异率区间+待重检字段优先' : '全量优先：精确差异数+重检通过记录' }}</dd>
        <dt>左表记录</dt><dd>{{ report.left_count }}（仅左 {{ report.only_left_count }}）</dd>
        <dt>右表记录</dt><dd>{{ report.right_count }}（仅右 {{ report.only_right_count }}）</dd>
      </dl>

      <h3>字段级差异</h3>
      <table class="data-table" data-testid="field-diff-table">
        <thead>
          <tr>
            <th>字段</th><th>结果类型</th><th>采样差异率</th><th>差异率区间</th>
            <th>全量字段差异数</th><th>重检通过记录</th><th>重检结论</th><th>差异样例</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="diff in report.field_diffs" :key="diff.field" :data-testid="`field-row-${diff.field}`">
            <td>{{ diff.field }}</td>
            <td>
              <span :class="['tag', diff.estimated ? 'tag-estimate' : 'tag-exact']">
                {{ diff.estimated ? '估算' : '精确' }}
              </span>
            </td>
            <td>{{ diff.sample_diff_rate ?? '—' }}</td>
            <td>{{ diff.diff_rate_range ? `${diff.diff_rate_range[0]} ~ ${diff.diff_rate_range[1]}` : '—' }}</td>
            <td data-testid="exact-diff-count">{{ diff.exact_diff_count ?? '待重检' }}</td>
            <td>{{ diff.passed_pairs != null ? `${diff.passed_pairs}/${diff.checked_pairs}` : '—' }}</td>
            <td>{{ diff.recheck_status }}</td>
            <td>
              <details v-if="diff.samples && diff.samples.length">
                <summary>{{ diff.samples.length }} 条样例</summary>
                <ul class="sample-list">
                  <li v-for="(s, i) in diff.samples" :key="i">
                    主键 {{ s.key }}：左值「{{ s.left_value }}」/ 右值「{{ s.right_value }}」
                  </li>
                </ul>
              </details>
              <span v-else>—</span>
            </td>
          </tr>
        </tbody>
      </table>

      <h3>重检批次</h3>
      <div class="recheck-panel" data-testid="recheck-panel">
        <p>采样 {{ report.recheck_batch?.sampled_pairs ?? 0 }} / 全量 {{ report.recheck_batch?.total_pairs ?? 0 }} 对匹配记录</p>
        <p v-if="report.recheck_batch?.pending_fields?.length">
          待重检字段：<strong>{{ report.recheck_batch.pending_fields.join('、') }}</strong>
        </p>
        <p v-else>所有高风险字段已完成全量重检。</p>
        <button class="btn primary" type="button" data-testid="run-recheck"
                :disabled="rechecking || !report.recheck_batch?.pending_fields?.length"
                @click="runRecheck">
          {{ rechecking ? '重检中…' : '执行全量重检' }}
        </button>
        <table v-if="report.recheck_batch?.passed_records?.length" class="data-table" style="margin-top:10px">
          <thead><tr><th>字段</th><th>检查匹配数</th><th>通过数</th><th>重检时间</th></tr></thead>
          <tbody>
            <tr v-for="(rec, i) in report.recheck_batch.passed_records" :key="i">
              <td>{{ rec.field }}</td><td>{{ rec.checked_pairs }}</td><td>{{ rec.passed_pairs }}</td><td>{{ rec.checked_at }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <h3>质量稽核回写</h3>
      <table v-if="report.quality_notes?.length" class="data-table" data-testid="quality-notes-table">
        <thead><tr><th>稽核规则</th><th>校验表</th><th>校验字段</th><th>稽核总数</th><th>失败数</th><th>回写时间</th></tr></thead>
        <tbody>
          <tr v-for="(note, i) in report.quality_notes" :key="i">
            <td>{{ note.rule_id }}</td><td>{{ note.table }}</td><td>{{ note.field }}</td>
            <td>{{ note.total }}</td><td>{{ note.failed }}</td><td>{{ note.written_at }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else class="empty-state">暂无质量稽核回写记录（稽核只读取已落库报告，不重扫全量明细）。</p>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { request } from '@/api/client'

interface FieldDiff {
  field: string
  estimated: boolean
  sample_diff_rate?: number
  diff_rate_range?: [number, number]
  exact_diff_count?: number
  checked_pairs?: number
  passed_pairs?: number
  recheck_status: string
  samples?: Array<{ key: string; left_value: unknown; right_value: unknown }>
}
interface CompareReport {
  entry_id: number
  task_no: string
  left_table: string
  right_table: string
  batch_no: string
  key_field: string
  result_mode: string
  conflict_policy: string
  sample_ratio: number
  diff_count: number
  left_count: number
  right_count: number
  only_left_count: number
  only_right_count: number
  field_diffs: FieldDiff[]
  recheck_batch: {
    batch_size: number
    sampled_pairs: number
    total_pairs: number
    pending_fields: string[]
    passed_records: Array<{ field: string; checked_pairs: number; passed_pairs: number; checked_at: string }>
  }
  quality_notes: Array<Record<string, unknown>>
}

const route = useRoute()
const router = useRouter()
const report = ref<CompareReport | null>(null)
const errorMessage = ref('')
const rechecking = ref(false)

const entryId = computed(() => Number(route.params.id))
const modeLabel = computed(() => (report.value?.result_mode === 'exact' ? '精确结果（全量）' : '估算结果（采样）'))

async function loadReport() {
  errorMessage.value = ''
  try {
    const res = await request(`/api/table_compare/${entryId.value}/report`)
    if (!res.ok) throw new Error((await res.json()).detail || '报告读取失败')
    report.value = await res.json()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '报告读取失败'
  }
}

async function runRecheck() {
  rechecking.value = true
  try {
    const res = await request(`/api/table_compare/${entryId.value}/recheck`, {
      method: 'POST',
      body: JSON.stringify({ fields: report.value?.recheck_batch.pending_fields ?? [] }),
    })
    const payload = await res.json()
    if (!res.ok || !payload.ok) throw new Error(payload.message || '重检失败')
    await loadReport()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '重检失败'
  } finally {
    rechecking.value = false
  }
}

function goBack() {
  router.push('/table_compare')
}

onMounted(loadReport)
</script>

<style scoped>
.report-meta { display: grid; grid-template-columns: 120px 1fr 120px 1fr; gap: 6px 12px; background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px 16px; margin: 0 0 16px; }
.report-meta dt { color: var(--muted); font-size: 12px; }
.report-meta dd { margin: 0; font-size: 13px; }
h3 { font-size: 15px; margin: 18px 0 8px; }
.tag { padding: 2px 8px; border-radius: 10px; font-size: 12px; }
.tag-estimate { background: #fef3c7; color: #92400e; }
.tag-exact { background: #dcfce7; color: #166534; }
.sample-list { margin: 6px 0 0; padding-left: 16px; font-size: 12px; color: var(--muted); }
.recheck-panel { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px 16px; }
</style>
