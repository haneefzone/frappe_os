import { ref, watch } from 'vue'

type Theme = 'dark' | 'light'

const STORAGE_KEY = 'fdm-theme'

function load(): Theme {
  // Dark is the default; only an explicit 'light' choice switches.
  return localStorage.getItem(STORAGE_KEY) === 'light' ? 'light' : 'dark'
}

function apply(theme: Theme) {
  document.documentElement.classList.toggle('light', theme === 'light')
  document.documentElement.style.colorScheme = theme
}

const theme = ref<Theme>(load())
apply(theme.value)
watch(theme, (value) => {
  apply(value)
  localStorage.setItem(STORAGE_KEY, value)
})

export function useTheme() {
  function toggle() {
    theme.value = theme.value === 'dark' ? 'light' : 'dark'
  }
  return { theme, toggle }
}
