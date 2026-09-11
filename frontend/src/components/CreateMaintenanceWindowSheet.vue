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
            :aria-label="isEditing ? 'Edit maintenance window' : 'New maintenance window'"
          >
            <header class="flex items-center justify-between border-b border-line px-5 py-4">
              <div>
                <h2 class="text-section font-semibold text-ink-1">
                  {{ isEditing ? 'Edit maintenance window' : 'New maintenance window' }}
                </h2>
                <p class="text-meta text-ink-2">
                  Block dangerous operations on a server during a recurring time window.
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
              <p v-if="error" class="text-label text-err" role="alert">{{ error }}</p>

              <Field label="Name">
                <input v-model.trim="form.name" v-bind="inputAttrs" placeholder="Business hours — no updates" />
              </Field>

              <Field label="Server">
                <select v-model="form.server_id" v-bind="inputAttrs" :disabled="isEditing">
                  <option :value="null" disabled>Select a server…</option>
                  <option v-for="s in servers" :key="s.id" :value="s.id">{{ s.name }}</option>
                </select>
              </Field>

              <Field
                label="Schedule (cron)"
                hint="5-field cron expression in the server timezone. E.g. '0 9 * * 1-5' = weekdays 09:00."
              >
                <input v-model.trim="form.cron" v-bind="inputAttrs" placeholder="0 9 * * 1-5" />
              </Field>

              <Field label="Duration (minutes)" hint="How long the window stays active after each trigger.">
                <input
                  v-model.number="form.duration_minutes"
                  v-bind="inputAttrs"
                  type="number"
                  min="1"
                  max="10080"
                  placeholder="480"
                />
              </Field>

              <Field label="Timezone">
                <input v-model.trim="form.timezone" v-bind="inputAttrs" placeholder="Asia/Dubai" />
              </Field>

              <fieldset class="space-y-2">
                <legend class="text-label font-medium text-ink-2">Block during this window</legend>
                <label
                  v-for="cls in dangerClasses"
                  :key="cls"
                  class="flex cursor-pointer items-center gap-2 text-label text-ink-1"
                >
                  <input
                    type="checkbox"
                    :value="cls"
                    v-model="form.blocked_danger_classes"
                    class="h-4 w-4 rounded border-line accent-ink-1"
                  />
                  {{ DANGER_CLASS_LABELS[cls] ?? cls }}
                  <span class="text-meta text-ink-3">{{ dangerClassHint(cls) }}</span>
                </label>
                <p v-if="form.blocked_danger_classes.length === 0" class="text-meta text-warn">
                  No classes blocked — this window has no effect yet.
                </p>
              </fieldset>

              <label class="flex cursor-pointer items-center gap-2 text-label text-ink-1">
                <input type="checkbox" v-model="form.enabled" class="h-4 w-4 rounded border-line accent-ink-1" />
                Enabled
              </label>
            </div>

            <footer class="flex items-center justify-end gap-2 border-t border-line px-5 py-4">
              <Button variant="subtle" theme="gray" label="Cancel" :disabled="saving" @click="tryClose" />
              <Button
                variant="solid"
                theme="gray"
                :label="isEditing ? 'Save changes' : 'Create window'"
                :disabled="!canSubmit || saving"
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
import { computed, ref, watch } from 'vue'
import LucideX from '~icons/lucide/x'
import { ApiError } from '../api/client'
import {
  DANGER_CLASS_LABELS,
  maintenanceWindowsApi,
  type CreateMaintenanceWindowPayload,
  type DangerClass,
  type MaintenanceWindow,
} from '../api/maintenanceWindows'
import type { Server } from '../api/servers'
import Field from './SheetField.vue'

const props = defineProps<{
  open: boolean
  servers: Server[]
  editing?: MaintenanceWindow | null
}>()

const emit = defineEmits<{
  close: []
  saved: [window: MaintenanceWindow]
}>()

const inputAttrs = {
  class:
    'w-full rounded border border-line bg-surface px-3 py-1.5 text-label text-ink-1 placeholder:text-ink-3 focus:outline-none focus:ring-1 focus:ring-ink-1',
}

const dangerClasses: DangerClass[] = ['update', 'restore', 'production_setup']

function dangerClassHint(cls: DangerClass): string {
  switch (cls) {
    case 'update':
      return '(bench update, clone-to-staging, promote)'
    case 'restore':
      return '(site restore, restore-test)'
    case 'production_setup':
      return '(bench setup production)'
  }
}

const isEditing = computed(() => props.editing != null)
const error = ref('')
const saving = ref(false)

const defaultForm = () => ({
  name: '',
  server_id: null as number | null,
  cron: '0 9 * * 1-5',
  duration_minutes: 480,
  timezone: 'Asia/Dubai',
  blocked_danger_classes: [] as DangerClass[],
  enabled: true,
})

const form = ref(defaultForm())

watch(
  () => props.open,
  (open) => {
    if (!open) return
    if (props.editing) {
      form.value = {
        name: props.editing.name,
        server_id: props.editing.server_id,
        cron: props.editing.cron,
        duration_minutes: props.editing.duration_minutes,
        timezone: props.editing.timezone,
        blocked_danger_classes: [...(props.editing.blocked_danger_classes as DangerClass[])],
        enabled: props.editing.enabled,
      }
    } else {
      form.value = defaultForm()
    }
    error.value = ''
  },
)

const canSubmit = computed(
  () =>
    form.value.name.length > 0 &&
    form.value.server_id != null &&
    form.value.cron.trim().length > 0 &&
    form.value.duration_minutes > 0,
)

function tryClose() {
  if (!saving.value) emit('close')
}

async function submit() {
  if (!canSubmit.value) return
  saving.value = true
  error.value = ''
  try {
    const payload: CreateMaintenanceWindowPayload = {
      name: form.value.name,
      server_id: form.value.server_id!,
      cron: form.value.cron,
      duration_minutes: form.value.duration_minutes,
      timezone: form.value.timezone,
      blocked_danger_classes: form.value.blocked_danger_classes,
      enabled: form.value.enabled,
    }
    const saved = isEditing.value
      ? await maintenanceWindowsApi.update(props.editing!.id, payload)
      : await maintenanceWindowsApi.create(payload)
    emit('saved', saved)
    emit('close')
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : 'Could not save the maintenance window.'
  } finally {
    saving.value = false
  }
}
</script>
