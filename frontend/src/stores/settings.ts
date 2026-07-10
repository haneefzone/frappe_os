/**
 * The settings store: the white-label branding source of truth for the app
 * shell. It fetches GET /api/settings once after login and exposes reactive
 * `productName` + `logoUrl` so the sidebar rebrands live when an Admin saves
 * on the Settings page (SettingsPage calls `set()` on success).
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { type Settings, settingsApi } from '../api/settings'

export const useSettingsStore = defineStore('settings', () => {
  const settings = ref<Settings | null>(null)
  const loaded = ref(false)

  const productName = computed(() => settings.value?.product_name || 'FDM Platform')

  // The logo is served by the API; cache-bust on updated_at so a re-upload
  // shows immediately instead of a stale cached image.
  const logoUrl = computed(() => {
    const s = settings.value
    return s?.logo_path ? `${s.logo_path}?v=${encodeURIComponent(s.updated_at)}` : null
  })

  /** Idempotent: fetch once after login. Failures leave the defaults in place. */
  async function load() {
    if (loaded.value) return
    try {
      settings.value = await settingsApi.get()
    } catch {
      // Branding is non-critical; fall back to defaults if the fetch fails.
    } finally {
      loaded.value = true
    }
  }

  /** Replace the cached settings (after an Admin saves) so branding updates live. */
  function set(next: Settings) {
    settings.value = next
    loaded.value = true
  }

  return { settings, loaded, productName, logoUrl, load, set }
})
