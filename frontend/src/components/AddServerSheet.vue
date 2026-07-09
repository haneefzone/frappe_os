<template>
  <Teleport to="body">
    <Transition
      enter-active-class="transition duration-150 ease-out"
      enter-from-class="opacity-0"
      leave-active-class="transition duration-150 ease-out"
      leave-to-class="opacity-0"
    >
      <div v-if="open" class="fixed inset-0 z-50 flex justify-end bg-black/60" @click.self="tryClose">
        <Transition
          enter-active-class="transition duration-150 ease-out"
          enter-from-class="translate-x-full"
          leave-active-class="transition duration-150 ease-out"
          leave-to-class="translate-x-full"
          appear
        >
          <aside
            class="flex h-full w-full max-w-[520px] flex-col border-l border-line bg-base shadow-xl"
            role="dialog"
            aria-modal="true"
            aria-label="Add server"
          >
            <!-- Header -->
            <header class="flex items-center justify-between border-b border-line px-5 py-4">
              <div>
                <h2 class="text-section font-semibold text-ink-1">Add server</h2>
                <p class="text-meta text-ink-2">
                  {{ phase === 'form' ? 'Register a managed host and test the connection.' : created?.name }}
                </p>
              </div>
              <button
                type="button"
                class="fdm-focus rounded p-1 text-ink-3 transition hover:bg-raised hover:text-ink-1"
                aria-label="Close"
                @click="tryClose"
              >
                <LucideX class="h-4 w-4" />
              </button>
            </header>

            <!-- Body -->
            <div class="min-h-0 flex-1 overflow-y-auto p-5">
              <!-- PHASE 1: identity + auth wizard -->
              <Wizard
                v-if="phase === 'form'"
                v-model="activeStep"
                :steps="steps"
                submit-label="Create server"
                :can-continue="canContinue"
                :submitting="creating"
                @submit="onCreate"
              >
                <!-- Identity -->
                <template #step-identity>
                  <div class="space-y-4">
                    <Field label="Name" hint="A friendly label, unique across the fleet.">
                      <input v-model.trim="form.name" v-bind="inputAttrs" placeholder="prod-web-01" />
                    </Field>
                    <div class="flex gap-3">
                      <Field label="Hostname or IP" class="flex-1">
                        <input v-model.trim="form.hostname" v-bind="inputAttrs" placeholder="10.0.0.5" />
                      </Field>
                      <Field label="SSH port" class="w-28">
                        <input v-model.number="form.ssh_port" type="number" min="1" max="65535" v-bind="inputAttrs" />
                      </Field>
                    </div>
                    <Field label="Environment">
                      <select v-model="form.env_tag" v-bind="inputAttrs">
                        <option value="dev">Development</option>
                        <option value="staging">Staging</option>
                        <option value="prod">Production</option>
                      </select>
                    </Field>
                    <Field label="Tags" hint="Optional, comma-separated.">
                      <input v-model.trim="tagsText" v-bind="inputAttrs" placeholder="dubai, ssd" />
                    </Field>
                    <Field label="Notes" hint="Optional.">
                      <textarea v-model.trim="form.notes" v-bind="inputAttrs" rows="2" />
                    </Field>
                  </div>
                </template>

                <!-- Auth -->
                <template #step-auth>
                  <div class="space-y-4">
                    <Field label="SSH username" hint="The bench owner — never root (bench refuses root).">
                      <input v-model.trim="cred.username" v-bind="inputAttrs" placeholder="frappe" />
                    </Field>

                    <div>
                      <span class="mb-1.5 block text-meta font-medium uppercase tracking-wide text-ink-2">
                        Authentication method
                      </span>
                      <div class="grid grid-cols-2 gap-2">
                        <button
                          v-for="opt in methodOptions"
                          :key="opt.value"
                          type="button"
                          class="fdm-focus rounded-lg border px-3 py-2 text-left text-label transition"
                          :class="method === opt.value
                            ? 'border-line-strong bg-raised text-ink-1'
                            : 'border-line text-ink-2 hover:border-line-strong'"
                          @click="method = opt.value"
                        >
                          <span class="block font-medium text-ink-1">{{ opt.label }}</span>
                          <span class="block text-meta text-ink-3">{{ opt.hint }}</span>
                        </button>
                      </div>
                    </div>

                    <Field v-if="method === 'paste'" label="Private key">
                      <textarea
                        v-model="cred.private_key"
                        v-bind="inputAttrs"
                        rows="5"
                        class="font-mono"
                        placeholder="-----BEGIN OPENSSH PRIVATE KEY-----"
                      />
                    </Field>

                    <Field v-if="method === 'upload'" label="Private key file">
                      <input type="file" accept=".pem,.key,text/*" @change="onKeyFile" v-bind="inputAttrs" />
                      <p v-if="cred.private_key" class="mt-1 text-meta text-ok">Key loaded ✓</p>
                    </Field>

                    <Field
                      v-if="(method === 'paste' || method === 'upload')"
                      label="Passphrase"
                      hint="Only if the key is encrypted."
                    >
                      <input v-model="cred.passphrase" type="password" v-bind="inputAttrs" autocomplete="off" />
                    </Field>

                    <div
                      v-if="method === 'generate'"
                      class="rounded-lg border border-line bg-surface px-3 py-2.5 text-label text-ink-2"
                    >
                      The platform generates an <span class="text-ink-1">ed25519</span> key pair. The private
                      key is encrypted immediately and never leaves the server; you'll get the public key to
                      install once you create the server.
                    </div>

                    <Field v-if="method === 'password'" label="Password">
                      <input v-model="cred.password" type="password" v-bind="inputAttrs" autocomplete="off" />
                    </Field>

                    <Field label="Sudo mode" hint="How the platform elevates for service checks.">
                      <select v-model="cred.sudo_mode" v-bind="inputAttrs">
                        <option value="nopasswd">Passwordless sudo (recommended)</option>
                        <option value="none">No sudo</option>
                      </select>
                    </Field>

                    <p v-if="formError" class="text-label text-err" role="alert">{{ formError }}</p>
                  </div>
                </template>
              </Wizard>

              <!-- PHASE 2: install pubkey (if generated) + streamed test -->
              <div v-else class="space-y-5">
                <div
                  v-if="created?.generated_public_key"
                  class="space-y-2 rounded-lg border border-line bg-surface p-3"
                >
                  <p class="text-label font-medium text-ink-1">Install this public key on the server</p>
                  <p class="text-meta text-ink-2">
                    Append it to
                    <span class="font-mono">/home/{{ cred.username }}/.ssh/authorized_keys</span>
                    on {{ created.hostname }}, then run the test.
                  </p>
                  <CopyField :value="created.generated_public_key" :mono="true" />
                </div>

                <div>
                  <div class="mb-2 flex items-center justify-between">
                    <span class="text-label font-medium text-ink-1">Connection test</span>
                    <Button
                      v-if="!testing"
                      variant="subtle"
                      theme="gray"
                      size="sm"
                      :label="hasRun ? 'Re-test' : 'Run test'"
                      @click="runTest"
                    />
                  </div>
                  <ul class="divide-y divide-line rounded-lg border border-line">
                    <li
                      v-for="row in rows"
                      :key="row.key"
                      class="flex items-center gap-3 px-3 py-2"
                    >
                      <LucideLoader2 v-if="row.status === 'running'" class="h-3.5 w-3.5 animate-spin text-run" />
                      <StatusDot v-else :status="dotOf(row.status)" />
                      <span class="text-label text-ink-1">{{ row.label }}</span>
                      <span class="ml-auto truncate text-meta text-ink-2" :title="row.value ?? ''">
                        {{ row.value ?? (row.status === 'fail' ? 'not found' : '') }}
                      </span>
                    </li>
                  </ul>
                  <p v-if="testError" class="mt-2 text-label text-err" role="alert">{{ testError }}</p>
                </div>
              </div>
            </div>

            <!-- Footer -->
            <footer v-if="phase === 'test'" class="flex justify-end gap-2 border-t border-line px-5 py-3.5">
              <Button variant="subtle" theme="gray" label="View server" @click="finish(true)" />
              <Button variant="solid" theme="gray" label="Done" @click="finish(false)" />
            </footer>
          </aside>
        </Transition>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup lang="ts">
import { Button } from 'frappe-ui'
import { computed, reactive, ref, watch } from 'vue'
import LucideLoader2 from '~icons/lucide/loader-2'
import LucideX from '~icons/lucide/x'
import {
  serversApi,
  streamServerTest,
  type CheckEvent,
  type CredentialInput,
  type ServerCreated,
} from '../api/servers'
import CopyField from './CopyField.vue'
import Field from './SheetField.vue'
import StatusDot from './StatusDot.vue'
import type { Status, WizardStep } from './types'
import Wizard from './Wizard.vue'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ close: []; created: [ServerCreated]; view: [number] }>()

type Method = 'paste' | 'upload' | 'generate' | 'password'
type RowStatus = 'pending' | 'running' | 'ok' | 'fail' | 'skipped'

const steps: WizardStep[] = [
  { key: 'identity', label: 'Identity', description: 'What and where the server is.' },
  { key: 'auth', label: 'Authentication', description: 'How the platform signs in.' },
]

const methodOptions: { value: Method; label: string; hint: string }[] = [
  { value: 'paste', label: 'Paste key', hint: 'An existing private key' },
  { value: 'upload', label: 'Upload key', hint: 'From a .pem/.key file' },
  { value: 'generate', label: 'Generate key', hint: 'ed25519, server-side' },
  { value: 'password', label: 'Password', hint: 'Username + password' },
]

const inputAttrs = {
  class:
    'fdm-focus w-full rounded-lg border border-line bg-surface px-2.5 py-1.5 text-label text-ink-1 placeholder:text-ink-3 focus:border-line-strong',
}

const activeStep = ref(0)
const phase = ref<'form' | 'test'>('form')
const creating = ref(false)
const formError = ref('')

const form = reactive({
  name: '',
  hostname: '',
  ssh_port: 22,
  env_tag: 'dev' as 'prod' | 'staging' | 'dev',
  notes: '' as string,
})
const tagsText = ref('')
const cred = reactive({
  username: 'frappe',
  sudo_mode: 'nopasswd' as 'nopasswd' | 'none',
  private_key: '' as string,
  passphrase: '' as string,
  password: '' as string,
})
const method = ref<Method>('generate')

const created = ref<ServerCreated | null>(null)

const canContinue = computed(() => {
  if (activeStep.value === 0) return !!form.name && !!form.hostname && form.ssh_port > 0
  // auth step
  if (!cred.username) return false
  if (method.value === 'generate') return true
  if (method.value === 'password') return !!cred.password
  return !!cred.private_key // paste / upload
})

function reset() {
  activeStep.value = 0
  phase.value = 'form'
  creating.value = false
  formError.value = ''
  testError.value = ''
  hasRun.value = false
  created.value = null
  Object.assign(form, { name: '', hostname: '', ssh_port: 22, env_tag: 'dev', notes: '' })
  tagsText.value = ''
  Object.assign(cred, {
    username: 'frappe',
    sudo_mode: 'nopasswd',
    private_key: '',
    passphrase: '',
    password: '',
  })
  method.value = 'generate'
  seedRows()
}

watch(
  () => props.open,
  (open) => {
    if (open) reset()
  },
)

function onKeyFile(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0]
  if (!file) return
  const reader = new FileReader()
  reader.onload = () => (cred.private_key = String(reader.result ?? ''))
  reader.readAsText(file)
}

function buildCredential(): CredentialInput {
  if (method.value === 'password') {
    return { username: cred.username, auth_type: 'password', sudo_mode: cred.sudo_mode, password: cred.password }
  }
  const base: CredentialInput = { username: cred.username, auth_type: 'key', sudo_mode: cred.sudo_mode }
  if (method.value === 'generate') return { ...base, generate: true }
  return { ...base, private_key: cred.private_key, passphrase: cred.passphrase || undefined }
}

async function onCreate() {
  creating.value = true
  formError.value = ''
  try {
    const tags = tagsText.value
      .split(',')
      .map((t) => t.trim())
      .filter(Boolean)
    const result = await serversApi.create({
      name: form.name,
      hostname: form.hostname,
      ssh_port: form.ssh_port,
      env_tag: form.env_tag,
      tags,
      notes: form.notes || null,
      credential: buildCredential(),
    })
    created.value = result
    phase.value = 'test'
    // For a pasted/uploaded key or password we can test immediately; for a
    // generated key the user must install the public key first, so wait.
    if (method.value !== 'generate') runTest()
  } catch (error) {
    formError.value = error instanceof Error ? error.message : 'Could not create the server.'
  } finally {
    creating.value = false
  }
}

// -- streamed connection test ------------------------------------------------

const testing = ref(false)
const hasRun = ref(false)
const testError = ref('')

const rows = reactive<{ key: string; label: string; status: RowStatus; value: string | null }[]>([])

const TOOL_KEYS = ['git', 'python3', 'uv', 'node', 'mariadb', 'redis-server', 'wkhtmltopdf', 'bench']

function seedRows() {
  rows.splice(0, rows.length,
    { key: 'ssh', label: 'SSH connection', status: 'pending', value: null },
    { key: 'whoami', label: 'Login user', status: 'pending', value: null },
    { key: 'sudo', label: 'Passwordless sudo', status: 'pending', value: null },
    { key: 'os', label: 'Operating system', status: 'pending', value: null },
    ...TOOL_KEYS.map((t) => ({ key: `tool:${t}`, label: t, status: 'pending' as RowStatus, value: null })),
  )
}

function dotOf(status: RowStatus): Status {
  if (status === 'ok') return 'ok'
  if (status === 'fail') return 'err'
  return 'muted'
}

function applyEvent(ev: CheckEvent) {
  if (ev.check === 'done') return
  if (ev.check === 'error') {
    testError.value = ev.error ?? 'The test reported an error.'
    return
  }
  const key = ev.check === 'tool' ? `tool:${ev.name}` : ev.check
  const row = rows.find((r) => r.key === key)
  if (row) {
    row.status = ev.ok ? 'ok' : 'fail'
    row.value = ev.value ?? null
  }
}

async function runTest() {
  if (!created.value || testing.value) return
  testing.value = true
  hasRun.value = true
  testError.value = ''
  seedRows()
  rows[0].status = 'running'
  try {
    await streamServerTest(created.value.id, applyEvent)
  } catch (error) {
    testError.value = error instanceof Error ? error.message : 'The connection test failed to start.'
  } finally {
    // Any check still pending (e.g. after an SSH failure short-circuit) is skipped.
    for (const row of rows) {
      if (row.status === 'pending' || row.status === 'running') row.status = 'skipped'
    }
    testing.value = false
  }
}

function tryClose() {
  if (!testing.value) emit('close')
}

function finish(viewServer: boolean) {
  const server = created.value
  if (server) emit('created', server)
  if (viewServer && server) emit('view', server.id)
  emit('close')
}
</script>
