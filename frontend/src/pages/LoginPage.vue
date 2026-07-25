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

      <form
        class="rounded-lg border border-line bg-surface p-6"
        novalidate
        @submit.prevent="submit"
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
            class="h-9 w-full rounded-md border border-line bg-base px-3 text-body text-ink-1 outline-none transition-colors placeholder:text-ink-3 focus:border-line-strong"
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
            class="h-9 w-full rounded-md border border-line bg-base px-3 text-body text-ink-1 outline-none transition-colors placeholder:text-ink-3 focus:border-line-strong"
          />
        </label>

        <p v-if="errorMessage" class="mt-3 text-label text-err" role="alert">
          {{ errorMessage }}
        </p>

        <Button
          type="submit"
          class="mt-5 w-full"
          variant="solid"
          theme="gray"
          size="md"
          label="Sign in"
          :loading="loading"
          :disabled="loading || !email || !password"
        />
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
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import LucideServer from '~icons/lucide/server'
import { ApiError } from '../api/client'
import { useAuthStore } from '../stores/auth'
import { useSettingsStore } from '../stores/settings'
import { useTheme } from '../composables/useTheme'

const auth = useAuthStore()
const settingsStore = useSettingsStore()
const { theme } = useTheme()
const route = useRoute()
const router = useRouter()

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

async function submit() {
  loading.value = true
  errorMessage.value = ''
  try {
    await auth.login(email.value, password.value)
    const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : '/'
    router.push(redirect)
  } catch (error) {
    errorMessage.value =
      error instanceof ApiError ? error.message : 'Could not reach the server. Try again.'
  } finally {
    loading.value = false
  }
}
</script>
