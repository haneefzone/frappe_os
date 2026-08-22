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
            class="flex h-full w-full max-w-[560px] flex-col border-l border-line bg-base"
            role="dialog"
            aria-modal="true"
            :aria-label="isEdit ? 'Edit alert rule' : 'New alert rule'"
          >
            <header class="flex items-center justify-between border-b border-line px-5 py-4">
              <div>
                <h2 class="text-section font-semibold text-ink-1">
                  {{ isEdit ? 'Edit alert rule' : 'New alert rule' }}
                </h2>
                <p class="text-meta text-ink-2">
                  {{ isEdit ? 'Update the rule configuration.' : 'Define a metric threshold that triggers notifications.' }}
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
              <!-- Name -->
              <Field label="Name" hint="A short label for this rule.">
                <input
                  v-model.trim="form.name"
                  v-bind="inputAttrs"
                  placeholder="High CPU on production"
                  maxlength="120"
                />
              </Field>

              <!-- Condition row: metric / comparator / threshold -->
              <div class="grid grid-cols-3 gap-3">
                <Field label="Metric">
                  <select v-model="form.metric" v-bind="inputAttrs">
                    <option v-for="m in METRIC_OPTIONS" :key="m.value" :value="m.value">
                      {{ m.label }}
                    </option>
                  </select>
                </Field>
                <Field label="Comparator">
                  <select v-model="form.comparator" v-bind="inputAttrs">
                    <option v-for="c in COMPARATOR_OPTIONS" :key="c.value" :value="c.value">
                      {{ c.label }}
                    </option>
                  </select>
                </Field>
                <Field label="Threshold" :hint="thresholdHint">
                  <input
                    v-model.number="form.threshold"
                    v-bind="inputAttrs"
                    type="number"
                    step="any"
                    placeholder="85"
                  />
                </Field>
              </div>

              <!-- Scope -->
              <Field label="Scope">
                <div class="flex gap-4">
                  <label class="flex items-center gap-1.5 text-label text-ink-1 cursor-pointer">
                    <input v-model="form.scope" type="radio" value="global" />
                    All servers
                  </label>
                  <label class="flex items-center gap-1.5 text-label text-ink-1 cursor-pointer">
                    <input v-model="form.scope" type="radio" value="server" />
                    One server
                  </label>
                </div>
              </Field>

              <Field v-if="form.scope === 'server'" label="Server">
                <select v-model="form.scope_server_id" v-bind="inputAttrs">
                  <option :value="null" disabled>Select a server…</option>
                  <option v-for="s in servers" :key="s.id" :value="s.id">{{ s.name }}</option>
                </select>
              </Field>

              <!-- Cooldown -->
              <Field label="Cooldown (minutes)" hint="Minimum gap between repeated notifications for the same breach. 0 = no dedup.">
                <input
                  v-model.number="form.cooldown_minutes"
                  v-bind="inputAttrs"
                  type="number"
                  min="0"
                  max="10080"
                  placeholder="30"
                />
              </Field>

              <!-- Channels -->
              <div class="space-y-3 rounded-lg border border-line bg-surface p-4">
                <p class="text-meta font-medium uppercase tracking-wide text-ink-2">Channels</p>
                <p v-if="!form.channel_email && !form.channel_webhook" class="text-meta text-warn">
                  At least one channel must be enabled.
                </p>

                <!-- Email -->
                <div class="space-y-2">
                  <label class="flex items-center gap-2 text-label text-ink-1 cursor-pointer">
                    <input v-model="form.channel_email" type="checkbox" />
                    Email
                  </label>
                  <div v-if="form.channel_email" class="ml-5">
                    <Field label="To" hint="Comma-separated email addresses. Leave blank to notify all active users.">
                      <input
                        v-model.trim="form.email_to"
                        v-bind="inputAttrs"
                        placeholder="ops@example.com, admin@example.com"
                        maxlength="500"
                      />
                    </Field>
                  </div>
                </div>

                <!-- Webhook -->
                <div class="space-y-2">
                  <label class="flex items-center gap-2 text-label text-ink-1 cursor-pointer">
                    <input v-model="form.channel_webhook" type="checkbox" />
                    Webhook
                  </label>
                  <div v-if="form.channel_webhook" class="ml-5 space-y-2">
                    <Field label="URL" hint="HTTPS endpoint to POST the alert payload to.">
                      <input
                        v-model.trim="form.webhook_url"
                        v-bind="inputAttrs"
                        type="url"
                        placeholder="https://hooks.example.com/alert"
                        maxlength="500"
                      />
                    </Field>
                    <Field
                      label="Signing secret"
                      :hint="
                        isEdit && rule?.webhook_secret_set
                          ? 'Leave blank to keep the existing secret, or enter a new value to replace it.'
                          : 'Optional HMAC signing key. Never returned by the API once saved.'
                      "
                    >
                      <input
                        v-model.trim="form.webhook_secret"
                        v-bind="inputAttrs"
                        type="password"
                        autocomplete="new-password"
                        :disabled="clearSecret"
                        :placeholder="isEdit && rule?.webhook_secret_set ? '(current secret kept)' : 'Optional'"
                        maxlength="255"
                      />
                    </Field>
                    <template v-if="isEdit && rule?.webhook_secret_set">
                      <p class="text-meta text-ink-2">A signing secret is currently configured.</p>
                      <label class="flex items-center gap-2 text-label text-ink-1 cursor-pointer">
                        <input
                          v-model="clearSecret"
                          type="checkbox"
                          @change="clearSecret && (form.webhook_secret = '')"
                        />
                        Remove the signing secret
                      </label>
                    </template>
                  </div>
                </div>
              </div>

              <!-- Enabled -->
              <label class="flex items-center gap-2 text-label text-ink-1 cursor-pointer">
                <input v-model="form.enabled" type="checkbox" />
                Enabled — evaluate this rule during monitoring sweeps
              </label>

              <p v-if="submitError" class="text-label text-err" role="alert">{{ submitError }}</p>
            </div>

            <footer class="flex items-center justify-end gap-2 border-t border-line px-5 py-4">
              <Button variant="subtle" theme="gray" label="Cancel" @click="tryClose" />
              <Button
                variant="solid"
                theme="gray"
                :label="isEdit ? 'Save changes' : 'Create rule'"
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
import {
  alertsApi,
  COMPARATOR_OPTIONS,
  METRIC_LABELS,
  METRIC_OPTIONS,
  type AlertMetric,
  type AlertComparator,
  type AlertScope,
  type AlertRule,
  type AlertRuleUpdatePayload,
} from '../api/alerts'
import { serversApi, type Server } from '../api/servers'
import Field from './SheetField.vue'

const props = defineProps<{
  open: boolean
  /** When provided, the sheet is in edit mode. */
  rule?: AlertRule | null
}>()
const emit = defineEmits<{ close: []; saved: [] }>()

const isEdit = computed(() => !!props.rule)

const inputAttrs = {
  class:
    'fdm-focus w-full rounded-lg border border-line bg-surface px-2.5 py-1.5 text-label text-ink-1 placeholder:text-ink-3 focus:border-line-strong',
}

const servers = ref<Server[]>([])
const submitting = ref(false)
const submitError = ref('')
const clearSecret = ref(false)

const form = reactive({
  name: '',
  metric: 'cpu_pct' as AlertMetric,
  comparator: '>' as AlertComparator,
  threshold: 85,
  scope: 'global' as AlertScope,
  scope_server_id: null as number | null,
  cooldown_minutes: 30,
  channel_email: false,
  channel_webhook: false,
  email_to: '',
  webhook_url: '',
  webhook_secret: '',
  enabled: true,
})

const thresholdHint = computed(() => {
  if (form.metric.endsWith('_pct')) return `Percent value, e.g. 85 (${METRIC_LABELS[form.metric]}).`
  if (form.metric === 'load1') return `Load average value, e.g. 1.5 (${METRIC_LABELS[form.metric]}).`
  return 'Numeric value.'
})

const canSubmit = computed(() => {
  if (!form.name) return false
  if (!form.channel_email && !form.channel_webhook) return false
  if (form.scope === 'server' && !form.scope_server_id) return false
  if (form.channel_webhook && !form.webhook_url) return false
  return true
})

function reset() {
  submitError.value = ''
  submitting.value = false
  clearSecret.value = false
  if (props.rule) {
    Object.assign(form, {
      name: props.rule.name,
      metric: props.rule.metric,
      comparator: props.rule.comparator,
      threshold: props.rule.threshold,
      scope: props.rule.scope,
      scope_server_id: props.rule.scope_server_id,
      cooldown_minutes: props.rule.cooldown_minutes,
      channel_email: props.rule.channel_email,
      channel_webhook: props.rule.channel_webhook,
      email_to: props.rule.email_to ?? '',
      webhook_url: props.rule.webhook_url ?? '',
      webhook_secret: '',
      enabled: props.rule.enabled,
    })
  } else {
    Object.assign(form, {
      name: '',
      metric: 'cpu_pct',
      comparator: '>',
      threshold: 85,
      scope: 'global',
      scope_server_id: null,
      cooldown_minutes: 30,
      channel_email: false,
      channel_webhook: false,
      email_to: '',
      webhook_url: '',
      webhook_secret: '',
      enabled: true,
    })
  }
}

watch(
  () => props.open,
  async (open) => {
    if (open) {
      reset()
      try {
        servers.value = await serversApi.list()
      } catch {
        servers.value = []
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
  try {
    if (isEdit.value && props.rule) {
      const payload: AlertRuleUpdatePayload = {
        name: form.name,
        metric: form.metric,
        comparator: form.comparator,
        threshold: form.threshold,
        scope: form.scope,
        scope_server_id: form.scope === 'server' ? form.scope_server_id : null,
        cooldown_minutes: form.cooldown_minutes,
        channel_email: form.channel_email,
        channel_webhook: form.channel_webhook,
        email_to: form.channel_email ? (form.email_to || null) : null,
        webhook_url: form.channel_webhook ? (form.webhook_url || null) : null,
        enabled: form.enabled,
      }
      if (clearSecret.value) payload.webhook_secret = ''
      else if (form.webhook_secret) payload.webhook_secret = form.webhook_secret
      await alertsApi.update(props.rule.id, payload)
    } else {
      await alertsApi.create({
        name: form.name,
        metric: form.metric,
        comparator: form.comparator,
        threshold: form.threshold,
        scope: form.scope,
        scope_server_id: form.scope === 'server' ? form.scope_server_id : null,
        cooldown_minutes: form.cooldown_minutes,
        channel_email: form.channel_email,
        channel_webhook: form.channel_webhook,
        email_to: form.channel_email ? (form.email_to || null) : null,
        webhook_url: form.channel_webhook ? (form.webhook_url || null) : null,
        webhook_secret: form.webhook_secret || null,
        enabled: form.enabled,
      })
    }
    emit('saved')
    emit('close')
  } catch (e) {
    submitError.value = e instanceof ApiError ? e.message : 'Could not save the alert rule.'
  } finally {
    submitting.value = false
  }
}
</script>
