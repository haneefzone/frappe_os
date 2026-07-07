<template>
  <div class="flex h-screen" :style="{ background: 'var(--bg-base)' }">
    <aside
      class="flex w-56 flex-col border-r px-4 py-5"
      :style="{ background: 'var(--bg-surface)', borderColor: 'var(--border)' }"
    >
      <div class="text-sm font-semibold" :style="{ color: 'var(--text-primary)' }">
        FDM Platform
      </div>
      <div class="mt-1 text-xs" :style="{ color: 'var(--text-muted)' }">v0.1.0</div>
    </aside>
    <main class="flex flex-1 items-center justify-center">
      <div
        class="rounded-lg border px-10 py-8 text-center"
        :style="{ background: 'var(--bg-raised)', borderColor: 'var(--border)' }"
      >
        <h1 class="text-2xl font-semibold" :style="{ color: 'var(--text-primary)' }">
          FDM Platform
        </h1>
        <p class="mt-2 text-sm" :style="{ color: 'var(--text-secondary)' }">
          Self-hosted control panel for bare-metal Frappe/ERPNext deployments.
        </p>
        <div class="mt-6 flex justify-center">
          <Button variant="solid" theme="gray">Get started</Button>
        </div>
      </div>
    </main>
  </div>
</template>

<script setup lang="ts">
import { Button } from 'frappe-ui'
</script>
