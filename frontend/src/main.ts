import { createApp } from 'vue'
import App from './App.vue'
import './composables/useTheme' // applies the persisted theme before first paint
import './index.css'
import { router } from './router'

createApp(App).use(router).mount('#app')
