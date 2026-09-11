import { defineStore } from 'pinia'
import { mfaApi } from '../api/auth'
import {
  authApi,
  setMfaEnrollmentRequiredHandler,
  setUnauthorizedHandler,
  type UserInfo,
} from '../api/client'
import { toast } from '../components/toast'
import { router } from '../router'

export const useAuthStore = defineStore('auth', {
  state: () => ({
    user: null as UserInfo | null,
    /** True once the initial /me probe has resolved (either way). */
    initialized: false,
    /** True while /api/bootstrap/status says needs_setup=true. */
    needsSetup: false,
    /** True once the bootstrap status has been checked for this session. */
    setupChecked: false,
  }),

  getters: {
    isAuthenticated: (state) => state.user !== null,
    hasPermission: (state) => (permission: string) =>
      state.user !== null &&
      (state.user.permissions.includes('*') || state.user.permissions.includes(permission)),
  },

  actions: {
    /** Called once by the router guard before the first navigation resolves. */
    async bootstrap() {
      setUnauthorizedHandler(() => this.handleSessionLost())
      setMfaEnrollmentRequiredHandler(() => {
        router.push({ name: 'security', query: { tab: '2fa' } })
        toast.warning(
          'Two-factor authentication is required for your role. Enrol to continue.',
          { title: '2FA required' },
        )
      })
      try {
        this.user = await authApi.me()
      } catch {
        this.user = null
      }
      this.initialized = true
      // After the /setup wizard completes and redirects here, re-check bootstrap
      // status so the guard no longer blocks normal navigation.
      if (this.needsSetup && this.user !== null) {
        this.needsSetup = false
        this.setupChecked = true
      }
    },

    /**
     * Step 1 of the login flow. Returns true when a 2FA code is required
     * (mfa_required=true); the caller should show the MFA step and then call
     * verifyMfa(). Returns false when the login is complete and user is set.
     */
    async login(email: string, password: string): Promise<boolean> {
      const result = await authApi.login(email, password)
      if (!result.mfa_required && result.user) {
        this.user = result.user
      }
      return result.mfa_required
    },

    /** Step 2 of the login flow: exchange mfa_pending cookie for a real session. */
    async verifyMfa(code: string) {
      const user = await mfaApi.verify(code)
      this.user = user
    },

    async logout() {
      try {
        await authApi.logout()
      } finally {
        this.user = null
        router.push({ name: 'login' })
      }
    },

    /** Re-fetch /me to pick up updated fields (e.g. mfa_enabled after enrol/disable). */
    async refreshUser() {
      try {
        this.user = await authApi.me()
      } catch {
        // ignore
      }
    },

    /** 401 with a dead refresh token: drop state and return to the login page. */
    handleSessionLost() {
      if (this.user === null) return
      this.user = null
      router.push({ name: 'login', query: { redirect: router.currentRoute.value.fullPath } })
    },
  },
})
