<template>
  <div class="flex min-h-screen items-center justify-center bg-base px-4">
    <div class="w-full max-w-sm">
      <!-- Product mark above the card, frappe-style -->
      <div class="mb-6 flex flex-col items-center gap-2">
        <div
          class="flex h-10 w-10 items-center justify-center rounded-lg border border-line bg-raised"
        >
          <LucideServer class="h-5 w-5 text-ink-1" />
        </div>
        <h1 class="text-page font-semibold text-ink-1">FDM Platform</h1>
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
    </div>
  </div>
</template>

<script setup lang="ts">
import { Button } from 'frappe-ui'
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import LucideServer from '~icons/lucide/server'
import { ApiError } from '../api/client'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()

const email = ref('')
const password = ref('')
const loading = ref(false)
const errorMessage = ref('')

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
