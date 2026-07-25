<template>
  <Teleport to="body">
    <Transition
      enter-active-class="transition duration-150 ease-out"
      enter-from-class="opacity-0"
      leave-active-class="transition duration-150 ease-out"
      leave-to-class="opacity-0"
    >
      <div
        v-if="baselineId != null"
        class="fixed inset-0 z-50 flex justify-end bg-black/60"
        @click.self="close"
      >
        <Transition
          enter-active-class="transition duration-150 ease-out"
          enter-from-class="translate-x-full"
          leave-active-class="transition duration-150 ease-out"
          leave-to-class="translate-x-full"
          appear
        >
          <aside
            ref="sheetEl"
            class="flex h-full w-full max-w-[720px] flex-col border-l border-line bg-base"
            role="dialog"
            aria-modal="true"
            aria-label="Config drift detail"
            tabindex="-1"
            @keydown.esc.stop="close"
          >
            <!-- Header -->
            <header class="flex items-start justify-between gap-4 border-b border-line px-5 py-4">
              <div class="min-w-0">
                <div class="flex items-center gap-2.5">
                  <h2 class="text-section font-semibold text-ink-1">Config drift</h2>
                  <StatusBadge v-if="detail" :status="statusBadge" :label="statusLabel" />
                </div>
                <p v-if="detail" class="mt-0.5 font-mono text-meta text-ink-2 truncate" :title="detail.baseline.path">
                  {{ detail.baseline.artifact_key }} — {{ detail.baseline.path }}
                </p>
              </div>
              <button
                type="button"
                class="fdm-focus shrink-0 rounded p-1 text-ink-3 transition hover:bg-raised hover:text-ink-1"
                aria-label="Close"
                @click="close"
              >
                <LucideX class="h-4 w-4" />
              </button>
            </header>

            <!-- Body -->
            <div class="min-h-0 flex-1 overflow-y-auto">
              <!-- Loading -->
              <div v-if="loading" class="space-y-3 p-5">
                <div class="h-4 w-48 animate-pulse rounded bg-raised" />
                <div class="h-48 animate-pulse rounded bg-raised" />
              </div>

              <!-- Error -->
              <p v-else-if="loadError" class="p-5 text-label text-err" role="alert">{{ loadError }}</p>

              <!-- Content -->
              <div v-else-if="detail" class="flex flex-col gap-4 p-5">
                <!-- Meta grid -->
                <dl class="grid grid-cols-2 gap-x-6 gap-y-1.5 rounded-lg border border-line bg-surface px-4 py-3 text-label">
                  <div class="contents">
                    <dt class="text-meta uppercase tracking-wide text-ink-3">Baseline hash</dt>
                    <dd class="truncate font-mono text-ink-1" :title="detail.baseline.sha256 ?? '—'">
                      {{ detail.baseline.sha256 ? detail.baseline.sha256.slice(0, 16) + '…' : '—' }}
                    </dd>
                  </div>
                  <div v-if="detail.baseline.status === 'drifted'" class="contents">
                    <dt class="text-meta uppercase tracking-wide text-ink-3">Current hash</dt>
                    <dd class="truncate font-mono text-ink-1" :title="detail.baseline.current_sha256 ?? '—'">
                      {{ detail.baseline.current_sha256 ? detail.baseline.current_sha256.slice(0, 16) + '…' : 'missing' }}
                    </dd>
                  </div>
                  <div class="contents">
                    <dt class="text-meta uppercase tracking-wide text-ink-3">Captured</dt>
                    <dd class="text-ink-1" :title="absoluteTime(detail.baseline.captured_at)">
                      {{ relativeTime(detail.baseline.captured_at) }}
                    </dd>
                  </div>
                  <div v-if="detail.baseline.last_checked_at" class="contents">
                    <dt class="text-meta uppercase tracking-wide text-ink-3">Last checked</dt>
                    <dd class="text-ink-1" :title="absoluteTime(detail.baseline.last_checked_at)">
                      {{ relativeTime(detail.baseline.last_checked_at) }}
                    </dd>
                  </div>
                  <div v-if="detail.baseline.drift_detected_at" class="contents">
                    <dt class="text-meta uppercase tracking-wide text-ink-3">Drift detected</dt>
                    <dd class="text-ink-1" :title="absoluteTime(detail.baseline.drift_detected_at)">
                      {{ relativeTime(detail.baseline.drift_detected_at) }}
                    </dd>
                  </div>
                  <div class="contents">
                    <dt class="text-meta uppercase tracking-wide text-ink-3">Size</dt>
                    <dd class="text-ink-1">{{ formatBytes(detail.baseline.size) }}</dd>
                  </div>
                </dl>

                <!-- Hash-only message -->
                <div v-if="detail.hash_only" class="rounded-lg border border-line bg-surface px-4 py-3">
                  <p class="flex items-start gap-2 text-label text-ink-2">
                    <LucideShieldOff class="mt-0.5 h-4 w-4 shrink-0 text-ink-3" />
                    Content is not stored for this artifact (it may contain secrets or be binary).
                    Drift is tracked by hash only.
                  </p>
                  <p v-if="detail.baseline.status === 'drifted'" class="mt-1.5 text-label text-err">
                    The hash has changed — the file was modified outside of managed jobs.
                  </p>
                </div>

                <!-- Unified diff viewer -->
                <div v-else-if="detail.unified_diff" class="rounded-lg border border-line overflow-hidden">
                  <div class="border-b border-line bg-raised px-4 py-2 text-meta font-medium text-ink-2">
                    Diff — baseline vs current
                  </div>
                  <div class="overflow-x-auto bg-[var(--bg-base)] font-mono text-meta leading-5">
                    <div
                      v-for="(seg, i) in diffSegments"
                      :key="i"
                      :class="seg.cls"
                      class="whitespace-pre px-4 py-px"
                    >{{ seg.line }}</div>
                  </div>
                </div>

                <!-- No diff (status is baseline/accepted, same content) -->
                <div v-else class="rounded-lg border border-line bg-surface px-4 py-3">
                  <p class="text-label text-ink-2">No changes detected — content matches the baseline.</p>
                </div>
              </div>
            </div>

            <!-- Footer actions -->
            <footer v-if="detail" class="flex items-center gap-2 border-t border-line px-5 py-3.5">
              <template v-if="detail.baseline.status === 'drifted' && canManage">
                <Button
                  variant="solid"
                  theme="gray"
                  label="Accept as new baseline"
                  :loading="accepting"
                  @click="openAccept"
                />
              </template>
              <span
                v-else-if="detail.baseline.status === 'accepted'"
                class="text-label text-ink-3"
              >
                Drift accepted — this state is now the baseline.
              </span>
              <span
                v-else-if="detail.baseline.status === 'baseline'"
                class="text-label text-ink-3"
              >
                Config is in sync with the managed baseline.
              </span>
              <Button
                class="ml-auto"
                variant="subtle"
                theme="gray"
                label="Close"
                @click="close"
              />
            </footer>
          </aside>
        </Transition>
      </div>
    </Transition>
  </Teleport>

  <!-- Accept-as-baseline modal -->
  <Teleport to="body">
    <Transition
      enter-active-class="transition duration-150 ease-out"
      enter-from-class="opacity-0"
      leave-active-class="transition duration-150 ease-out"
      leave-to-class="opacity-0"
    >
      <div
        v-if="acceptOpen"
        class="fixed inset-0 z-[60] flex items-center justify-center bg-black/60 p-4"
        @click.self="acceptOpen = false"
      >
        <div
          ref="acceptPanel"
          role="dialog"
          aria-modal="true"
          aria-label="Accept drift as new baseline"
          tabindex="-1"
          class="fdm-focus w-full max-w-md rounded-lg border border-line bg-raised outline-none"
          @keydown.esc.stop="acceptOpen = false"
        >
          <div class="border-b border-line px-5 py-4">
            <h3 class="text-section font-semibold text-ink-1">Accept as new baseline</h3>
            <p class="mt-0.5 text-label text-ink-2">
              This records the current (drifted) state as the accepted baseline. A reason is
              required for the audit trail.
            </p>
          </div>
          <div class="px-5 py-4 space-y-3">
            <div>
              <label class="mb-1 block text-label font-medium text-ink-2" for="accept-reason">
                Reason <span class="text-err">*</span>
              </label>
              <textarea
                id="accept-reason"
                ref="reasonInput"
                v-model.trim="acceptReason"
                rows="3"
                maxlength="500"
                placeholder="e.g. Manual config tuning for performance — reviewed and approved."
                class="fdm-focus w-full rounded-lg border border-line bg-base px-3 py-2 text-label text-ink-1 placeholder:text-ink-3 focus:border-line-strong resize-none"
              />
              <p class="mt-1 text-right text-meta text-ink-3">{{ acceptReason.length }}/500</p>
            </div>
            <p v-if="acceptError" class="text-label text-err" role="alert">{{ acceptError }}</p>
          </div>
          <div class="flex justify-end gap-2 border-t border-line px-5 py-3.5">
            <Button variant="subtle" theme="gray" label="Cancel" @click="acceptOpen = false" />
            <Button
              variant="solid"
              theme="gray"
              label="Accept baseline"
              :disabled="!acceptReason"
              :loading="accepting"
              @click="confirmAccept"
            />
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup lang="ts">
import { Button } from 'frappe-ui'
import { computed, nextTick, ref, watch } from 'vue'
import LucideShieldOff from '~icons/lucide/shield-off'
import LucideX from '~icons/lucide/x'
import { driftApi, type DriftDiff, type DriftStatus } from '../api/drift'
import StatusBadge from './StatusBadge.vue'
import { toast } from './toast'
import type { Status } from './types'
import { absoluteTime, relativeTime } from '../lib/servers'
import { useAuthStore } from '../stores/auth'

const props = defineProps<{ baselineId: number | null }>()
const emit = defineEmits<{
  close: []
  accepted: [id: number]
}>()

const auth = useAuthStore()
const canManage = auth.hasPermission('server:manage')

const sheetEl = ref<HTMLElement | null>(null)
const acceptPanel = ref<HTMLElement | null>(null)
const reasonInput = ref<HTMLTextAreaElement | null>(null)

const detail = ref<DriftDiff | null>(null)
const loading = ref(false)
const loadError = ref('')

const acceptOpen = ref(false)
const acceptReason = ref('')
const acceptError = ref('')
const accepting = ref(false)

const STATUS_BADGE: Record<DriftStatus, Status> = {
  baseline: 'ok',
  drifted: 'err',
  accepted: 'warn',
}
const STATUS_LABEL_MAP: Record<DriftStatus, string> = {
  baseline: 'In sync',
  drifted: 'Drifted',
  accepted: 'Accepted',
}

const statusBadge = computed<Status>(() =>
  STATUS_BADGE[(detail.value?.baseline.status as DriftStatus) ?? 'baseline'],
)
const statusLabel = computed(
  () => STATUS_LABEL_MAP[(detail.value?.baseline.status as DriftStatus) ?? 'baseline'],
)

interface DiffSeg { line: string; cls: string }

const diffSegments = computed<DiffSeg[]>(() => {
  const raw = detail.value?.unified_diff ?? ''
  if (!raw) return []
  return raw.split('\n').map((line) => {
    if (line.startsWith('+++') || line.startsWith('---')) {
      return { line, cls: 'text-ink-2' }
    }
    if (line.startsWith('@@')) {
      return { line, cls: 'text-run bg-run/10' }
    }
    if (line.startsWith('+')) {
      return { line, cls: 'text-ok bg-ok/10' }
    }
    if (line.startsWith('-')) {
      return { line, cls: 'text-err bg-err/10' }
    }
    return { line, cls: 'text-ink-2' }
  })
})

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}

async function load(id: number) {
  loading.value = true
  loadError.value = ''
  detail.value = null
  try {
    detail.value = await driftApi.get(id)
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : 'Could not load drift detail.'
  } finally {
    loading.value = false
  }
  await nextTick()
  sheetEl.value?.focus()
}

function close() {
  emit('close')
}

function openAccept() {
  acceptReason.value = ''
  acceptError.value = ''
  acceptOpen.value = true
  nextTick(() => reasonInput.value?.focus())
}

async function confirmAccept() {
  if (!acceptReason.value || !detail.value) return
  accepting.value = true
  acceptError.value = ''
  try {
    await driftApi.accept(detail.value.baseline.id, acceptReason.value)
    toast.success('Drift accepted as new baseline.')
    acceptOpen.value = false
    emit('accepted', detail.value.baseline.id)
    close()
  } catch (e) {
    acceptError.value = e instanceof Error ? e.message : 'Could not accept the drift.'
  } finally {
    accepting.value = false
  }
}

watch(
  () => props.baselineId,
  (id) => {
    if (id != null) void load(id)
    else detail.value = null
  },
)
</script>
