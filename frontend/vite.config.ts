import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import frappeui from 'frappe-ui/vite'
import pkg from './package.json'

export default defineConfig({
  define: {
    __APP_VERSION__: JSON.stringify(pkg.version),
  },
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
    // With frappe-ui excluded, esbuild never scans its imports, so its CJS
    // deps must be pre-bundled explicitly or dev serving fails on them.
    include: ['feather-icons', 'dompurify', 'socket.io-client', 'dayjs'],
  },
  server: {
    // The operator browses from the Windows host into this Hyper-V VM
    // (docs/dev-setup.md) — localhost-only binding would be unreachable.
    host: true,
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
})
