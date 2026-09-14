import frappeUIPreset from 'frappe-ui/tailwind'

/** @type {import('tailwindcss').Config} */
export default {
  presets: [frappeUIPreset],
  darkMode: 'class',
  content: [
    './index.html',
    './src/**/*.{vue,js,ts,jsx,tsx}',
    './node_modules/frappe-ui/src/components/**/*.{vue,js,ts}',
  ],
  theme: {
    extend: {
      // FDM tokens (docs/uiux-spec.md B2). Neutrals resolve through the CSS
      // variables in src/index.css so they flip with the theme; status colors
      // are literal hex because they are identical in both themes (and literal
      // values keep Tailwind's `/10` opacity modifiers working).
      colors: {
        base: 'var(--bg-base)',
        // frappe-ui's Tailwind plugin extends `backgroundColor.surface` with its own
        // nested object ({white, gray-1, …}). When our scalar `'var(--bg-surface)'`
        // is resolved against that object extension, the object wins and the bare
        // `bg-surface` utility is never emitted — inputs fall back to the
        // @tailwindcss/forms hardcoded `background-color:#fff`, making typed text
        // (near-white #f4f4f5 in dark mode) invisible on a white background.
        // Using {DEFAULT:…} matches the `line` token pattern and deep-merges
        // correctly: the resulting `backgroundColor.surface` object gains a DEFAULT
        // key alongside frappe-ui's nested keys, so both `bg-surface` and
        // `bg-surface-gray-*` are generated.
        surface: {
          DEFAULT: 'var(--bg-surface)',
        },
        raised: 'var(--bg-raised)',
        line: {
          DEFAULT: 'var(--border)',
          strong: 'var(--border-strong)',
        },
        ink: {
          1: 'var(--text-primary)',
          2: 'var(--text-secondary)',
          3: 'var(--text-muted)',
        },
        ok: '#22C55E',
        warn: '#F59E0B',
        err: '#EF4444',
        run: '#3B82F6',
      },
      // Type scale: 12 meta · 13 secondary · 14 body · 16 section · 20 page · 28 KPI
      fontSize: {
        meta: ['12px', '16px'],
        label: ['13px', '18px'],
        body: ['14px', '20px'],
        section: ['16px', '22px'],
        page: ['20px', '26px'],
        kpi: ['28px', '32px'],
      },
      fontFamily: {
        mono: [
          'JetBrains Mono',
          'ui-monospace',
          'SFMono-Regular',
          'Menlo',
          'Consolas',
          'monospace',
        ],
      },
      transitionDuration: {
        DEFAULT: '150ms',
      },
    },
  },
}
