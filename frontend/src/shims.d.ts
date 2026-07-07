declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<object, object, unknown>
  export default component
}

declare module 'frappe-ui'

// Injected by the `define` block in vite.config.ts (package.json version).
declare const __APP_VERSION__: string

// Virtual icon modules served by the frappe-ui vite plugin (lucide-static).
declare module '~icons/lucide/*' {
  import type { FunctionalComponent, SVGAttributes } from 'vue'
  const component: FunctionalComponent<SVGAttributes>
  export default component
}
