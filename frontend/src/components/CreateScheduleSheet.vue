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
            aria-label="New schedule"
          >
            <header class="flex items-center justify-between border-b border-line px-5 py-4">
              <div>
                <h2 class="text-section font-semibold text-ink-1">New schedule</h2>
                <p class="text-meta text-ink-2">
                  Run a backup or retention sweep automatically on a cadence.
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
              <Field label="Name" hint="A label for this schedule.">
                <input v-model.trim="form.name" v-bind="inputAttrs" placeholder="Nightly backup" />
              </Field>

              <Field label="Site" hint="The site this schedule acts on.">
                <select v-model="form.target_id" v-bind="inputAttrs">
                  <option :value="null" disabled>Select a site…</option>
                  <option v-for="s in sites" :key="s.id" :value="s.id">{{ s.name }}</option>
                </select>
              </Field>

              <Field label="Action">
                <select v-model="form.action_name" v-bind="inputAttrs">
                  <option value="site.backup">Backup</option>
                  <option value="backup.retention_sweep">Retention sweep</option>
                </select>
              </Field>

              <label
                v-if="form.action_name === 'site.backup'"
                class="flex items-center gap-2 text-label text-ink-1"
              >
                <input v-model="form.with_files" type="checkbox" />
                Include files (larger, but a complete backup)
              </label>

              <div v-if="form.action_name === 'backup.retention_sweep'" class="grid grid-cols-2 gap-3">
                <Field label="Keep last" hint="Newest N to keep.">
                  <input v-model.number="form.retention_keep_last" v-bind="inputAttrs" type="number" min="1" placeholder="7" />
                </Field>
                <Field label="Keep days" hint="Keep newer than N days.">
                  <input v-model.number="form.retention_keep_days" v-bind="inputAttrs" type="number" min="1" placeholder="30" />
                </Field>
              </div>
              <p v-if="form.action_name === 'backup.retention_sweep'" class="text-meta text-ink-2">
                The newest backup is always kept — a sweep never leaves a site with zero backups.
              </p>

              <Field label="Cadence">
                <div class="flex gap-2">
                  <label class="flex items-center gap-1.5 text-label text-ink-1">
                    <input v-model="mode" type="radio" value="interval" /> Interval
                  </label>
                  <label class="flex items-center gap-1.5 text-label text-ink-1">
                    <input v-model="mode" type="radio" value="cron" /> Cron
                  </label>
                </div>
              </Field>

              <div v-if="mode === 'interval'" class="grid grid-cols-2 gap-3">
                <Field label="Every" hint="How often to run.">
                  <input v-model.number="intervalValue" v-bind="inputAttrs" type="number" min="1" />
                </Field>
                <Field label="Unit">
                  <select v-model="intervalUnit" v-bind="inputAttrs">
                    <option value="3600">Hours</option>
                    <option value="86400">Days</option>
                    <option value="60">Minutes</option>
                  </select>
                </Field>
              </div>

              <template v-else>
                <Field label="Cron expression" hint="5 fields: min hour day month weekday. e.g. 0 2 * * * = 02:00 daily.">
                  <input v-model.trim="form.cron" v-bind="inputAttrs" placeholder="0 2 * * *" class="font-mono" />
                </Field>
                <Field label="Timezone" hint="The cron is read in this timezone.">
                  <input v-model.trim="form.timezone" v-bind="inputAttrs" placeholder="Asia/Dubai" />
                </Field>
              </template>

              <p v-if="submitError" class="text-label text-err" role="alert">{{ submitError }}</p>
            </div>

            <footer class="flex items-center justify-end gap-2 border-t border-line px-5 py-4">
              <Button variant="subtle" theme="gray" label="Cancel" @click="tryClose" />
              <Button
                variant="solid"
                theme="gray"
                label="Create schedule"
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
import { ApiError } from '../api/client'
import { schedulesApi } from '../api/schedules'
import { sitesApi, type Site } from '../api/sites'
import Field from './SheetField.vue'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ close: []; saved: [] }>()

const inputAttrs = {
  class:
    'fdm-focus w-full rounded-lg border border-line bg-surface px-2.5 py-1.5 text-label text-ink-1 placeholder:text-ink-3 focus:border-line-strong',
}

const sites = ref<Site[]>([])
const mode = ref<'interval' | 'cron'>('interval')
const intervalValue = ref(1)
const intervalUnit = ref('86400') // seconds per unit
const submitting = ref(false)
const submitError = ref('')

const form = reactive({
  name: '',
  target_id: null as number | null,
  action_name: 'site.backup' as string,
  with_files: false,
  retention_keep_last: null as number | null,
  retention_keep_days: null as number | null,
  cron: '0 2 * * *',
  timezone: 'Asia/Dubai',
})

const canSubmit = computed(
  () => form.name.length > 0 && form.target_id != null &&
    (mode.value === 'interval' ? intervalValue.value >= 1 : form.cron.length > 0),
)

function reset() {
  submitError.value = ''
  submitting.value = false
  mode.value = 'interval'
  intervalValue.value = 1
  intervalUnit.value = '86400'
  Object.assign(form, {
    name: '', target_id: null, action_name: 'site.backup', with_files: false,
    retention_keep_last: null, retention_keep_days: null, cron: '0 2 * * *',
    timezone: 'Asia/Dubai',
  })
}

watch(
  () => props.open,
  async (open) => {
    if (open) {
      reset()
      try {
        sites.value = await sitesApi.list()
      } catch {
        sites.value = []
      }
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
  const payload: Record<string, unknown> = {
    name: form.name,
    target_type: 'site',
    target_id: form.target_id,
    action_name: form.action_name,
  }
  if (form.action_name === 'site.backup') payload.with_files = form.with_files
  if (form.action_name === 'backup.retention_sweep') {
    payload.retention_keep_last = form.retention_keep_last || null
    payload.retention_keep_days = form.retention_keep_days || null
  }
  if (mode.value === 'interval') {
    payload.interval_seconds = intervalValue.value * Number(intervalUnit.value)
  } else {
    payload.cron = form.cron
    payload.timezone = form.timezone
  }
  try {
    await schedulesApi.create(payload as never)
    emit('saved')
    emit('close')
  } catch (e) {
    submitError.value = e instanceof ApiError ? e.message : 'Could not create the schedule.'
  } finally {
    submitting.value = false
  }
}
</script>
