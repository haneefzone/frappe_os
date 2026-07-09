<template>
  <div class="flex h-full flex-col">
    <header class="flex items-center justify-between border-b border-line px-8 py-5">
      <div>
        <h1 class="text-lg font-semibold text-ink-1">Create bench</h1>
        <p class="text-meta text-ink-2">
          Pre-flight the target, run <code class="font-mono">bench init</code>, and register the result.
        </p>
      </div>
      <Button variant="subtle" theme="gray" label="Back to benches" @click="router.push('/benches')" />
    </header>

    <div class="min-h-0 flex-1 overflow-y-auto p-8">
      <div class="mx-auto max-w-2xl">
        <Wizard
          v-model="active"
          :steps="steps"
          submit-label="Create bench"
          :can-continue="canContinue"
          :submitting="submitting"
          @submit="submit"
        >
          <!-- 1. Server -->
          <template #step-server>
            <EmptyState
              v-if="!servers.length"
              :icon="LucideServer"
              title="No servers yet"
              message="Register an Ubuntu server before you can create a bench on it."
              cta-label="Add a server"
              @cta="router.push('/servers')"
            />
            <div v-else class="space-y-2">
              <label
                v-for="s in servers"
                :key="s.id"
                class="fdm-focus-within flex cursor-pointer items-center gap-3 rounded-lg border px-4 py-3 transition"
                :class="form.serverId === s.id ? 'border-line-strong bg-raised' : 'border-line hover:border-line-strong'"
              >
                <input
                  v-model.number="form.serverId"
                  type="radio"
                  name="server"
                  :value="s.id"
                  class="accent-white"
                />
                <StatusDot :status="serverDot(s.status)" />
                <span class="font-medium text-ink-1">{{ s.name }}</span>
                <EnvironmentBadge :env="s.env_tag" />
                <span class="ml-auto font-mono text-meta text-ink-3">{{ s.hostname }}</span>
              </label>
            </div>
          </template>

          <!-- 2. Version -->
          <template #step-version>
            <p v-if="matrixError" class="mb-3 text-label text-err" role="alert">{{ matrixError }}</p>
            <div class="grid gap-3 sm:grid-cols-3">
              <label
                v-for="e in matrix"
                :key="e.major"
                class="fdm-focus-within flex cursor-pointer flex-col gap-2 rounded-lg border px-4 py-4 transition"
                :class="form.version === e.major ? 'border-line-strong bg-raised' : 'border-line hover:border-line-strong'"
              >
                <div class="flex items-center gap-2">
                  <input
                    v-model="form.version"
                    type="radio"
                    name="version"
                    :value="e.major"
                    class="accent-white"
                  />
                  <span class="text-base font-semibold text-ink-1">v{{ e.major }}</span>
                </div>
                <span class="text-meta text-ink-2">{{ e.line }}</span>
                <span class="text-meta text-ink-3">{{ e.tooling }}</span>
              </label>
            </div>
          </template>

          <!-- 3. Details -->
          <template #step-details>
            <div class="space-y-4">
              <div>
                <label class="mb-1 block text-label font-medium text-ink-1" for="bench-name">Bench name</label>
                <input
                  id="bench-name"
                  v-model.trim="form.name"
                  type="text"
                  placeholder="frappe-bench"
                  class="fdm-focus w-full rounded-lg border border-line bg-base px-3 py-2 font-mono text-label text-ink-1 placeholder:text-ink-3"
                  :aria-invalid="form.name.length > 0 && !nameValid"
                />
                <p class="mt-1 text-meta" :class="form.name.length > 0 && !nameValid ? 'text-err' : 'text-ink-3'">
                  Lowercase letters, digits, and <code>. _ -</code>; 2–81 chars. The directory
                  <code class="font-mono">{{ form.path }}/{{ form.name || 'name' }}</code> is created.
                </p>
              </div>
              <div>
                <label class="mb-1 block text-label font-medium text-ink-1" for="bench-path">Parent path</label>
                <input
                  id="bench-path"
                  v-model.trim="form.path"
                  type="text"
                  class="fdm-focus w-full rounded-lg border border-line bg-base px-3 py-2 font-mono text-label text-ink-1"
                  :aria-invalid="!pathValid"
                />
                <p class="mt-1 text-meta" :class="pathValid ? 'text-ink-3' : 'text-err'">
                  Absolute path owned by the bench user (bench refuses to run as root — gotcha #1).
                </p>
              </div>
            </div>
          </template>

          <!-- 4. Pre-flight -->
          <template #step-preflight>
            <div class="mb-3 flex items-center justify-between">
              <p class="text-label text-ink-2">
                Checks for v{{ form.version }} on <span class="font-mono">{{ form.path }}</span>.
              </p>
              <Button
                variant="subtle"
                theme="gray"
                :label="preflighting ? 'Running…' : report ? 'Re-run' : 'Run pre-flight'"
                :loading="preflighting"
                @click="runPreflight"
              >
                <template #prefix><LucideRadar class="h-4 w-4" /></template>
              </Button>
            </div>

            <p v-if="preflightError" class="mb-3 text-label text-err" role="alert">{{ preflightError }}</p>

            <!-- Skeleton while running -->
            <div v-if="preflighting && !report" class="space-y-2">
              <div v-for="i in 6" :key="i" class="h-12 animate-pulse rounded-lg border border-line bg-raised" />
            </div>

            <p v-else-if="!report" class="rounded-lg border border-dashed border-line px-4 py-8 text-center text-meta text-ink-3">
              Run the pre-flight to see whether the target can host this bench.
            </p>

            <div v-else class="space-y-2" aria-live="polite">
              <div
                v-for="c in report.checks"
                :key="c.key"
                class="flex items-start gap-3 rounded-lg border border-line bg-surface px-4 py-3"
              >
                <StatusDot :status="checkDot(c.status)" class="mt-1 shrink-0" />
                <div class="min-w-0">
                  <p class="flex items-center gap-2 text-label font-medium text-ink-1">
                    {{ c.title }}
                    <span v-if="c.status !== 'pass'" class="text-meta uppercase tracking-wide" :class="c.status === 'fail' ? 'text-err' : 'text-warn'">
                      {{ c.status }}<span v-if="c.blocking && c.status === 'fail'"> · blocks init</span>
                    </span>
                  </p>
                  <p class="text-meta text-ink-2">{{ c.detail }}</p>
                </div>
              </div>

              <p
                v-if="report.blocked"
                class="rounded-lg border border-err/40 bg-err/10 px-4 py-3 text-label text-err"
                role="alert"
              >
                A blocking check failed. Fix it on the target and re-run — <code>bench init</code> will not start.
              </p>
              <p
                v-else-if="report.has_warnings"
                class="rounded-lg border border-warn/40 bg-warn/10 px-4 py-3 text-label text-warn"
              >
                Warnings won't block <code>bench init</code>, but resolve them before creating sites.
              </p>
            </div>
          </template>

          <!-- 5. Review -->
          <template #step-review>
            <dl class="mb-4 divide-y divide-line rounded-lg border border-line">
              <div class="flex justify-between px-4 py-2.5 text-label">
                <dt class="text-ink-3">Server</dt>
                <dd class="font-medium text-ink-1">{{ serverName }}</dd>
              </div>
              <div class="flex justify-between px-4 py-2.5 text-label">
                <dt class="text-ink-3">Version</dt>
                <dd class="font-medium text-ink-1">v{{ form.version }} <span class="text-ink-3">({{ selectedEntry?.line }})</span></dd>
              </div>
              <div class="flex justify-between px-4 py-2.5 text-label">
                <dt class="text-ink-3">Bench directory</dt>
                <dd class="font-mono text-ink-1">{{ form.path }}/{{ form.name }}</dd>
              </div>
              <div class="flex justify-between px-4 py-2.5 text-label">
                <dt class="text-ink-3">Pre-flight</dt>
                <dd class="font-medium" :class="report && !report.blocked ? 'text-ok' : 'text-ink-3'">
                  {{ report ? (report.blocked ? 'blocked' : report.has_warnings ? 'passed with warnings' : 'all clear') : 'not run' }}
                </dd>
              </div>
            </dl>

            <details class="rounded-lg border border-line bg-surface">
              <summary class="fdm-focus cursor-pointer px-4 py-2.5 text-label font-medium text-ink-1">
                Show exact commands
              </summary>
              <pre class="overflow-x-auto border-t border-line px-4 py-3 font-mono text-meta text-ink-2">{{ exactCommands }}</pre>
            </details>

            <p class="mt-3 text-meta text-ink-3">
              Creating runs one job: pre-flight → <code>bench init</code> → register. It re-runs the
              pre-flight server-side, so a blocking failure still stops <code>bench init</code>.
            </p>
          </template>
        </Wizard>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { Button } from 'frappe-ui'
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import LucideRadar from '~icons/lucide/radar'
import LucideServer from '~icons/lucide/server'
import { ApiError } from '../api/client'
import {
  benchesApi,
  parsePreflightLine,
  type PreflightReport,
  type PreflightStatus,
  type VersionMatrixEntry,
} from '../api/benches'
import { streamJobLogs } from '../api/jobs'
import { serversApi, type Server } from '../api/servers'
import EmptyState from '../components/EmptyState.vue'
import EnvironmentBadge from '../components/EnvironmentBadge.vue'
import StatusDot from '../components/StatusDot.vue'
import { toast } from '../components/toast'
import type { Status, WizardStep } from '../components/types'
import { statusDot as serverDot } from '../lib/servers'

const router = useRouter()

const NAME_RE = /^[a-z0-9][a-z0-9._-]{1,80}$/
const PATH_RE = /^\/[\w./-]{1,300}$/

const steps: WizardStep[] = [
  { key: 'server', label: 'Server', description: 'Where the bench will be created.' },
  { key: 'version', label: 'Version', description: 'Pick a Frappe version — the matrix sets the toolchain.' },
  { key: 'details', label: 'Details', description: 'Name the bench and choose where it lives.' },
  { key: 'preflight', label: 'Pre-flight', description: 'Verify the target can host this version before we install.' },
  { key: 'review', label: 'Review', description: 'Confirm, then create.' },
]

const active = ref(0)
const servers = ref<Server[]>([])
const matrix = ref<VersionMatrixEntry[]>([])
const matrixError = ref('')

const form = reactive({
  serverId: null as number | null,
  version: '',
  name: '',
  path: '/home/frappe',
})

const preflighting = ref(false)
const preflightError = ref('')
const report = ref<PreflightReport | null>(null)
const submitting = ref(false)

const nameValid = computed(() => NAME_RE.test(form.name))
const pathValid = computed(() => PATH_RE.test(form.path))
const selectedEntry = computed(() => matrix.value.find((e) => e.major === form.version) ?? null)
const serverName = computed(() => servers.value.find((s) => s.id === form.serverId)?.name ?? '')

const exactCommands = computed(() => {
  const branch = selectedEntry.value?.branch ?? `version-${form.version}`
  return [
    '# 1. Pre-flight (read-only probes)',
    'uv --version; node --version; mariadb --version; wkhtmltopdf --version',
    `df -B1 --output=avail ${form.path}`,
    '',
    '# 2. Initialize the bench (long-running)',
    `cd ${form.path} && bench init --frappe-branch ${branch} ${form.name || '<name>'}`,
    '',
    '# 3. Register it in the platform inventory',
    `cd ${form.path}/${form.name || '<name>'} && bench version`,
  ].join('\n')
})

const canContinue = computed(() => {
  switch (active.value) {
    case 0:
      return form.serverId != null
    case 1:
      return form.version !== ''
    case 2:
      return nameValid.value && pathValid.value
    case 3:
      // Warnings may pass; a blocking failure (or no run yet) cannot.
      return report.value != null && !report.value.blocked
    default:
      return true
  }
})

const checkDot = (s: PreflightStatus): Status => (s === 'pass' ? 'ok' : s === 'warn' ? 'warn' : 'err')

// Anything that changes what the pre-flight tested invalidates the result, so a
// stale "all clear" can never let a submit through.
watch(
  () => [form.serverId, form.version, form.path],
  () => {
    report.value = null
    preflightError.value = ''
  },
)

async function load() {
  try {
    const [srv, mtx] = await Promise.all([serversApi.list(), benchesApi.versionMatrix()])
    servers.value = srv
    matrix.value = mtx.entries
    if (mtx.entries.length) form.version = mtx.entries[0].major
  } catch (error) {
    matrixError.value = error instanceof Error ? error.message : 'Could not load the version matrix.'
  }
}

async function runPreflight() {
  if (preflighting.value || form.serverId == null) return
  preflighting.value = true
  preflightError.value = ''
  report.value = null
  try {
    const job = await benchesApi.preflight({
      server_id: form.serverId,
      frappe_version: form.version,
      path: form.path,
    })
    let found: PreflightReport | null = null
    await streamJobLogs(job.id, 0, {
      onLog: (frame) => {
        const parsed = parsePreflightLine(frame.content)
        if (parsed) found = parsed
      },
      onEnd: (info) => {
        if (!found && info.status !== 'success') {
          preflightError.value = `Pre-flight ended: ${info.status}.`
        }
      },
    })
    if (found) report.value = found
    else if (!preflightError.value) preflightError.value = 'Pre-flight produced no result — try again.'
  } catch (error) {
    preflightError.value = error instanceof Error ? error.message : 'Could not run pre-flight.'
  } finally {
    preflighting.value = false
  }
}

// Auto-run the pre-flight the first time the user lands on that step.
watch(active, (i) => {
  if (i === 3 && !report.value && !preflighting.value) runPreflight()
})

async function submit() {
  if (submitting.value || form.serverId == null) return
  submitting.value = true
  try {
    const job = await benchesApi.create({
      server_id: form.serverId,
      frappe_version: form.version,
      name: form.name,
      path: form.path,
    })
    toast.success('Bench creation started.')
    router.push(`/jobs/${job.id}`)
  } catch (error) {
    const message =
      error instanceof ApiError && error.status === 409
        ? 'A create job is already running for this bench.'
        : error instanceof Error
          ? error.message
          : 'Could not start bench creation.'
    toast.error(message)
    submitting.value = false
  }
}

onMounted(load)
</script>
