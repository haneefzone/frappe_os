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
            ref="el"
            class="flex h-full w-full max-w-[520px] flex-col border-l border-line bg-base shadow-xl"
            role="dialog"
            aria-modal="true"
            :aria-label="editing ? 'Edit source' : 'Add source'"
          >
            <!-- Header -->
            <header class="flex items-center justify-between border-b border-line px-5 py-4">
              <div>
                <h2 class="text-section font-semibold text-ink-1">
                  {{ editing ? 'Edit source' : 'Add source' }}
                </h2>
                <p class="text-meta text-ink-2">
                  A reusable Frappe app source — marketplace name, or a GitHub/GitLab repo.
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
            <div class="min-h-0 flex-1 space-y-4 overflow-y-auto p-5">
              <Field label="Module name" hint="The Frappe app/module name, e.g. hrms or erpnext.">
                <input v-model.trim="form.name" v-bind="inputAttrs" placeholder="erpnext" />
              </Field>

              <Field
                label="Marketplace name or repo URL"
                hint="A marketplace app name, or a github.com / gitlab.com repository URL."
              >
                <input v-model.trim="form.repo_url" v-bind="inputAttrs" placeholder="https://github.com/frappe/erpnext" />
              </Field>

              <!-- Compatibility banner for non-marketplace repos -->
              <p
                v-if="looksLikeRepo"
                class="rounded-lg border border-warn/40 bg-warn/10 px-3 py-2.5 text-label text-warn"
              >
                Frappe-version compatibility for this repo is unverified — test before installing on
                production.
              </p>

              <label class="flex items-center gap-2 text-label text-ink-1">
                <input v-model="form.is_private" type="checkbox" class="accent-white" />
                Private repository
              </label>

              <Field
                v-if="form.is_private"
                label="Deploy key"
                hint="Write-only — stored encrypted and never shown again."
              >
                <textarea
                  v-model="form.deploy_key"
                  v-bind="inputAttrs"
                  rows="4"
                  class="font-mono"
                  :placeholder="editing && hasDeployKey ? 'Leave blank to keep the current key' : '-----BEGIN OPENSSH PRIVATE KEY-----'"
                />
              </Field>

              <Field label="Notes" hint="Optional.">
                <textarea v-model.trim="form.notes" v-bind="inputAttrs" rows="2" />
              </Field>

              <!-- Fetch branches -->
              <div class="rounded-lg border border-line bg-surface p-3">
                <div class="mb-2 flex items-center justify-between">
                  <span class="text-label font-medium text-ink-1">Default branch</span>
                  <Button
                    variant="subtle"
                    theme="gray"
                    size="sm"
                    :label="fetching ? 'Fetching…' : 'Fetch branches'"
                    :loading="fetching"
                    :disabled="!form.repo_url || serverId == null"
                    @click="fetchBranches"
                  />
                </div>

                <Field label="Fetch via server" hint="Runs git ls-remote from this server.">
                  <select v-model.number="serverId" v-bind="inputAttrs">
                    <option :value="null" disabled>Select a server…</option>
                    <option v-for="s in servers" :key="s.id" :value="s.id">{{ s.name }} ({{ s.hostname }})</option>
                  </select>
                </Field>

                <Field v-if="branches.length" label="Branch" class="mt-3">
                  <select v-model="form.default_branch" v-bind="inputAttrs">
                    <option v-for="b in branches" :key="b" :value="b">{{ b }}</option>
                  </select>
                </Field>
                <input
                  v-else
                  v-model.trim="form.default_branch"
                  v-bind="inputAttrs"
                  class="mt-3 fdm-focus w-full rounded-lg border border-line bg-surface px-2.5 py-1.5 text-label text-ink-1 placeholder:text-ink-3 focus:border-line-strong"
                  placeholder="Default branch, e.g. version-15 (optional)"
                />

                <p v-if="fetchError" class="mt-2 text-label text-err" role="alert">{{ fetchError }}</p>
              </div>

              <p v-if="submitError" class="text-label text-err" role="alert">{{ submitError }}</p>
            </div>

            <!-- Footer -->
            <footer class="flex justify-end gap-2 border-t border-line px-5 py-3.5">
              <Button variant="subtle" theme="gray" label="Cancel" @click="tryClose" />
              <Button
                variant="solid"
                theme="gray"
                :label="editing ? 'Save source' : 'Add source'"
                :loading="submitting"
                :disabled="!canSubmit"
                @click="submit"
              />
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
import LucideX from '~icons/lucide/x'
import { appsApi, parseBranchesLine, type AppSource } from '../api/apps'
import { ApiError } from '../api/client'
import { streamJobLogs } from '../api/jobs'
import { serversApi, type Server } from '../api/servers'
import Field from './SheetField.vue'

const props = defineProps<{
  open: boolean
  /** When set, the sheet edits an existing source instead of creating one. */
  source?: AppSource | null
}>()
const emit = defineEmits<{ close: []; saved: [] }>()

const inputAttrs = {
  class:
    'fdm-focus w-full rounded-lg border border-line bg-surface px-2.5 py-1.5 text-label text-ink-1 placeholder:text-ink-3 focus:border-line-strong',
}

const el = ref<HTMLElement>()

const editing = computed(() => props.source != null)
const hasDeployKey = computed(() => props.source?.has_deploy_key ?? false)

const form = reactive({
  name: '',
  repo_url: '',
  default_branch: '' as string,
  is_private: false,
  deploy_key: '' as string,
  notes: '' as string,
})

const servers = ref<Server[]>([])
const serverId = ref<number | null>(null)
const branches = ref<string[]>([])
const fetching = ref(false)
const fetchError = ref('')
const submitting = ref(false)
const submitError = ref('')

// A marketplace name has no scheme/host; anything URL-shaped is an arbitrary repo.
const looksLikeRepo = computed(
  () => /^(https?:\/\/|git@)/.test(form.repo_url) || form.repo_url.includes('/'),
)

const canSubmit = computed(() => form.name.length > 0 && form.repo_url.length > 0)

function reset() {
  branches.value = []
  fetchError.value = ''
  submitError.value = ''
  fetching.value = false
  submitting.value = false
  const src = props.source
  Object.assign(form, {
    name: src?.name ?? '',
    repo_url: src?.repo_url ?? '',
    default_branch: src?.default_branch ?? '',
    is_private: src?.is_private ?? false,
    deploy_key: '',
    notes: src?.notes ?? '',
  })
}

async function loadServers() {
  try {
    servers.value = await serversApi.list()
    if (serverId.value == null && servers.value.length) serverId.value = servers.value[0].id
  } catch {
    // Non-fatal: the user can still save a source without fetching branches.
  }
}

watch(
  () => props.open,
  (open) => {
    if (open) {
      reset()
      loadServers()
    }
  },
)

async function fetchBranches() {
  if (fetching.value || serverId.value == null || !form.repo_url) return
  fetching.value = true
  fetchError.value = ''
  branches.value = []
  try {
    const job = await appsApi.listBranches({ server_id: serverId.value, repo_url: form.repo_url })
    let found: string[] | null = null
    await streamJobLogs(job.id, 0, {
      onLog: (frame) => {
        const parsed = parseBranchesLine(frame.content)
        if (parsed) found = parsed
      },
      onEnd: (info) => {
        if (!found && info.status !== 'success') {
          fetchError.value = `Branch listing ended: ${info.status}.`
        }
      },
    })
    if (found) {
      branches.value = found
      if (found.length && !found.includes(form.default_branch)) form.default_branch = found[0]
    } else if (!fetchError.value) {
      fetchError.value = 'No branches returned — check the URL and deploy key.'
    }
  } catch (error) {
    fetchError.value = error instanceof Error ? error.message : 'Could not list branches.'
  } finally {
    fetching.value = false
  }
}

async function submit() {
  if (submitting.value || !canSubmit.value) return
  submitting.value = true
  submitError.value = ''
  try {
    const payload = {
      name: form.name,
      repo_url: form.repo_url,
      default_branch: form.default_branch || null,
      is_private: form.is_private,
      // Only send a deploy key when the user actually typed one (write-only).
      deploy_key: form.is_private && form.deploy_key ? form.deploy_key : undefined,
      notes: form.notes || null,
    }
    if (props.source) await appsApi.updateSource(props.source.id, payload)
    else await appsApi.createSource(payload)
    emit('saved')
    emit('close')
  } catch (error) {
    submitError.value =
      error instanceof ApiError && error.status === 409
        ? `A source named "${form.name}" already exists.`
        : error instanceof ApiError && error.status === 422
          ? error.message
          : error instanceof Error
            ? error.message
            : 'Could not save the source.'
  } finally {
    submitting.value = false
  }
}

function tryClose() {
  if (!submitting.value) emit('close')
}
</script>
