<template>
  <div ref="root" class="relative">
    <!-- Bell button -->
    <button
      type="button"
      class="fdm-focus relative flex h-8 w-8 items-center justify-center rounded-md transition-colors duration-150 ease-out"
      :style="{ color: open ? 'var(--text-primary)' : 'var(--text-secondary)' }"
      aria-label="Notifications"
      :aria-expanded="open"
      aria-haspopup="dialog"
      @click="toggleDrawer"
    >
      <LucideBell class="h-5 w-5" />
      <!-- Unread badge -->
      <span
        v-if="store.unreadCount > 0"
        class="absolute right-0.5 top-0.5 flex h-4 min-w-[1rem] items-center justify-center rounded-full px-0.5 text-[10px] font-bold"
        :style="{ background: '#B91C1C', color: '#fff' }"
        :aria-label="`${store.unreadCount} unread notifications`"
      >
        {{ store.unreadCount > 99 ? '99+' : store.unreadCount }}
      </span>
    </button>

    <!-- Dropdown drawer -->
    <Transition
      enter-active-class="transition-all duration-150 ease-out"
      leave-active-class="transition-all duration-100 ease-in"
      enter-from-class="opacity-0 translate-y-1"
      leave-to-class="opacity-0 translate-y-1"
    >
      <div
        v-if="open"
        class="absolute right-0 top-10 z-40 flex w-96 flex-col rounded-xl border shadow-2xl"
        :style="{
          background: 'var(--bg-raised)',
          borderColor: 'var(--border-strong)',
          maxHeight: '480px',
        }"
        role="dialog"
        aria-label="Notification center"
      >
        <!-- Header -->
        <div
          class="flex items-center justify-between border-b px-4 py-3"
          :style="{ borderColor: 'var(--border)' }"
        >
          <span class="text-sm font-medium" :style="{ color: 'var(--text-primary)' }">
            Notifications
          </span>
          <div class="flex items-center gap-2">
            <button
              v-if="store.unreadCount > 0"
              type="button"
              class="fdm-focus text-xs transition-colors duration-150"
              :style="{ color: 'var(--text-secondary)' }"
              @click="markAllRead"
            >
              Mark all read
            </button>
            <button
              type="button"
              class="fdm-focus ml-1 text-xs transition-colors duration-150"
              :style="{ color: 'var(--text-secondary)' }"
              aria-label="Notification preferences"
              @click="showPrefs = !showPrefs"
            >
              <LucideSettings class="h-4 w-4" />
            </button>
          </div>
        </div>

        <!-- Channel preferences panel -->
        <div
          v-if="showPrefs"
          class="border-b px-4 py-3"
          :style="{ borderColor: 'var(--border)' }"
        >
          <p class="mb-2 text-xs font-medium uppercase tracking-wide" :style="{ color: 'var(--text-muted)' }">
            Per-event channels
          </p>
          <div v-if="prefsLoading" class="text-xs" :style="{ color: 'var(--text-muted)' }">
            Loading…
          </div>
          <div v-else class="space-y-2">
            <div
              v-for="pref in prefs"
              :key="pref.event_type"
              class="flex items-center justify-between"
            >
              <span class="text-xs" :style="{ color: 'var(--text-secondary)' }">
                {{ eventLabel(pref.event_type) }}
              </span>
              <div class="flex gap-3">
                <label class="flex cursor-pointer items-center gap-1 text-xs" :style="{ color: 'var(--text-muted)' }">
                  <input
                    type="checkbox"
                    class="rounded"
                    :checked="pref.channel_in_app"
                    @change="togglePref(pref.event_type, 'channel_in_app', !pref.channel_in_app)"
                  />
                  In-app
                </label>
                <label class="flex cursor-pointer items-center gap-1 text-xs" :style="{ color: 'var(--text-muted)' }">
                  <input
                    type="checkbox"
                    class="rounded"
                    :checked="pref.channel_email"
                    @change="togglePref(pref.event_type, 'channel_email', !pref.channel_email)"
                  />
                  Email
                </label>
                <label class="flex cursor-pointer items-center gap-1 text-xs" :style="{ color: 'var(--text-muted)' }">
                  <input
                    type="checkbox"
                    class="rounded"
                    :checked="pref.channel_webhook"
                    @change="togglePref(pref.event_type, 'channel_webhook', !pref.channel_webhook)"
                  />
                  Webhook
                </label>
              </div>
            </div>
          </div>
        </div>

        <!-- Feed -->
        <div class="min-h-0 flex-1 overflow-y-auto" aria-live="polite" aria-label="Notifications feed">
          <div v-if="store.loading && store.items.length === 0" class="px-4 py-6 text-center text-sm" :style="{ color: 'var(--text-muted)' }">
            Loading…
          </div>
          <div v-else-if="store.items.length === 0" class="flex flex-col items-center gap-2 px-4 py-8">
            <LucideBellOff class="h-8 w-8" :style="{ color: 'var(--text-muted)' }" />
            <p class="text-sm" :style="{ color: 'var(--text-muted)' }">No notifications</p>
          </div>
          <div v-else>
            <div
              v-for="n in store.items"
              :key="n.id"
              class="group flex items-start gap-3 border-b px-4 py-3 transition-colors duration-100"
              :style="{
                borderColor: 'var(--border)',
                background: n.read ? 'transparent' : 'rgba(255,255,255,0.03)',
              }"
            >
              <!-- Status dot: unread = accent blue, read = muted -->
              <span
                class="mt-1.5 h-2 w-2 shrink-0 rounded-full"
                :style="{ background: n.read ? 'var(--border-strong)' : '#3B82F6' }"
              />
              <div class="min-w-0 flex-1">
                <p
                  class="truncate text-sm"
                  :class="n.read ? '' : 'font-medium'"
                  :style="{ color: n.read ? 'var(--text-secondary)' : 'var(--text-primary)' }"
                >
                  {{ n.title }}
                </p>
                <p v-if="n.body" class="mt-0.5 line-clamp-2 text-xs" :style="{ color: 'var(--text-muted)' }">
                  {{ n.body }}
                </p>
                <div class="mt-1 flex items-center gap-3">
                  <span class="text-[11px]" :style="{ color: 'var(--text-muted)' }">
                    {{ relTime(n.created_at) }}
                  </span>
                  <router-link
                    v-if="entityUrl(n)"
                    :to="entityUrl(n)!"
                    class="text-[11px] hover:underline"
                    :style="{ color: 'var(--text-secondary)' }"
                    @click="store.markRead(n.id); open = false"
                  >
                    View →
                  </router-link>
                </div>
              </div>
              <div class="flex shrink-0 flex-col gap-1 opacity-0 transition-opacity group-hover:opacity-100 group-focus-within:opacity-100">
                <button
                  v-if="!n.read"
                  type="button"
                  class="fdm-focus text-[11px]"
                  :style="{ color: 'var(--text-muted)' }"
                  title="Mark read"
                  aria-label="Mark read"
                  @click="store.markRead(n.id)"
                >
                  <LucideCheck class="h-3.5 w-3.5" />
                </button>
                <button
                  type="button"
                  class="fdm-focus text-[11px]"
                  :style="{ color: 'var(--text-muted)' }"
                  title="Dismiss"
                  aria-label="Dismiss"
                  @click="store.dismiss(n.id)"
                >
                  <LucideX class="h-3.5 w-3.5" />
                </button>
              </div>
            </div>
          </div>
        </div>
      </div>
    </Transition>
  </div>
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import LucideBell from '~icons/lucide/bell'
import LucideBellOff from '~icons/lucide/bell-off'
import LucideCheck from '~icons/lucide/check'
import LucideSettings from '~icons/lucide/settings'
import LucideX from '~icons/lucide/x'
import { type NotificationPref, notificationsApi } from '../api/notifications'
import { useNotificationsStore } from '../stores/notifications'

const store = useNotificationsStore()
const open = ref(false)
const showPrefs = ref(false)
const prefs = ref<NotificationPref[]>([])
const prefsLoading = ref(false)
const root = ref<HTMLElement | null>(null)

function toggleDrawer() {
  open.value = !open.value
  if (open.value) void store.fetch()
}

watch(open, (v) => {
  if (v && showPrefs.value) void loadPrefs()
})

watch(showPrefs, (v) => {
  if (v) void loadPrefs()
})

async function loadPrefs() {
  prefsLoading.value = true
  try {
    prefs.value = await notificationsApi.getPreferences()
  } finally {
    prefsLoading.value = false
  }
}

async function togglePref(
  eventType: string,
  field: 'channel_in_app' | 'channel_email' | 'channel_webhook',
  value: boolean,
) {
  const pref = prefs.value.find((p) => p.event_type === eventType)
  if (!pref) return
  const updated = { ...pref, [field]: value }
  prefs.value = prefs.value.map((p) => (p.event_type === eventType ? updated : p))
  try {
    await notificationsApi.setPreference(eventType, {
      channel_in_app: updated.channel_in_app,
      channel_email: updated.channel_email,
      channel_webhook: updated.channel_webhook,
      webhook_url: updated.webhook_url,
    })
  } catch {
    // revert optimistic update
    prefs.value = prefs.value.map((p) => (p.event_type === eventType ? pref : p))
  }
}

async function markAllRead() {
  await store.markAllRead()
}

function entityUrl(n: { entity_type: string | null; entity_id: number | null }): string | null {
  if (!n.entity_type || n.entity_id == null) return null
  const map: Record<string, string> = {
    job: `/jobs/${n.entity_id}`,
    site: `/sites/${n.entity_id}`,
    server: `/servers/${n.entity_id}`,
    bench: `/benches/${n.entity_id}`,
  }
  return map[n.entity_type] ?? null
}

function eventLabel(eventType: string): string {
  const map: Record<string, string> = {
    'job.success': 'Job succeeded',
    'job.failure': 'Job failed',
    'uptime.down': 'Site down',
    'uptime.restored': 'Site restored',
    'ssl.expiry': 'SSL expiring',
    'backup.compliance_breach': 'Backup compliance breach',
  }
  return map[eventType] ?? eventType
}

function relTime(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime()
  const sec = Math.floor(diff / 1000)
  if (sec < 60) return 'just now'
  const min = Math.floor(sec / 60)
  if (min < 60) return `${min}m ago`
  const hr = Math.floor(min / 60)
  if (hr < 24) return `${hr}h ago`
  return `${Math.floor(hr / 24)}d ago`
}

function onDocumentClick(e: MouseEvent) {
  if (root.value && !root.value.contains(e.target as Node)) {
    open.value = false
    showPrefs.value = false
  }
}

onMounted(() => document.addEventListener('click', onDocumentClick))
onBeforeUnmount(() => document.removeEventListener('click', onDocumentClick))
</script>
