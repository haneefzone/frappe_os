<template>
  <div class="flex h-full flex-col">
    <header class="flex items-center justify-between border-b border-line px-8 py-5">
      <div>
        <h1 class="text-lg font-semibold text-ink-1">Security</h1>
        <p class="text-meta text-ink-2">
          Sessions, two-factor authentication, and access policies.
        </p>
      </div>
    </header>

    <div class="min-h-0 flex-1 overflow-y-auto p-8">
      <!-- Tabs -->
      <div class="mb-6 flex gap-1 border-b border-line">
        <button
          v-for="t in tabs"
          :key="t.key"
          type="button"
          class="fdm-focus -mb-px border-b-2 px-3 py-2 text-label font-medium transition-colors"
          :class="
            tab === t.key
              ? 'border-ink-1 text-ink-1'
              : 'border-transparent text-ink-3 hover:text-ink-1'
          "
          @click="tab = t.key"
        >
          {{ t.label }}
        </button>
      </div>

      <!-- ── SESSIONS ──────────────────────────────────────────────────────── -->
      <section v-show="tab === 'sessions'" class="max-w-3xl space-y-5">
        <div class="rounded-lg border border-line bg-surface">
          <div class="flex items-center justify-between border-b border-line px-5 py-3.5">
            <div>
              <h2 class="text-section font-semibold text-ink-1">Active sessions</h2>
              <p class="mt-0.5 text-label text-ink-2">
                Devices and browsers that currently have access to your account.
              </p>
            </div>
            <Button
              v-if="sessions.length > 1"
              variant="subtle"
              theme="red"
              size="sm"
              :label="revokingOthers ? 'Revoking…' : 'Revoke all other sessions'"
              :loading="revokingOthers"
              @click="revokeAllOthers"
            />
          </div>

          <p v-if="sessionsError" class="px-5 py-3 text-label text-err" role="alert">
            {{ sessionsError }}
          </p>

          <div v-if="sessionsLoading" class="p-5 space-y-2">
            <div v-for="i in 2" :key="i" class="h-14 animate-pulse rounded-md bg-raised" />
          </div>

          <ul v-else-if="sessions.length" class="divide-y divide-line">
            <li
              v-for="s in sessions"
              :key="s.id"
              class="flex items-start gap-4 px-5 py-3.5"
            >
              <div class="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-md border border-line bg-raised">
                <LucideMonitor class="h-4 w-4 text-ink-2" />
              </div>
              <div class="min-w-0 flex-1">
                <div class="flex items-center gap-2">
                  <span class="truncate text-label font-medium text-ink-1">
                    {{ s.user_agent ? truncateUA(s.user_agent) : 'Unknown browser' }}
                  </span>
                  <StatusBadge v-if="s.is_current" status="ok" label="This device" />
                </div>
                <p class="mt-0.5 text-meta text-ink-3">
                  <span v-if="s.ip">{{ s.ip }} · </span>
                  Last seen {{ relativeTime(s.last_seen_at) }} · Created {{ relativeTime(s.created_at) }}
                </p>
              </div>
              <Button
                variant="subtle"
                theme="red"
                size="sm"
                label="Revoke"
                :loading="revokingId === s.id"
                :disabled="s.is_current || revokingId === s.id"
                @click="revokeSession(s)"
              />
            </li>
          </ul>

          <EmptyState
            v-else
            :icon="LucideMonitor"
            title="No active sessions"
            message="You have no active sessions right now."
          />
        </div>
      </section>

      <!-- ── TWO-FACTOR AUTH ───────────────────────────────────────────────── -->
      <section v-show="tab === '2fa'" class="max-w-2xl space-y-5">
        <!-- Already enrolled -->
        <div v-if="mfaState === 'enrolled'" class="rounded-lg border border-line bg-surface p-5">
          <div class="flex items-start gap-4">
            <div class="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-ok/10">
              <LucideShieldCheck class="h-5 w-5 text-ok" />
            </div>
            <div class="flex-1">
              <h2 class="text-section font-semibold text-ink-1">
                Two-factor authentication is enabled
              </h2>
              <p class="mt-1 text-label text-ink-2">
                Your account is protected with a time-based one-time password (TOTP).
              </p>
            </div>
          </div>
          <div class="mt-5 flex items-center gap-3">
            <Button
              variant="subtle"
              theme="red"
              size="sm"
              :label="disablingMfa ? 'Disabling…' : 'Disable 2FA'"
              :loading="disablingMfa"
              @click="disableMfa"
            />
          </div>
          <p v-if="mfaError" class="mt-3 text-label text-err" role="alert">{{ mfaError }}</p>
        </div>

        <!-- Not enrolled: invite to set up -->
        <div v-else-if="mfaState === 'not-enrolled'" class="rounded-lg border border-line bg-surface p-5">
          <div class="flex items-start gap-4">
            <div class="flex h-10 w-10 shrink-0 items-center justify-center rounded-full border border-line bg-raised">
              <LucideShield class="h-5 w-5 text-ink-2" />
            </div>
            <div>
              <h2 class="text-section font-semibold text-ink-1">
                Set up two-factor authentication
              </h2>
              <p class="mt-1 text-label text-ink-2">
                Add an extra layer of security by requiring a one-time code from an authenticator
                app (Google Authenticator, Authy, 1Password, etc.) at sign-in.
              </p>
            </div>
          </div>
          <div class="mt-5">
            <Button
              variant="solid"
              theme="gray"
              size="sm"
              :label="settingUpMfa ? 'Starting…' : 'Enable 2FA'"
              :loading="settingUpMfa"
              @click="startMfaSetup"
            />
          </div>
          <p v-if="mfaError" class="mt-3 text-label text-err" role="alert">{{ mfaError }}</p>
        </div>

        <!-- Scanning: show QR code + secret -->
        <div v-else-if="mfaState === 'scanning'" class="rounded-lg border border-line bg-surface p-5">
          <h2 class="text-section font-semibold text-ink-1">Scan with your authenticator app</h2>
          <p class="mt-1 text-label text-ink-2">
            Open your authenticator app and scan the QR code below, or enter the secret manually.
          </p>

          <div class="mt-5 flex flex-col items-center gap-4 sm:flex-row sm:items-start">
            <div
              v-if="qrDataUrl"
              class="shrink-0 overflow-hidden rounded-lg border border-line p-2 bg-white"
            >
              <img :src="qrDataUrl" alt="TOTP QR code" class="h-44 w-44" />
            </div>
            <div v-else class="h-48 w-48 shrink-0 animate-pulse rounded-lg bg-raised" />
            <div class="w-full min-w-0">
              <p class="mb-2 text-meta font-medium uppercase tracking-wide text-ink-3">
                Manual entry secret
              </p>
              <CopyField
                :value="setupData?.secret ?? ''"
                :mono="true"
                class="max-w-full"
              />
              <p class="mt-2 text-meta text-ink-3">
                Can't scan? Copy the secret above and paste it as a "time-based" (TOTP) key in
                your authenticator app.
              </p>
            </div>
          </div>

          <div class="mt-5">
            <Button
              variant="solid"
              theme="gray"
              size="sm"
              label="I've scanned it — next"
              @click="mfaState = 'confirming'"
            />
          </div>
        </div>

        <!-- Confirming: enter the code to prove they scanned it -->
        <div v-else-if="mfaState === 'confirming'" class="rounded-lg border border-line bg-surface p-5">
          <h2 class="text-section font-semibold text-ink-1">Verify your authenticator</h2>
          <p class="mt-1 text-label text-ink-2">
            Enter the 6-digit code currently shown in your authenticator app to confirm setup.
          </p>

          <div class="mt-5">
            <label for="confirm-code" class="mb-1 block text-label font-medium text-ink-2">
              Authenticator code
            </label>
            <input
              id="confirm-code"
              v-model="setupCode"
              type="text"
              inputmode="numeric"
              maxlength="6"
              placeholder="000000"
              class="fdm-focus h-9 w-40 rounded-md border border-line bg-base px-3 font-mono text-body text-ink-1 outline-none transition-colors placeholder:text-ink-3 focus:border-line-strong"
              @keydown.enter.prevent="confirmMfaSetup"
            />
          </div>

          <p v-if="mfaError" class="mt-3 text-label text-err" role="alert">{{ mfaError }}</p>

          <div class="mt-5 flex gap-3">
            <Button
              variant="solid"
              theme="gray"
              size="sm"
              :label="settingUpMfa ? 'Verifying…' : 'Confirm'"
              :loading="settingUpMfa"
              :disabled="setupCode.length !== 6 || settingUpMfa"
              @click="confirmMfaSetup"
            />
            <Button
              variant="subtle"
              theme="gray"
              size="sm"
              label="Back"
              :disabled="settingUpMfa"
              @click="mfaState = 'scanning'"
            />
          </div>
        </div>

        <!-- Codes: show recovery codes once -->
        <div v-else-if="mfaState === 'codes'" class="rounded-lg border border-line bg-surface p-5">
          <div class="flex items-start gap-3">
            <div class="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-warn/10">
              <LucideAlertTriangle class="h-5 w-5 text-warn" />
            </div>
            <div>
              <h2 class="text-section font-semibold text-ink-1">Save your recovery codes</h2>
              <p class="mt-1 text-label text-ink-2">
                These 10 codes can each be used once to sign in if you lose access to your
                authenticator. Store them somewhere safe — they won't be shown again.
              </p>
            </div>
          </div>

          <div class="mt-5 grid grid-cols-2 gap-1.5 rounded-lg border border-line bg-base p-4 font-mono text-label text-ink-1">
            <span v-for="code in recoveryCodes" :key="code">{{ code }}</span>
          </div>

          <div class="mt-4 flex gap-2">
            <Button
              variant="subtle"
              theme="gray"
              size="sm"
              label="Copy all"
              @click="copyAllCodes"
            >
              <template #prefix><LucideCopy class="h-3.5 w-3.5" /></template>
            </Button>
            <Button
              variant="subtle"
              theme="gray"
              size="sm"
              label="Download"
              @click="downloadCodes"
            >
              <template #prefix><LucideDownload class="h-3.5 w-3.5" /></template>
            </Button>
          </div>

          <div class="mt-5">
            <Button
              variant="solid"
              theme="gray"
              size="sm"
              label="Done — I've saved my codes"
              @click="doneMfaSetup"
            />
          </div>
        </div>
      </section>

      <!-- ── FAILED LOGINS (admin) ─────────────────────────────────────────── -->
      <section v-show="tab === 'logins'" class="max-w-3xl space-y-5">
        <div class="rounded-lg border border-line bg-surface">
          <div class="border-b border-line px-5 py-3.5">
            <h2 class="text-section font-semibold text-ink-1">Failed login attempts</h2>
            <p class="mt-0.5 text-label text-ink-2">Recent unsuccessful sign-in attempts across all users.</p>
          </div>

          <p v-if="loginsError" class="px-5 py-3 text-label text-err" role="alert">
            {{ loginsError }}
          </p>

          <div v-if="loginsLoading" class="p-5 space-y-2">
            <div v-for="i in 5" :key="i" class="h-10 animate-pulse rounded-md bg-raised" />
          </div>

          <EmptyState
            v-else-if="loginAttempts.length === 0 && !loginsError"
            :icon="LucideShieldCheck"
            title="No failed attempts"
            message="No failed login attempts recorded recently."
          />

          <div v-else-if="loginAttempts.length" class="overflow-x-auto">
            <table class="w-full text-label">
              <thead>
                <tr class="border-b border-line">
                  <th class="px-5 py-2.5 text-left text-meta font-medium uppercase tracking-wide text-ink-3">Email</th>
                  <th class="px-5 py-2.5 text-left text-meta font-medium uppercase tracking-wide text-ink-3">IP</th>
                  <th class="px-5 py-2.5 text-left text-meta font-medium uppercase tracking-wide text-ink-3">Reason</th>
                  <th class="px-5 py-2.5 text-left text-meta font-medium uppercase tracking-wide text-ink-3">When</th>
                </tr>
              </thead>
              <tbody class="divide-y divide-line">
                <tr v-for="a in loginAttempts" :key="a.id">
                  <td class="px-5 py-2.5 text-ink-1">{{ a.email }}</td>
                  <td class="px-5 py-2.5 font-mono text-ink-2">{{ a.ip ?? '—' }}</td>
                  <td class="px-5 py-2.5 text-ink-2">{{ a.reason }}</td>
                  <td class="px-5 py-2.5 text-ink-3">{{ relativeTime(a.created_at) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </section>

      <!-- ── SECURITY POLICY (all users can read; admins can write) ────────── -->
      <section v-show="tab === 'policy'" class="max-w-2xl space-y-5">
        <p v-if="!canManage" class="rounded-lg border border-line bg-surface px-4 py-2.5 text-label text-ink-2">
          You have read-only access to the security policy. Ask an Admin to make changes.
        </p>

        <div v-if="policyLoading" class="space-y-4">
          <div v-for="i in 4" :key="i" class="h-24 animate-pulse rounded-lg border border-line bg-surface" />
        </div>

        <template v-else-if="localPolicy">
          <!-- Password policy -->
          <div class="rounded-lg border border-line bg-surface p-5">
            <h2 class="text-section font-semibold text-ink-1">Password policy</h2>
            <p class="mt-0.5 text-label text-ink-2">
              Rules applied when users set or change their password.
            </p>
            <div class="mt-4 space-y-4">
              <div>
                <label for="pw-min-len" class="mb-1 block text-meta font-medium uppercase tracking-wide text-ink-2">
                  Minimum length
                </label>
                <input
                  id="pw-min-len"
                  v-model.number="localPolicy.password_min_length"
                  type="number"
                  min="6"
                  max="128"
                  :disabled="!canManage"
                  v-bind="inputAttrs"
                  class="w-24"
                />
                <p class="mt-1 text-meta text-ink-3">Between 6 and 128 characters.</p>
              </div>
              <div class="flex items-center gap-3">
                <input
                  id="pw-complexity"
                  v-model="localPolicy.password_require_complexity"
                  type="checkbox"
                  :disabled="!canManage"
                  class="h-4 w-4 rounded border-line accent-ink-1"
                />
                <label for="pw-complexity" class="text-label text-ink-1">
                  Require uppercase, lowercase, digit, and symbol
                </label>
              </div>
              <div>
                <label for="pw-reuse" class="mb-1 block text-meta font-medium uppercase tracking-wide text-ink-2">
                  Reuse history
                </label>
                <input
                  id="pw-reuse"
                  v-model.number="localPolicy.password_reuse_history"
                  type="number"
                  min="0"
                  max="24"
                  :disabled="!canManage"
                  v-bind="inputAttrs"
                  class="w-24"
                />
                <p class="mt-1 text-meta text-ink-3">
                  Number of previous passwords that cannot be reused (0 = no restriction).
                </p>
              </div>
            </div>
          </div>

          <!-- Session timeouts -->
          <div class="rounded-lg border border-line bg-surface p-5">
            <h2 class="text-section font-semibold text-ink-1">Session timeouts</h2>
            <p class="mt-0.5 text-label text-ink-2">
              Idle timeout ends a session after inactivity; absolute timeout ends it regardless.
            </p>
            <div class="mt-4 grid grid-cols-2 gap-4">
              <div>
                <label for="idle-timeout" class="mb-1 block text-meta font-medium uppercase tracking-wide text-ink-2">
                  Idle timeout (minutes)
                </label>
                <input
                  id="idle-timeout"
                  v-model.number="localPolicy.session_idle_timeout_minutes"
                  type="number"
                  min="1"
                  max="10080"
                  :disabled="!canManage"
                  v-bind="inputAttrs"
                />
                <p class="mt-1 text-meta text-ink-3">Max 10 080 min (1 week).</p>
              </div>
              <div>
                <label for="abs-timeout" class="mb-1 block text-meta font-medium uppercase tracking-wide text-ink-2">
                  Absolute timeout (minutes)
                </label>
                <input
                  id="abs-timeout"
                  v-model.number="localPolicy.session_absolute_timeout_minutes"
                  type="number"
                  min="1"
                  max="43200"
                  :disabled="!canManage"
                  v-bind="inputAttrs"
                />
                <p class="mt-1 text-meta text-ink-3">Max 43 200 min (30 days).</p>
              </div>
            </div>
          </div>

          <!-- IP allowlist -->
          <div class="rounded-lg border border-line bg-surface p-5">
            <h2 class="text-section font-semibold text-ink-1">IP allowlist</h2>
            <p class="mt-0.5 text-label text-ink-2">
              Only logins from these IPs or CIDR ranges are permitted. Leave blank to allow all.
            </p>
            <div class="mt-4">
              <label for="ip-allowlist" class="mb-1 block text-meta font-medium uppercase tracking-wide text-ink-2">
                IPs / CIDRs
              </label>
              <textarea
                id="ip-allowlist"
                v-model="ipAllowlistText"
                :disabled="!canManage"
                rows="4"
                placeholder="192.168.1.0/24&#10;10.0.0.1"
                class="fdm-focus w-full rounded-lg border border-line bg-base px-2.5 py-1.5 font-mono text-label text-ink-1 placeholder:text-ink-3 focus:border-line-strong disabled:opacity-60"
              />
              <p class="mt-1 text-meta text-ink-3">One IP or CIDR per line.</p>
            </div>
          </div>

          <!-- Enforce 2FA per role -->
          <div class="rounded-lg border border-line bg-surface p-5">
            <h2 class="text-section font-semibold text-ink-1">Enforce 2FA per role</h2>
            <p class="mt-0.5 text-label text-ink-2">
              Users in these roles must have 2FA enrolled before any mutating action is allowed.
            </p>
            <div class="mt-4">
              <label for="enforce-2fa-roles" class="mb-1 block text-meta font-medium uppercase tracking-wide text-ink-2">
                Roles
              </label>
              <textarea
                id="enforce-2fa-roles"
                v-model="enforce2faRolesText"
                :disabled="!canManage"
                rows="3"
                placeholder="Admin&#10;Operator"
                class="fdm-focus w-full rounded-lg border border-line bg-base px-2.5 py-1.5 text-label text-ink-1 placeholder:text-ink-3 focus:border-line-strong disabled:opacity-60"
              />
              <p class="mt-1 text-meta text-ink-3">One role name per line. Names are case-sensitive.</p>
            </div>
          </div>

          <p v-if="policyError" class="text-label text-err" role="alert">{{ policyError }}</p>

          <div v-if="canManage" class="flex items-center gap-3">
            <Button
              variant="solid"
              theme="gray"
              :label="savingPolicy ? 'Saving…' : 'Save security policy'"
              :loading="savingPolicy"
              @click="savePolicy"
            />
          </div>
        </template>
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
import { Button } from 'frappe-ui'
import QRCode from 'qrcode'
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'
import LucideAlertTriangle from '~icons/lucide/alert-triangle'
import LucideCopy from '~icons/lucide/copy'
import LucideDownload from '~icons/lucide/download'
import LucideMonitor from '~icons/lucide/monitor'
import LucideShield from '~icons/lucide/shield'
import LucideShieldCheck from '~icons/lucide/shield-check'
import { type SessionOut, mfaApi, securityPolicyApi, sessionsApi } from '../api/auth'
import type { TOTPSetupOut } from '../api/auth'
import { ApiError } from '../api/client'
import CopyField from '../components/CopyField.vue'
import EmptyState from '../components/EmptyState.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { toast } from '../components/toast'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const route = useRoute()
const canManage = computed(() => auth.hasPermission('settings:manage'))

// ── Tabs ───────────────────────────────────────────────────────────────────
type TabKey = 'sessions' | '2fa' | 'logins' | 'policy'
const tabs = computed<{ key: TabKey; label: string }[]>(() => [
  { key: 'sessions', label: 'My sessions' },
  { key: '2fa', label: 'Two-factor auth' },
  { key: 'policy', label: 'Security policy' },
  ...(canManage.value ? ([{ key: 'logins', label: 'Failed logins' }] as const) : []),
])
const tab = ref<TabKey>('sessions')

const inputAttrs = {
  class:
    'fdm-focus w-full rounded-lg border border-line bg-base px-2.5 py-1.5 text-label text-ink-1 placeholder:text-ink-3 focus:border-line-strong disabled:opacity-60',
}

// ── Sessions ───────────────────────────────────────────────────────────────
const sessions = ref<SessionOut[]>([])
const sessionsLoading = ref(false)
const sessionsError = ref('')
const revokingId = ref<number | null>(null)
const revokingOthers = ref(false)

async function loadSessions() {
  sessionsLoading.value = true
  sessionsError.value = ''
  try {
    sessions.value = await sessionsApi.list()
  } catch (e) {
    sessionsError.value = e instanceof ApiError ? e.message : 'Failed to load sessions.'
  } finally {
    sessionsLoading.value = false
  }
}

async function revokeSession(s: SessionOut) {
  revokingId.value = s.id
  try {
    await sessionsApi.revoke(s.id)
    sessions.value = sessions.value.filter((x) => x.id !== s.id)
    toast.success('Session revoked.')
  } catch (e) {
    toast.error(e instanceof ApiError ? e.message : 'Failed to revoke session.')
  } finally {
    revokingId.value = null
  }
}

async function revokeAllOthers() {
  revokingOthers.value = true
  try {
    await sessionsApi.revokeOthers()
    // Keep only the current session in the list
    sessions.value = sessions.value.filter((s) => s.is_current)
    toast.success('All other sessions revoked.')
  } catch (e) {
    toast.error(e instanceof ApiError ? e.message : 'Failed to revoke sessions.')
  } finally {
    revokingOthers.value = false
  }
}

// ── 2FA ────────────────────────────────────────────────────────────────────
type MfaState = 'enrolled' | 'not-enrolled' | 'scanning' | 'confirming' | 'codes'
const mfaState = ref<MfaState>(auth.user?.mfa_enabled ? 'enrolled' : 'not-enrolled')
const setupData = ref<TOTPSetupOut | null>(null)
const qrDataUrl = ref<string | null>(null)
const setupCode = ref('')
const recoveryCodes = ref<string[]>([])
const settingUpMfa = ref(false)
const disablingMfa = ref(false)
const mfaError = ref('')

async function startMfaSetup() {
  settingUpMfa.value = true
  mfaError.value = ''
  try {
    setupData.value = await mfaApi.setup()
    qrDataUrl.value = await QRCode.toDataURL(setupData.value.provisioning_uri, {
      width: 176,
      margin: 2,
      color: { dark: '#000000', light: '#ffffff' },
    })
    mfaState.value = 'scanning'
  } catch (e) {
    mfaError.value = e instanceof ApiError ? e.message : 'Failed to start 2FA setup.'
  } finally {
    settingUpMfa.value = false
  }
}

async function confirmMfaSetup() {
  if (setupCode.value.length !== 6) return
  settingUpMfa.value = true
  mfaError.value = ''
  try {
    const result = await mfaApi.confirm(setupCode.value)
    recoveryCodes.value = result.recovery_codes
    mfaState.value = 'codes'
  } catch (e) {
    mfaError.value =
      e instanceof ApiError ? e.message : 'Failed to confirm 2FA. Check your code and try again.'
  } finally {
    settingUpMfa.value = false
  }
}

async function doneMfaSetup() {
  mfaState.value = 'enrolled'
  setupData.value = null
  qrDataUrl.value = null
  setupCode.value = ''
  recoveryCodes.value = []
  await auth.refreshUser()
}

async function disableMfa() {
  disablingMfa.value = true
  mfaError.value = ''
  try {
    await mfaApi.disable()
    mfaState.value = 'not-enrolled'
    await auth.refreshUser()
    toast.success('Two-factor authentication disabled.')
  } catch (e) {
    mfaError.value = e instanceof ApiError ? e.message : 'Failed to disable 2FA.'
  } finally {
    disablingMfa.value = false
  }
}

async function copyAllCodes() {
  try {
    await navigator.clipboard.writeText(recoveryCodes.value.join('\n'))
    toast.success('Recovery codes copied to clipboard.')
  } catch {
    toast.error('Could not copy — copy them manually.')
  }
}

function downloadCodes() {
  const text = [
    'FDM Platform — 2FA Recovery Codes',
    '',
    'Each code can only be used once. Store these somewhere safe.',
    '',
    ...recoveryCodes.value,
  ].join('\n')
  const blob = new Blob([text], { type: 'text/plain' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = 'fdm-recovery-codes.txt'
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}

// ── Failed logins ──────────────────────────────────────────────────────────
const loginAttempts = ref<import('../api/auth').LoginAttemptOut[]>([])
const loginsLoading = ref(false)
const loginsError = ref('')

async function loadLoginAttempts() {
  if (!canManage.value) return
  loginsLoading.value = true
  loginsError.value = ''
  try {
    loginAttempts.value = await sessionsApi.loginAttempts()
  } catch (e) {
    loginsError.value = e instanceof ApiError ? e.message : 'Failed to load login attempts.'
  } finally {
    loginsLoading.value = false
  }
}

// ── Security policy ────────────────────────────────────────────────────────
const policyLoading = ref(false)
const policyError = ref('')
const savingPolicy = ref(false)

const localPolicy = reactive({
  password_min_length: 12,
  password_require_complexity: true,
  password_reuse_history: 5,
  session_idle_timeout_minutes: 60,
  session_absolute_timeout_minutes: 1440,
})
const ipAllowlistText = ref('')
const enforce2faRolesText = ref('')

async function loadPolicy() {
  policyLoading.value = true
  policyError.value = ''
  try {
    const p = await securityPolicyApi.get()
    localPolicy.password_min_length = p.password_min_length
    localPolicy.password_require_complexity = p.password_require_complexity
    localPolicy.password_reuse_history = p.password_reuse_history
    localPolicy.session_idle_timeout_minutes = p.session_idle_timeout_minutes
    localPolicy.session_absolute_timeout_minutes = p.session_absolute_timeout_minutes
    ipAllowlistText.value = p.ip_allowlist.join('\n')
    enforce2faRolesText.value = p.enforce_2fa_roles.join('\n')
  } catch (e) {
    policyError.value = e instanceof ApiError ? e.message : 'Failed to load security policy.'
  } finally {
    policyLoading.value = false
  }
}

async function savePolicy() {
  savingPolicy.value = true
  policyError.value = ''
  const ip_allowlist = ipAllowlistText.value
    .split('\n')
    .map((s) => s.trim())
    .filter(Boolean)
  const enforce_2fa_roles = enforce2faRolesText.value
    .split('\n')
    .map((s) => s.trim())
    .filter(Boolean)
  try {
    await securityPolicyApi.update({
      ...localPolicy,
      ip_allowlist,
      enforce_2fa_roles,
    })
    toast.success('Security policy saved.')
  } catch (e) {
    policyError.value = e instanceof ApiError ? e.message : 'Failed to save security policy.'
  } finally {
    savingPolicy.value = false
  }
}

// ── Utilities ──────────────────────────────────────────────────────────────
function relativeTime(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime()
  const s = Math.floor(diff / 1000)
  if (s < 60) return 'just now'
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ago`
  return `${Math.floor(h / 24)}d ago`
}

function truncateUA(ua: string): string {
  // Extract just the browser/OS part; full UA strings are verbose.
  const m =
    ua.match(/\(([^)]+)\)/) ?? ua.match(/(Chrome|Firefox|Safari|Edge|Opera)[/\s][\d.]+/i)
  return m ? m[0].substring(0, 60) : ua.substring(0, 60)
}

// ── Lifecycle ──────────────────────────────────────────────────────────────
onMounted(async () => {
  // Support deep-link: /security?tab=2fa (used by the mfa_enrollment_required redirect)
  const queryTab = route.query.tab
  if (queryTab && ['sessions', '2fa', 'logins', 'policy'].includes(queryTab as string)) {
    tab.value = queryTab as TabKey
  }

  await Promise.all([loadSessions(), loadPolicy()])
  if (canManage.value) loadLoginAttempts()
})
</script>
