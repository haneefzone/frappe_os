import { createPinia } from 'pinia'
import { createApp } from 'vue'
// Ship the spec fonts (uiux-spec B2) instead of hoping the OS has them.
import '@fontsource/inter/400.css'
import '@fontsource/inter/500.css'
import '@fontsource/inter/600.css'
import '@fontsource/inter/700.css'
import '@fontsource/jetbrains-mono/400.css'
import '@fontsource/jetbrains-mono/500.css'
import App from './App.vue'
import './composables/useTheme' // applies the persisted theme before first paint
import './index.css'
import { router } from './router'

createApp(App).use(createPinia()).use(router).mount('#app')
