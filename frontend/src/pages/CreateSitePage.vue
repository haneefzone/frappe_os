<template>
  <div class="flex h-full flex-col">
    <header class="flex items-center justify-between border-b border-line px-8 py-5">
      <div>
        <h1 class="text-lg font-semibold text-ink-1">Create site</h1>
        <p class="text-meta text-ink-2">
          Run <code class="font-mono">bench new-site</code> non-interactively, with the dev-bench
          Redis dance handled for you.
        </p>
      </div>
      <Button variant="subtle" theme="gray" label="Back to sites" @click="router.push('/sites')" />
    </header>

    <div class="min-h-0 flex-1 overflow-y-auto p-8">
      <div class="mx-auto max-w-2xl">
        <Wizard
          v-model="active"
          :steps="steps"
          submit-label="Create site"
          :can-continue="canContinue"
          :submitting="submitting"
          @submit="submit"
        >
          <!-- 1. Bench -->
          <template #step-bench>
            <EmptyState
              v-if="!benches.length"
              :icon="LucideLayers"
              title="No benches yet"
              message="Discover or create a bench before you can add a site to it."
              cta-label="Go to benches"
              @cta="router.push('/benches')"
            />
            <div v-else class="space-y-2">
              <label
                v-for="b in benches"
                :key="b.id"
                class="fdm-focus-within flex cursor-pointer items-center gap-3 rounded-lg border px-4 py-3 transition"
                :class="form.benchId === b.id ? 'border-line-strong bg-raised' : 'border-line hover:border-line-strong'"
              >
                <input v-model.number="form.benchId" type="radio" name="bench" :value="b.id" class="accent-white" />
                <span class="font-medium text-ink-1">{{ b.name }}</span>
                <EnvironmentBadge v-if="envOf(b)" :env="envOf(b)!" />
                <span class="text-meta text-ink-3">{{ chip(b.frappe_version) }}</span>
                <span class="ml-auto truncate font-mono text-meta text-ink-3">{{ b.path }}</span>
              </label>

              <p
                v-if="selectedBench && !serverHasRootPw"
                class="mt-2 rounded-lg border border-warn/40 bg-warn/10 px-4 py-3 text-label text-warn"
                role="alert"
              >
                The server <strong>{{ serverName }}</strong> has no MariaDB root password set.
                <code>bench new-site</code> needs it — set it on the
                <button type="button" class="fdm-focus rounded underline" @click="router.push(`/servers/${selectedBench.server_id}`)">server settings</button>
                first.
              </p>
            </div>
          </template>

          <!-- 2. Details -->
          <template #step-details>
            <div class="space-y-5">
              <div>
                <label class="mb-1 block text-label font-medium text-ink-1" for="site-name">Site name</label>
                <input
                  id="site-name"
                  v-model.trim="form.name"
                  type="text"
                  placeholder="test1.localhost"
                  class="fdm-focus w-full rounded-lg border border-line bg-base px-3 py-2 font-mono text-label text-ink-1 placeholder:text-ink-3"
                  :aria-invalid="form.name.length > 0 && !nameValid"
                />
                <p class="mt-1 text-meta" :class="form.name.length > 0 && !nameValid ? 'text-err' : 'text-ink-3'">
                  Lowercase letters, digits, and <code>. -</code>; 2–81 chars. This is the site's
                  host name (e.g. <code class="font-mono">test1.localhost</code>).
                </p>
              </div>

              <div>
                <div class="mb-1 flex items-center justify-between">
                  <label class="block text-label font-medium text-ink-1" for="admin-pw">Administrator password</label>
                  <Button variant="subtle" theme="gray" label="Generate" @click="generatePassword">
                    <template #prefix><LucideKeyRound class="h-3.5 w-3.5" /></template>
                  </Button>
                </div>
                <div class="flex items-center gap-2">
                  <input
                    id="admin-pw"
                    v-model="form.adminPassword"
                    :type="showPassword ? 'text' : 'password'"
                    class="fdm-focus w-full rounded-lg border border-line bg-base px-3 py-2 font-mono text-label text-ink-1"
                  />
                  <button
                    type="button"
                    class="fdm-focus rounded p-2 text-ink-3 transition hover:bg-raised hover:text-ink-1"
                    :aria-label="showPassword ? 'Hide password' : 'Show password'"
                    @click="showPassword = !showPassword"
                  >
                    <LucideEyeOff v-if="showPassword" class="h-4 w-4" />
                    <LucideEye v-else class="h-4 w-4" />
                  </button>
                </div>
                <!-- Strength meter -->
                <div class="mt-2 flex items-center gap-2">
                  <div class="flex h-1.5 flex-1 gap-1">
                    <span
                      v-for="i in 4"
                      :key="i"
                      class="flex-1 rounded-full transition"
                      :class="i <= strength.score ? strength.barClass : 'bg-raised'"
                    />
                  </div>
                  <span class="w-16 text-right text-meta" :class="strength.textClass">{{ strength.label }}</span>
                </div>
                <p class="mt-1 text-meta text-ink-3">
                  Sets the site's <code>Administrator</code> login. It is sent over HTTPS and stored
                  encrypted — never shown in job logs.
                </p>
              </div>

              <!-- Environment classification (DOO-988) -->
              <div>
                <fieldset>
                  <legend class="mb-2 text-label font-medium text-ink-1">Environment</legend>
                  <div class="flex gap-3">
                    <label
                      v-for="opt in ENV_OPTIONS"
                      :key="opt.value"
                      class="fdm-focus-within flex flex-1 cursor-pointer flex-col gap-1 rounded-lg border px-3 py-2.5 transition"
                      :class="form.environment === opt.value
                        ? 'border-line-strong bg-raised'
                        : 'border-line hover:border-line-strong'"
                    >
                      <input
                        v-model="form.environment"
                        type="radio"
                        name="environment"
                        :value="opt.value"
                        class="sr-only"
                      />
                      <EnvironmentBadge :env="opt.value" />
                      <span class="mt-0.5 text-meta text-ink-3">{{ opt.desc }}</span>
                    </label>
                  </div>
                  <p v-if="form.environment === 'prod'" class="mt-2 rounded-lg border border-err/40 bg-err/5 px-3 py-2 text-meta text-err">
                    Promoting a prod site requires the <strong>danger</strong> role, typed site-name confirm, and a sign-off.
                  </p>
                </fieldset>
              </div>
            </div>
          </template>

          <!-- 3. Review -->
          <template #step-review>
            <dl class="mb-4 divide-y divide-line rounded-lg border border-line">
              <div class="flex justify-between px-4 py-2.5 text-label">
                <dt class="text-ink-3">Bench</dt>
                <dd class="font-medium text-ink-1">{{ selectedBench?.name }} <span class="font-mono text-ink-3">({{ selectedBench?.path }})</span></dd>
              </div>
              <div class="flex justify-between px-4 py-2.5 text-label">
                <dt class="text-ink-3">Server</dt>
                <dd class="font-medium text-ink-1">{{ serverName }}</dd>
              </div>
              <div class="flex justify-between px-4 py-2.5 text-label">
                <dt class="text-ink-3">Site</dt>
                <dd class="font-mono text-ink-1">{{ form.name }}</dd>
              </div>
              <div class="flex items-center justify-between px-4 py-2.5 text-label">
                <dt class="text-ink-3">Environment</dt>
                <dd><EnvironmentBadge :env="form.environment" /></dd>
              </div>
            </dl>

            <details class="rounded-lg border border-line bg-surface">
              <summary class="fdm-focus cursor-pointer px-4 py-2.5 text-label font-medium text-ink-1">
                Show exact command
              </summary>
              <pre class="overflow-x-auto border-t border-line px-4 py-3 font-mono text-meta text-ink-2">{{ exactCommands }}</pre>
            </details>

            <p class="mt-3 text-meta text-ink-3">
              Passwords are masked here and in every log line. Creating runs one job with zero
              interactive prompts — it never hangs waiting for input.
            </p>
          </template>
        </Wizard>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { Button } from 'frappe-ui'
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import LucideEye from '~icons/lucide/eye'
import LucideEyeOff from '~icons/lucide/eye-off'
import LucideKeyRound from '~icons/lucide/key-round'
import LucideLayers from '~icons/lucide/layers'
import { ApiError } from '../api/client'
import { benchesApi, type Bench } from '../api/benches'
import { serversApi, type EnvTag, type Server } from '../api/servers'
import { sitesApi, type SiteEnvironment } from '../api/sites'
import EmptyState from '../components/EmptyState.vue'
import EnvironmentBadge from '../components/EnvironmentBadge.vue'
import { toast } from '../components/toast'
import type { WizardStep } from '../components/types'
import { versionChip as chip } from '../lib/benches'

const router = useRouter()

// Mirrors the backend SITE_NAME whitelist (^[a-z0-9][a-z0-9.-]{1,80}$).
const NAME_RE = /^[a-z0-9][a-z0-9.-]{1,80}$/

const steps: WizardStep[] = [
  { key: 'bench', label: 'Bench', description: 'Where the site will be created.' },
  { key: 'details', label: 'Details', description: 'Name the site, set its password, and classify its environment.' },
  { key: 'review', label: 'Review', description: 'Confirm the exact command, then create.' },
]

// Environment options shown in the Details step picker.
const ENV_OPTIONS: { value: SiteEnvironment; desc: string }[] = [
  { value: 'dev', desc: 'Local or internal dev bench' },
  { value: 'staging', desc: 'Pre-production / QA' },
  { value: 'prod', desc: 'Serves real users — strict guardrail' },
]

const active = ref(0)
const benches = ref<Bench[]>([])
const servers = ref<Server[]>([])
const showPassword = ref(false)
const submitting = ref(false)

const form = reactive({
  benchId: null as number | null,
  name: '',
  adminPassword: '',
  environment: 'dev' as SiteEnvironment,
})

const nameValid = computed(() => NAME_RE.test(form.name))
const selectedBench = computed(() => benches.value.find((b) => b.id === form.benchId) ?? null)
const selectedServer = computed(() =>
  selectedBench.value ? servers.value.find((s) => s.id === selectedBench.value!.server_id) ?? null : null,
)
const serverName = computed(() => selectedServer.value?.name ?? '')
const serverHasRootPw = computed(() => selectedServer.value?.has_mariadb_root_password ?? false)

function envOf(bench: Bench): EnvTag | null {
  return servers.value.find((s) => s.id === bench.server_id)?.env_tag ?? null
}

const strength = computed(() => {
  const pw = form.adminPassword
  let score = 0
  if (pw.length >= 8) score++
  if (pw.length >= 14) score++
  if (/[a-z]/.test(pw) && /[A-Z]/.test(pw) && /\d/.test(pw)) score++
  if (/[^A-Za-z0-9]/.test(pw)) score++
  if (pw.length === 0) score = 0
  const table = [
    { label: '—', barClass: 'bg-raised', textClass: 'text-ink-3' },
    { label: 'Weak', barClass: 'bg-err', textClass: 'text-err' },
    { label: 'Fair', barClass: 'bg-warn', textClass: 'text-warn' },
    { label: 'Good', barClass: 'bg-warn', textClass: 'text-warn' },
    { label: 'Strong', barClass: 'bg-ok', textClass: 'text-ok' },
  ]
  return { score, ...table[score] }
})

function generatePassword() {
  // A strong 20-char password from a URL-safe alphabet (crypto RNG).
  const alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789!@#$%^&*'
  const bytes = new Uint32Array(20)
  crypto.getRandomValues(bytes)
  form.adminPassword = Array.from(bytes, (n) => alphabet[n % alphabet.length]).join('')
  showPassword.value = true
}

const exactCommands = computed(() => {
  const path = selectedBench.value?.path ?? '<bench>'
  const site = form.name || '<site>'
  return [
    '# Dev bench only: start the bench-owned Redis first (gotcha #3)',
    'redis-server config/redis_queue.conf --daemonize yes',
    'redis-server config/redis_cache.conf --daemonize yes',
    '',
    '# Create the site — non-interactive (gotcha #4), passwords masked',
    `cd ${path} && bench new-site ${site} \\`,
    '  --mariadb-root-username root --mariadb-root-password •••• \\',
    "  --admin-password •••• --mariadb-user-host-login-scope='%'",
    '',
    '# Dev bench only: shut the Redis back down so `bench start` can bind',
    'redis-cli -p 11000 shutdown nosave; redis-cli -p 13000 shutdown nosave',
  ].join('\n')
})

const canContinue = computed(() => {
  switch (active.value) {
    case 0:
      return form.benchId != null && serverHasRootPw.value
    case 1:
      return nameValid.value && form.adminPassword.length > 0
    default:
      return true
  }
})

async function load() {
  try {
    const [bnc, srv] = await Promise.all([benchesApi.list(), serversApi.list()])
    // Only active benches can host a new site.
    benches.value = bnc.filter((b) => b.status === 'active')
    servers.value = srv
  } catch (error) {
    toast.error(error instanceof Error ? error.message : 'Could not load benches.')
  }
}

async function submit() {
  if (submitting.value || form.benchId == null) return
  submitting.value = true
  try {
    const job = await sitesApi.create({
      bench_id: form.benchId,
      name: form.name,
      admin_password: form.adminPassword,
      environment: form.environment,
    })
    toast.success('Site creation started.')
    router.push(`/jobs/${job.id}`)
  } catch (error) {
    const message =
      error instanceof ApiError && error.status === 409
        ? 'A site job is already running on this bench.'
        : error instanceof ApiError && error.status === 422
          ? error.message
          : error instanceof Error
            ? error.message
            : 'Could not start site creation.'
    toast.error(message)
    submitting.value = false
  }
}

onMounted(load)
</script>
