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
            class="flex h-full w-full max-w-[520px] flex-col border-l border-line bg-base"
            role="dialog"
            aria-modal="true"
            :aria-label="isEdit ? 'Edit agent' : 'Register agent'"
          >
            <header class="flex items-center justify-between border-b border-line px-5 py-4">
              <div>
                <h2 class="text-section font-semibold text-ink-1">
                  {{ isEdit ? 'Edit agent' : 'Register agent' }}
                </h2>
                <p class="text-meta text-ink-2">
                  A scoped AI CLI that runs in one directory, over SSH, on allowed servers only.
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

            <div class="min-h-0 flex-1 space-y-4 overflow-y-auto p-5">
              <Field label="Name" hint="A label for this agent.">
                <input v-model.trim="form.name" v-bind="inputAttrs" placeholder="Claude Code (staging)" />
              </Field>

              <Field label="Kind">
                <select v-model="form.kind" v-bind="inputAttrs">
                  <option value="claude-code">Claude Code</option>
                  <option value="custom">Custom CLI</option>
                </select>
              </Field>

              <Field
                label="Command template"
                hint="The CLI to launch. e.g. `claude` — no shell metacharacters."
              >
                <input
                  v-model.trim="form.command_template"
                  v-bind="inputAttrs"
                  class="fdm-focus w-full rounded-lg border border-line bg-surface px-2.5 py-1.5 font-mono text-label text-ink-1 placeholder:text-ink-3 focus:border-line-strong"
                  placeholder="claude"
                />
              </Field>

              <Field
                label="Working directory"
                hint="Absolute path the agent is scoped to. The session opens here."
              >
                <input
                  v-model.trim="form.working_dir"
                  v-bind="inputAttrs"
                  class="fdm-focus w-full rounded-lg border border-line bg-surface px-2.5 py-1.5 font-mono text-label text-ink-1 placeholder:text-ink-3 focus:border-line-strong"
                  placeholder="/home/frappe/frappe-bench"
                />
              </Field>

              <div class="space-y-2">
                <label class="flex items-center gap-2 text-label text-ink-1">
                  <input v-model="form.read_only" type="checkbox" />
                  Read-only session (changes are reverted automatically)
                </label>
                <label class="flex items-center gap-2 text-label text-ink-1">
                  <input v-model="form.pre_change_backup" type="checkbox" :disabled="!form.read_only" />
                  Pre-change backup (snapshot before the agent runs)
                </label>
                <p
                  v-if="!form.read_only"
                  class="flex items-start gap-2 rounded-lg border border-line bg-surface px-3 py-2 text-meta text-ink-2"
                >
                  <LucideInfo class="mt-0.5 h-3.5 w-3.5 shrink-0 text-ink-3" />
                  A read-write agent requires a pre-change backup so its changes can be rolled back.
                </p>
              </div>

              <Field label="Allowed servers" hint="Sessions can only start on servers you select here.">
                <div
                  v-if="loadingServers"
                  class="rounded-lg border border-line bg-surface px-3 py-2 text-meta text-ink-3"
                >
                  Loading servers…
                </div>
                <div
                  v-else-if="servers.length === 0"
                  class="rounded-lg border border-line bg-surface px-3 py-2 text-meta text-ink-3"
                >
                  No servers are registered yet.
                </div>
                <div
                  v-else
                  class="max-h-48 space-y-1 overflow-y-auto rounded-lg border border-line bg-surface p-2"
                >
                  <label
                    v-for="s in servers"
                    :key="s.id"
                    class="flex items-center gap-2 rounded px-2 py-1 text-label text-ink-1 hover:bg-raised"
                  >
                    <input
                      type="checkbox"
                      :value="s.id"
                      :checked="form.allowed_server_ids.includes(s.id)"
                      @change="toggleServer(s.id)"
                    />
                    <span class="truncate">{{ s.name }}</span>
                    <span class="truncate text-meta text-ink-3">{{ s.hostname }}</span>
                  </label>
                </div>
              </Field>

              <p v-if="submitError" class="text-label text-err" role="alert">{{ submitError }}</p>
            </div>

            <footer class="flex items-center justify-end gap-2 border-t border-line px-5 py-4">
              <Button variant="subtle" theme="gray" label="Cancel" @click="tryClose" />
              <Button
                variant="solid"
                theme="gray"
                :label="isEdit ? 'Save agent' : 'Register agent'"
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
import LucideInfo from '~icons/lucide/info'
import LucideX from '~icons/lucide/x'
import {
  aiAgentsApi,
  type AgentConfig,
  type AgentKind,
  type CreateAgentPayload,
} from '../api/aiAgents'
import { ApiError } from '../api/client'
import { serversApi, type Server } from '../api/servers'
import Field from './SheetField.vue'

const props = defineProps<{ open: boolean; agent?: AgentConfig | null }>()
const emit = defineEmits<{ close: []; saved: [] }>()

const isEdit = computed(() => props.agent != null)

const inputAttrs = {
  class:
    'fdm-focus w-full rounded-lg border border-line bg-surface px-2.5 py-1.5 text-label text-ink-1 placeholder:text-ink-3 focus:border-line-strong',
}

const servers = ref<Server[]>([])
const loadingServers = ref(false)
const submitting = ref(false)
const submitError = ref('')

const form = reactive<CreateAgentPayload>({
  name: '',
  kind: 'claude-code' as AgentKind,
  command_template: 'claude',
  working_dir: '',
  read_only: true,
  pre_change_backup: false,
  allowed_server_ids: [],
})

// Read-write agents must keep a pre-change backup — force it on when read-only
// is turned off (backend returns 422 otherwise; we mirror the rule in the UI).
watch(
  () => form.read_only,
  (readOnly) => {
    if (!readOnly) form.pre_change_backup = true
  },
)

const canSubmit = computed(
  () =>
    form.name.length > 0 &&
    form.command_template.length > 0 &&
    form.working_dir.length > 0,
)

function toggleServer(id: number) {
  const i = form.allowed_server_ids.indexOf(id)
  if (i === -1) form.allowed_server_ids.push(id)
  else form.allowed_server_ids.splice(i, 1)
}

function resetFrom(agent?: AgentConfig | null) {
  submitError.value = ''
  submitting.value = false
  if (agent) {
    Object.assign(form, {
      name: agent.name,
      kind: agent.kind,
      command_template: agent.command_template,
      working_dir: agent.working_dir,
      read_only: agent.read_only,
      pre_change_backup: agent.pre_change_backup,
      allowed_server_ids: [...agent.allowed_server_ids],
    })
  } else {
    Object.assign(form, {
      name: '',
      kind: 'claude-code',
      command_template: 'claude',
      working_dir: '',
      read_only: true,
      pre_change_backup: false,
      allowed_server_ids: [],
    })
  }
}

watch(
  () => props.open,
  async (open) => {
    if (!open) return
    resetFrom(props.agent)
    loadingServers.value = true
    try {
      servers.value = await serversApi.list()
    } catch {
      servers.value = []
    } finally {
      loadingServers.value = false
    }
  },
)

function tryClose() {
  if (!submitting.value) emit('close')
}

async function submit() {
  if (!canSubmit.value) return
  submitting.value = true
  submitError.value = ''
  const payload: CreateAgentPayload = {
    name: form.name,
    kind: form.kind,
    command_template: form.command_template,
    working_dir: form.working_dir,
    read_only: form.read_only,
    pre_change_backup: form.read_only ? form.pre_change_backup : true,
    allowed_server_ids: [...form.allowed_server_ids],
  }
  try {
    if (isEdit.value && props.agent) {
      await aiAgentsApi.update(props.agent.id, payload)
    } else {
      await aiAgentsApi.create(payload)
    }
    emit('saved')
    emit('close')
  } catch (e) {
    submitError.value = e instanceof ApiError ? e.message : 'Could not save the agent.'
  } finally {
    submitting.value = false
  }
}
</script>
