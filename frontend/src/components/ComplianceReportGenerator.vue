<template>
  <section class="overflow-hidden rounded-lg border border-line bg-surface">
    <header class="flex items-center gap-2 border-b border-line px-5 py-3">
      <LucideFileBarChart class="h-4 w-4 text-ink-3" />
      <h2 class="text-label font-semibold text-ink-1">Compliance Report Export</h2>
      <span class="ml-1 text-meta text-ink-3">ISO-27001 evidence package</span>
    </header>

    <div class="p-5">
      <div class="flex flex-wrap items-end gap-3">
        <!-- Report type -->
        <div>
          <label for="cr-type" class="mb-1 block text-meta font-medium uppercase tracking-wide text-ink-2">
            Report type
          </label>
          <select id="cr-type" v-model="reportType" :disabled="generating" v-bind="inputAttrs">
            <option value="access">Access / Audit trail</option>
            <option value="backup_evidence">Backup evidence</option>
            <option value="access_review">Access review (users × roles)</option>
          </select>
        </div>

        <!-- Format toggle -->
        <div>
          <label class="mb-1 block text-meta font-medium uppercase tracking-wide text-ink-2">Format</label>
          <div class="flex overflow-hidden rounded-lg border border-line">
            <button
              class="px-3.5 py-1.5 text-label transition-colors focus-visible:outline-2 focus-visible:outline-run"
              :class="format === 'csv' ? 'bg-raised text-ink-1 font-medium' : 'bg-base text-ink-2 hover:text-ink-1'"
              :disabled="generating"
              @click="format = 'csv'"
            >
              CSV
            </button>
            <button
              class="border-l border-line px-3.5 py-1.5 text-label transition-colors focus-visible:outline-2 focus-visible:outline-run"
              :class="format === 'pdf' ? 'bg-raised text-ink-1 font-medium' : 'bg-base text-ink-2 hover:text-ink-1'"
              :disabled="generating"
              @click="format = 'pdf'"
            >
              PDF
            </button>
          </div>
        </div>

        <!-- Date range (optional) -->
        <div>
          <label for="cr-since" class="mb-1 block text-meta font-medium uppercase tracking-wide text-ink-2">
            From (optional)
          </label>
          <input
            id="cr-since"
            v-model="since"
            type="date"
            :disabled="generating"
            v-bind="inputAttrs"
          />
        </div>
        <div>
          <label for="cr-until" class="mb-1 block text-meta font-medium uppercase tracking-wide text-ink-2">
            Until (optional)
          </label>
          <input
            id="cr-until"
            v-model="until"
            type="date"
            :disabled="generating"
            v-bind="inputAttrs"
          />
        </div>

        <!-- Generate button -->
        <Button
          variant="solid"
          theme="gray"
          :label="generating ? 'Generating…' : 'Generate & Download'"
          :loading="generating"
          @click="generate"
        >
          <template #prefix><LucideDownload class="h-4 w-4" /></template>
        </Button>
      </div>

      <!-- Error -->
      <p v-if="genError" class="mt-3 text-label text-err" role="alert">{{ genError }}</p>

      <!-- Hash tamper-evidence display -->
      <div
        v-if="lastHash"
        class="mt-4 flex items-start gap-2 rounded-lg border border-line bg-base px-4 py-3 text-meta"
      >
        <LucideShieldCheck class="mt-0.5 h-4 w-4 shrink-0 text-ok" />
        <div>
          <span class="font-medium text-ink-1">Last export SHA-256:</span>
          <code class="ml-2 break-all font-mono text-ink-2">{{ lastHash }}</code>
          <p class="mt-0.5 text-ink-3">Recorded in the Audit Log for tamper-evidence.</p>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { Button } from 'frappe-ui'
import { ref } from 'vue'
import LucideDownload from '~icons/lucide/download'
import LucideFileBarChart from '~icons/lucide/file-bar-chart'
import LucideShieldCheck from '~icons/lucide/shield-check'
import { type ReportFormat, type ReportType, generateComplianceReport } from '../api/compliance_reports'
import { toast } from './toast'

const inputAttrs = {
  class:
    'fdm-focus rounded-lg border border-line bg-base px-2.5 py-1.5 text-label text-ink-1 placeholder:text-ink-3 focus:border-line-strong disabled:opacity-50',
}

const reportType = ref<ReportType>('access')
const format = ref<ReportFormat>('csv')
const since = ref('')
const until = ref('')
const generating = ref(false)
const genError = ref('')
const lastHash = ref('')

async function generate() {
  if (generating.value) return
  generating.value = true
  genError.value = ''
  lastHash.value = ''

  try {
    const result = await generateComplianceReport({
      report_type: reportType.value,
      format: format.value,
      since: since.value ? `${since.value}T00:00:00Z` : null,
      until: until.value ? `${until.value}T23:59:59Z` : null,
    })
    lastHash.value = result.contentHash
    toast.success(`Report downloaded: ${result.filename}`)
  } catch (error) {
    genError.value = error instanceof Error ? error.message : 'Report generation failed.'
    toast.error(genError.value)
  } finally {
    generating.value = false
  }
}
</script>
