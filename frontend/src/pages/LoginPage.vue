<template>
  <div class="flex min-h-screen items-center justify-center bg-base px-4">
    <div class="w-full max-w-sm">
      <!-- Branded product mark above the card (session 6.6: driven by /api/branding) -->
      <div class="mb-6 flex flex-col items-center gap-2">
        <div
          class="flex h-10 w-10 items-center justify-center overflow-hidden rounded-lg border border-line bg-raised"
        >
          <img
            v-if="logoSrc"
            :src="logoSrc"
            :alt="productName"
            class="h-full w-full object-contain"
            @error="logoSrc = null"
          />
          <LucideServer v-else class="h-5 w-5 text-ink-1" />
        </div>
        <h1 class="text-page font-semibold text-ink-1">{{ productName }}</h1>
        <p class="text-label text-ink-2">Sign in to manage your Frappe deployments</p>
      </div>

      <!-- Step 1: Credentials -->
      <form
        v-if="step === 'credentials'"
        class="rounded-lg border border-line bg-surface p-6"
        novalidate
        @submit.prevent="submitCredentials"
      >
        <label class="block">
          <span class="mb-1.5 block text-label font-medium text-ink-2">Email</span>
          <input
            v-model="email"
            type="email"
            name="email"
            autocomplete="username"
            required
            autofocus
            placeholder="you@company.com"
            v-bind="inputAttrs"
          />
        </label>

        <label class="mt-4 block">
          <span class="mb-1.5 block text-label font-medium text-ink-2">Password</span>
          <input
            v-model="password"
            type="password"
            name="password"
            autocomplete="current-password"
            required
            placeholder="••••••••"
            v-bind="inputAttrs"
          />
        </label>

        <p v-if="errorMessage" class="mt-3 text-label text-err" role="alert">
          {{ errorMessage }}
        </p>

        <p v-if="lockoutSeconds > 0" class="mt-3 text-label text-warn" role="status">
          Too many attempts. Try again in {{ lockoutSeconds }}s.
        </p>

        <Button
          type="submit"
          class="mt-5 w-full"
          variant="solid"
          theme="gray"
          size="md"
          label="Sign in"
          :loading="loading"
          :disabled="loading || !email || !password || lockoutSeconds > 0"
        />
      </form>

      <!-- Step 2: MFA code -->
      <form
        v-else
        class="rounded-lg border border-line bg-surface p-6"
        novalidate
        @submit.prevent="submitMfa"
      >
        <div class="mb-5 flex flex-col items-center gap-2 text-center">
          <div class="flex h-10 w-10 items-center justify-center rounded-full border border-line bg-raised">
            <LucideShield class="h-5 w-5 text-ink-1" />
          </div>
          <h2 class="text-section font-semibold text-ink-1">Two-factor authentication</h2>
          <p class="text-label text-ink-2">
            Enter the 6-digit code from your authenticator app, or use a recovery code.
          </p>
        </div>

        <label class="block">
          <span class="mb-1.5 block text-label font-medium text-ink-2">Code</span>
          <input
            ref="mfaInput"
            v-model="mfaCode"
            type="text"
            name="otp"
            autocomplete="one-time-code"
            inputmode="text"
            autocapitalize="characters"
            maxlength="9"
            placeholder="000000"
            v-bind="inputAttrs"
          />
        </label>
        <p class="mt-1 text-meta text-ink-3">
          6-digit TOTP code or a recovery code (XXXX-XXXX).
        </p>

        <p v-if="mfaError" class="mt-3 text-label text-err" role="alert">
          {{ mfaError }}
        </p>

        <p v-if="lockoutSeconds > 0" class="mt-3 text-label text-warn" role="status">
          Too many attempts. Try again in {{ lockoutSeconds }}s.
        </p>

        <Button
          type="submit"
          class="mt-5 w-full"
          variant="solid"
          theme="gray"
          size="md"
          label="Verify"
          :loading="loading"
          :disabled="loading || !mfaCode || lockoutSeconds > 0"
        />

        <button
          type="button"
          class="mt-3 w-full text-center text-label text-ink-3 transition hover:text-ink-1"
          @click="backToCredentials"
        >
          Use a different account
        </button>
      </form>

      <p v-if="supportLink" class="mt-4 text-center text-meta text-ink-3">
        Need help?
        <a
          :href="supportLink"
          target="_blank"
          rel="noopener noreferrer"
          class="text-ink-2 underline underline-offset-2 hover:text-ink-1"
        >Contact support</a>
      </p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { Button } from 'frappe-ui'
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import LucideServer from '~icons/lucide/server'
import LucideShield from '~icons/lucide/shield'
import { ApiError } from '../api/client'
import { useAuthStore } from '../stores/auth'
import { useSettingsStore } from '../stores/settings'
import { useTheme } from '../composables/useTheme'

const auth = useAuthStore()
const settingsStore = useSettingsStore()
const { theme } = useTheme()
const route = useRoute()
const router = useRouter()

const inputAttrs = {
  class:
    'h-9 w-full rounded-md border border-line bg-base px-3 text-body text-ink-1 outline-none transition-colors placeholder:text-ink-3 focus:border-line-strong',
}

type Step = 'credentials' | 'mfa'
const step = ref<Step>('credentials')

const email = ref('')
const password = ref('')
const loading = ref(false)
const errorMessage = ref('')

const productName = computed(() => settingsStore.productName)
const supportLink = computed(() => settingsStore.supportLink)

// Use dark or light logo depending on current theme; fall back to the other,
// then to null (shows the server icon).
const logoSrc = ref<string | null>(null)
function updateLogoSrc() {
  if (theme.value === 'dark' && settingsStore.logoDarkUrl) {
    logoSrc.value = settingsStore.logoDarkUrl
  } else if (settingsStore.logoUrl) {
    logoSrc.value = settingsStore.logoUrl
  } else {
    logoSrc.value = null
  }
}

onMounted(async () => {
  // Fetch the public brand bundle so the login page renders branded even before
  // the user is authenticated. This is safe — /api/branding exposes nothing sensitive.
  await settingsStore.loadPublicBranding()
  updateLogoSrc()
})

const mfaCode = ref('')
const mfaError = ref('')
const mfaInput = ref<HTMLInputElement | null>(null)

const lockoutSeconds = ref(0)
let lockoutTimer: ReturnType<typeof setInterval> | null = null

function startLockout(seconds: number) {
  lockoutSeconds.value = seconds
  if (lockoutTimer) clearInterval(lockoutTimer)
  lockoutTimer = setInterval(() => {
    lockoutSeconds.value = Math.max(0, lockoutSeconds.value - 1)
    if (lockoutSeconds.value === 0 && lockoutTimer) {
      clearInterval(lockoutTimer)
      lockoutTimer = null
    }
  }, 1000)
}

onUnmounted(() => {
  if (lockoutTimer) clearInterval(lockoutTimer)
})

async function submitCredentials() {
  loading.value = true
  errorMessage.value = ''
  lockoutSeconds.value = 0
  try {
    const mfaRequired = await auth.login(email.value, password.value)
    if (mfaRequired) {
      step.value = 'mfa'
      await nextTick()
      mfaInput.value?.focus()
    } else {
      const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : '/'
      router.push(redirect)
    }
  } catch (error) {
    if (error instanceof ApiError) {
      if (error.status === 429 && error.retryAfter) {
        startLockout(error.retryAfter)
        errorMessage.value = ''
      } else {
        errorMessage.value = error.message
      }
    } else {
      errorMessage.value = 'Could not reach the server. Try again.'
    }
  } finally {
    loading.value = false
  }
}

async function submitMfa() {
  loading.value = true
  mfaError.value = ''
  lockoutSeconds.value = 0
  try {
    await auth.verifyMfa(mfaCode.value)
    const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : '/'
    router.push(redirect)
  } catch (error) {
    if (error instanceof ApiError) {
      if (error.status === 429 && error.retryAfter) {
        startLockout(error.retryAfter)
        mfaError.value = ''
      } else {
        mfaError.value = error.message
      }
    } else {
      mfaError.value = 'Could not reach the server. Try again.'
    }
  } finally {
    loading.value = false
  }
}

function backToCredentials() {
  step.value = 'credentials'
  mfaCode.value = ''
  mfaError.value = ''
  lockoutSeconds.value = 0
  if (lockoutTimer) {
    clearInterval(lockoutTimer)
    lockoutTimer = null
  }
}
</script>
