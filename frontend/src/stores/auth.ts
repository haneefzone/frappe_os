import { defineStore } from 'pinia'
import { authApi, setUnauthorizedHandler, type UserInfo } from '../api/client'
import { router } from '../router'

export const useAuthStore = defineStore('auth', {
  state: () => ({
    user: null as UserInfo | null,
    /** True once the initial /me probe has resolved (either way). */
    initialized: false,
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
      try {
        this.user = await authApi.me()
      } catch {
        this.user = null
      }
      this.initialized = true
    },

    async login(email: string, password: string) {
      this.user = await authApi.login(email, password)
    },

    async logout() {
      try {
        await authApi.logout()
      } finally {
        this.user = null
        router.push({ name: 'login' })
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
