<template>
  <div class="flex min-h-screen flex-col items-center justify-start bg-base px-4 py-10">
    <!-- Header -->
    <div class="mb-8 flex flex-col items-center gap-2">
      <div
        class="flex h-10 w-10 items-center justify-center overflow-hidden rounded-lg border border-line bg-raised"
      >
        <LucideServer class="h-5 w-5 text-ink-1" />
      </div>
      <h1 class="text-page font-semibold text-ink-1">First-run Setup</h1>
      <p class="text-label text-ink-2">Configure your FDM Platform installation</p>
    </div>

    <!-- Wizard card -->
    <div class="w-full max-w-2xl">
      <Wizard
        :steps="steps"
        v-model="activeStep"
        submit-label="Complete setup"
        :can-continue="canContinue"
        :submitting="submitting"
        @submit="handleSubmit"
      >
        <!-- ----------------------------------------------------------------
             Step 1: Welcome & Preflight
        ---------------------------------------------------------------- -->
        <template #step-preflight>
          <div class="space-y-4">
            <p class="text-label text-ink-2">
              Checking your platform prerequisites before configuration.
            </p>

            <div v-if="!preflightRan" class="flex justify-start">
              <Button
                variant="solid"
                theme="gray"
                label="Run preflight checks"
                :loading="preflightRunning"
                @click="runPreflight"
              />
            </div>

            <div v-if="preflightRan" class="space-y-2">
              <div
                v-for="check in preflightChecks"
                :key="check.name"
                class="flex items-start gap-3 rounded-md border px-3 py-2.5"
                :class="check.ok ? 'border-ok/30 bg-ok/5' : 'border-err/30 bg-err/5'"
              >
                <LucideCheckCircle v-if="check.ok" class="mt-0.5 h-4 w-4 shrink-0 text-ok" />
                <LucideXCircle v-else class="mt-0.5 h-4 w-4 shrink-0 text-err" />
                <div class="min-w-0">
                  <p class="text-label font-medium text-ink-1">{{ check.name }}</p>
                  <p class="text-meta text-ink-2">{{ check.detail }}</p>
                  <p v-if="check.hint && !check.ok" class="mt-1 text-meta text-warn">
                    {{ check.hint }}
                  </p>
                </div>
              </div>

              <div v-if="!preflightAllOk" class="mt-3 flex items-center gap-2">
                <Button
                  variant="subtle"
                  theme="gray"
                  size="sm"
                  label="Re-run checks"
                  :loading="preflightRunning"
                  @click="runPreflight"
                />
                <p class="text-meta text-ink-2">Fix the issues above, then re-run.</p>
              </div>
            </div>
          </div>
        </template>

        <!-- ----------------------------------------------------------------
             Step 2: Create admin account
        ---------------------------------------------------------------- -->
        <template #step-admin>
          <div class="space-y-4">
            <label class="block">
              <span class="mb-1.5 block text-label font-medium text-ink-2">Email address</span>
              <input
                v-model="admin.email"
                type="email"
                autocomplete="email"
                placeholder="admin@yourcompany.com"
                v-bind="inputAttrs"
              />
            </label>
            <label class="block">
              <span class="mb-1.5 block text-label font-medium text-ink-2">Full name</span>
              <input
                v-model="admin.fullName"
                type="text"
                autocomplete="name"
                placeholder="Jane Smith"
                v-bind="inputAttrs"
              />
            </label>
            <div>
              <label class="block">
                <span class="mb-1.5 block text-label font-medium text-ink-2">Password</span>
                <input
                  v-model="admin.password"
                  type="password"
                  autocomplete="new-password"
                  placeholder="At least 8 characters"
                  v-bind="inputAttrs"
                  @input="updateStrength"
                />
              </label>
              <!-- Password strength meter -->
              <div v-if="admin.password" class="mt-2">
                <div class="flex gap-1">
                  <div
                    v-for="n in 4"
                    :key="n"
                    class="h-1 flex-1 rounded-full transition-colors"
                    :class="n <= passwordStrength ? strengthColor : 'bg-line'"
                  />
                </div>
                <p class="mt-1 text-meta" :class="strengthTextColor">
                  {{ strengthLabel }}
                </p>
              </div>
            </div>
            <label class="block">
              <span class="mb-1.5 block text-label font-medium text-ink-2">Confirm password</span>
              <input
                v-model="admin.confirmPassword"
                type="password"
                autocomplete="new-password"
                placeholder="Repeat password"
                v-bind="inputAttrs"
              />
              <p
                v-if="admin.confirmPassword && admin.password !== admin.confirmPassword"
                class="mt-1 text-meta text-err"
              >
                Passwords do not match
              </p>
            </label>
          </div>
        </template>

        <!-- ----------------------------------------------------------------
             Step 3: Branding & locale
        ---------------------------------------------------------------- -->
        <template #step-branding>
          <div class="space-y-4">
            <label class="block">
              <span class="mb-1.5 block text-label font-medium text-ink-2">Product name</span>
              <input
                v-model="branding.productName"
                type="text"
                placeholder="FDM Platform"
                maxlength="80"
                v-bind="inputAttrs"
              />
              <p class="mt-1 text-meta text-ink-2">
                Shown in the sidebar, login page, and browser tab.
              </p>
            </label>
            <label class="block">
              <span class="mb-1.5 block text-label font-medium text-ink-2">Default timezone</span>
              <select v-model="branding.defaultTz" v-bind="selectAttrs">
                <option v-for="tz in commonTimezones" :key="tz.value" :value="tz.value">
                  {{ tz.label }}
                </option>
              </select>
              <p class="mt-1 text-meta text-ink-2">
                All timestamps are stored in UTC and displayed in this timezone.
              </p>
            </label>
          </div>
        </template>

        <!-- ----------------------------------------------------------------
             Step 4: Notifications (informational — configured post-setup)
        ---------------------------------------------------------------- -->
        <template #step-notifications>
          <div class="space-y-4">
            <div class="rounded-md border border-line bg-raised px-4 py-3 text-label text-ink-2">
              <p class="font-medium text-ink-1 mb-1">Email notifications</p>
              <p>
                SMTP configuration is available after setup in
                <span class="font-medium text-ink-1">Settings → Notifications</span>.
                You can configure alert recipients, SMTP credentials, and test delivery there once
                you are logged in.
              </p>
            </div>
          </div>
        </template>

        <!-- ----------------------------------------------------------------
             Step 5: Review & complete
        ---------------------------------------------------------------- -->
        <template #step-review>
          <div class="space-y-4">
            <p class="text-label text-ink-2">
              Review your configuration before completing setup.
            </p>

            <!-- Admin section -->
            <div class="rounded-md border border-line bg-raised px-4 py-3">
              <p class="mb-2 text-label font-medium text-ink-1">Admin account</p>
              <dl class="space-y-1 text-label">
                <div class="flex gap-2">
                  <dt class="w-24 shrink-0 text-ink-2">Email</dt>
                  <dd class="text-ink-1">{{ admin.email }}</dd>
                </div>
                <div class="flex gap-2">
                  <dt class="w-24 shrink-0 text-ink-2">Name</dt>
                  <dd class="text-ink-1">{{ admin.fullName }}</dd>
                </div>
              </dl>
            </div>

            <!-- Branding section -->
            <div class="rounded-md border border-line bg-raised px-4 py-3">
              <p class="mb-2 text-label font-medium text-ink-1">Branding & locale</p>
              <dl class="space-y-1 text-label">
                <div class="flex gap-2">
                  <dt class="w-24 shrink-0 text-ink-2">Product</dt>
                  <dd class="text-ink-1">{{ branding.productName }}</dd>
                </div>
                <div class="flex gap-2">
                  <dt class="w-24 shrink-0 text-ink-2">Timezone</dt>
                  <dd class="text-ink-1">{{ branding.defaultTz }}</dd>
                </div>
              </dl>
            </div>

            <!-- Notifications section -->
            <div class="rounded-md border border-line bg-raised px-4 py-3">
              <p class="mb-2 text-label font-medium text-ink-1">Notifications</p>
              <p class="text-label text-ink-2">
                Configure SMTP in Settings → Notifications after setup.
              </p>
            </div>

            <p v-if="submitError" class="rounded-md border border-err/30 bg-err/5 px-3 py-2 text-label text-err">
              {{ submitError }}
            </p>
          </div>
        </template>
      </Wizard>
    </div>
  </div>
</template>

<script setup lang="ts">
import { Button } from 'frappe-ui'
import { computed, ref } from 'vue'
import LucideCheckCircle from '~icons/lucide/check-circle'
import LucideServer from '~icons/lucide/server'
import LucideXCircle from '~icons/lucide/x-circle'
import { bootstrapApi, type PreflightCheck } from '../api/bootstrap'
import Wizard from '../components/Wizard.vue'
import type { WizardStep } from '../components/types'
import { useAuthStore } from '../stores/auth'
import { router } from '../router'

const auth = useAuthStore()

const steps: WizardStep[] = [
  { key: 'preflight', label: 'Preflight', description: 'Check platform prerequisites' },
  { key: 'admin', label: 'Admin account', description: 'Create the first administrator' },
  { key: 'branding', label: 'Branding', description: 'Product name and timezone' },
  { key: 'notifications', label: 'Notifications', description: 'Email alerts (optional)' },
  { key: 'review', label: 'Review', description: 'Review and complete setup' },
]

const activeStep = ref(0)

// ------ Preflight ------
const preflightRan = ref(false)
const preflightRunning = ref(false)
const preflightChecks = ref<PreflightCheck[]>([])
const preflightAllOk = ref(false)

async function runPreflight() {
  preflightRunning.value = true
  try {
    const result = await bootstrapApi.preflight()
    preflightChecks.value = result.checks
    preflightAllOk.value = result.all_ok
    preflightRan.value = true
  } catch {
    preflightChecks.value = [
      {
        name: 'Connection',
        ok: false,
        detail: 'Could not reach the API — is the backend running?',
        hint: 'Run `make dev` to start all services.',
      },
    ]
    preflightAllOk.value = false
    preflightRan.value = true
  } finally {
    preflightRunning.value = false
  }
}

// ------ Admin ------
const admin = ref({ email: '', fullName: '', password: '', confirmPassword: '' })
const passwordStrength = ref(0)

function updateStrength() {
  const p = admin.value.password
  let score = 0
  if (p.length >= 8) score++
  if (/[A-Z]/.test(p)) score++
  if (/[0-9]/.test(p)) score++
  if (/[^A-Za-z0-9]/.test(p)) score++
  passwordStrength.value = score
}

const strengthLabel = computed(() => {
  const labels = ['', 'Weak', 'Fair', 'Good', 'Strong']
  return labels[passwordStrength.value] ?? ''
})
const strengthColor = computed(() => {
  if (passwordStrength.value <= 1) return 'bg-err'
  if (passwordStrength.value === 2) return 'bg-warn'
  return 'bg-ok'
})
const strengthTextColor = computed(() => {
  if (passwordStrength.value <= 1) return 'text-err'
  if (passwordStrength.value === 2) return 'text-warn'
  return 'text-ok'
})

// ------ Branding ------
const branding = ref({ productName: 'FDM Platform', defaultTz: 'Asia/Dubai' })

const commonTimezones = [
  { label: 'Asia/Dubai (UTC+4)', value: 'Asia/Dubai' },
  { label: 'Asia/Riyadh (UTC+3)', value: 'Asia/Riyadh' },
  { label: 'Asia/Kuwait (UTC+3)', value: 'Asia/Kuwait' },
  { label: 'Asia/Bahrain (UTC+3)', value: 'Asia/Bahrain' },
  { label: 'Asia/Muscat (UTC+4)', value: 'Asia/Muscat' },
  { label: 'Asia/Qatar (UTC+3)', value: 'Asia/Qatar' },
  { label: 'Asia/Karachi (UTC+5)', value: 'Asia/Karachi' },
  { label: 'Asia/Kolkata (UTC+5:30)', value: 'Asia/Kolkata' },
  { label: 'Europe/London (UTC+0/+1)', value: 'Europe/London' },
  { label: 'Europe/Amsterdam (UTC+1/+2)', value: 'Europe/Amsterdam' },
  { label: 'America/New_York (UTC-5/-4)', value: 'America/New_York' },
  { label: 'America/Los_Angeles (UTC-8/-7)', value: 'America/Los_Angeles' },
  { label: 'UTC', value: 'UTC' },
]

// ------ canContinue per step ------
const canContinue = computed(() => {
  switch (activeStep.value) {
    case 0: // preflight
      return preflightRan.value && preflightAllOk.value
    case 1: // admin
      return (
        admin.value.email.includes('@') &&
        admin.value.fullName.trim().length > 0 &&
        admin.value.password.length >= 8 &&
        admin.value.password === admin.value.confirmPassword
      )
    case 2: // branding
      return branding.value.productName.trim().length > 0
    case 3: // notifications — always continuable (skippable)
      return true
    case 4: // review
      return true
    default:
      return true
  }
})

// ------ Submit ------
const submitting = ref(false)
const submitError = ref<string | null>(null)

async function handleSubmit() {
  submitting.value = true
  submitError.value = null
  try {
    await bootstrapApi.complete({
      admin: {
        email: admin.value.email.trim(),
        full_name: admin.value.fullName.trim(),
        password: admin.value.password,
      },
      branding: {
        product_name: branding.value.productName.trim(),
        default_tz: branding.value.defaultTz,
      },
    })
    // Backend auto-issues session cookies; sync the auth store then redirect.
    await auth.bootstrap()
    router.push({ path: '/' })
  } catch (err: unknown) {
    const e = err as { detail?: string }
    submitError.value = e?.detail ?? 'Setup failed — please check the server logs and try again.'
  } finally {
    submitting.value = false
  }
}

// ------ Shared input styles ------
const inputAttrs = {
  class:
    'w-full rounded-md border border-line bg-raised px-3 py-2 text-label text-ink-1 placeholder-ink-2 outline-none ring-offset-0 transition focus:border-ink-2 focus:ring-1 focus:ring-ink-2',
}
const selectAttrs = {
  class:
    'w-full rounded-md border border-line bg-raised px-3 py-2 text-label text-ink-1 outline-none transition focus:border-ink-2 focus:ring-1 focus:ring-ink-2',
}
</script>
