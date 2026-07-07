import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import frappeui from 'frappe-ui/vite'

export default defineConfig({
  plugins: [
    // frappe-ui is used for components + design tokens only (CLAUDE.md rule 9).
    // Its Frappe-site integrations (proxy, jinja boot data, build config) stay off;
    // we only need the ~icons resolver its components rely on.
    frappeui({
      lucideIcons: true,
      frappeProxy: false,
      jinjaBootData: false,
      buildConfig: false,
    }),
    vue(),
  ],
  optimizeDeps: {
    // frappe-ui ships raw source; the esbuild prebundler can't resolve its
    // virtual ~icons/* imports, so it must go through the plugin pipeline.
    exclude: ['frappe-ui'],
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
})
