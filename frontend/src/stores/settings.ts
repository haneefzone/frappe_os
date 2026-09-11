/**
 * The settings store: the white-label branding source of truth for the app
 * shell (sessions 1.12 + 6.6). It fetches GET /api/settings once after login
 * and exposes reactive brand tokens so every surface rebrands live when an
 * Admin saves on the Settings page.
 *
 * Brand token injection side effects (document-level, idempotent):
 *  - document.title ← productName
 *  - <link rel="icon"> href ← faviconUrl (or browser default)
 *  - CSS --brand-accent on :root ← accentHex (or design-system default)
 *
 * Status colours (--status-ok/warn/err/info) are NEVER overridden here
 * (uiux-spec B1.5: colour only means state).
 */

import { defineStore } from 'pinia'
import { computed, ref, watch } from 'vue'
import { type Settings, settingsApi } from '../api/settings'

export const useSettingsStore = defineStore('settings', () => {
  const settings = ref<Settings | null>(null)
  const loaded = ref(false)

  const productName = computed(() => settings.value?.product_name || 'FDM Platform')

  // Cache-bust on updated_at so a re-upload shows immediately.
  const logoUrl = computed(() => {
    const s = settings.value
    return s?.logo_path ? `${s.logo_path}?v=${encodeURIComponent(s.updated_at)}` : null
  })

  const logoDarkUrl = computed(() => {
    const s = settings.value
    return s?.logo_dark_path ? `${s.logo_dark_path}?v=${encodeURIComponent(s.updated_at)}` : null
  })

  const faviconUrl = computed(() => {
    const s = settings.value
    return s?.favicon_path ? `${s.favicon_path}?v=${encodeURIComponent(s.updated_at)}` : null
  })

  const accentHex = computed(() => settings.value?.accent_hex ?? null)
  const supportLink = computed(() => settings.value?.support_link ?? null)
  const footerLine = computed(() => settings.value?.footer_line ?? null)

  // -----------------------------------------------------------------------
  // Brand token injection: update document-level state when settings change.
  // -----------------------------------------------------------------------

  function _applyBrandTokens(name: string, favicon: string | null, accent: string | null) {
    // Tab title.
    document.title = name

    // Favicon: update or insert <link rel="icon">.
    let link = document.querySelector<HTMLLinkElement>('link[rel="icon"]')
    if (favicon) {
      if (!link) {
        link = document.createElement('link')
        link.rel = 'icon'
        document.head.appendChild(link)
      }
      link.href = favicon
    } else if (link) {
      // No custom favicon — remove any previously set one so the browser
      // falls back to its default.
      link.removeAttribute('href')
    }

    // Accent CSS variable on :root — status colours are never touched here.
    if (accent) {
      document.documentElement.style.setProperty('--brand-accent', accent)
    } else {
      // Remove the override so the CSS default takes effect.
      document.documentElement.style.removeProperty('--brand-accent')
    }
  }

  // Reactively apply brand tokens whenever the relevant computed values change.
  watch(
    [productName, faviconUrl, accentHex],
    ([name, favicon, accent]) => {
      _applyBrandTokens(name, favicon, accent)
    },
    { immediate: false },
  )

  /** Idempotent: fetch once after login. Failures leave defaults in place. */
  async function load() {
    if (loaded.value) return
    try {
      settings.value = await settingsApi.get()
    } catch {
      // Branding is non-critical; fall back to defaults if the fetch fails.
    } finally {
      loaded.value = true
      _applyBrandTokens(productName.value, faviconUrl.value, accentHex.value)
    }
  }

  /**
   * Seed from the public /api/branding bundle (no auth required).
   * Used by the login page and wizard to render branded before auth.
   * Does not set `loaded` — the full GET /api/settings runs after login.
   */
  async function loadPublicBranding() {
    try {
      const b = await settingsApi.branding()
      // Merge only the brand fields; leave auth-gated defaults untouched.
      settings.value = {
        product_name: b.product_name,
        logo_path: b.logo_url,
        logo_dark_path: b.logo_dark_url,
        favicon_path: b.favicon_url,
        accent_hex: b.accent_hex,
        support_link: b.support_link,
        footer_line: b.footer_line,
        // Placeholders — these are never read before full auth.
        default_tz: '',
        bench_base_path: '',
        port_range_start: 0,
        port_range_end: 0,
        updated_at: new Date().toISOString(),
      }
      _applyBrandTokens(productName.value, faviconUrl.value, accentHex.value)
    } catch {
      // Non-critical: fall back to hard-coded defaults.
    }
  }

  /** Replace the cached settings (after an Admin saves) so branding updates live. */
  function set(next: Settings) {
    settings.value = next
    loaded.value = true
  }

  return {
    settings,
    loaded,
    productName,
    logoUrl,
    logoDarkUrl,
    faviconUrl,
    accentHex,
    supportLink,
    footerLine,
    load,
    loadPublicBranding,
    set,
  }
})
