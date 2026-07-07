import type { FunctionalComponent, SVGAttributes } from 'vue'
import LucideActivity from '~icons/lucide/activity'
import LucideArchive from '~icons/lucide/archive'
import LucideBlocks from '~icons/lucide/blocks'
import LucideBot from '~icons/lucide/bot'
import LucideCalendarClock from '~icons/lucide/calendar-clock'
import LucideChartBar from '~icons/lucide/chart-bar'
import LucideFileText from '~icons/lucide/file-text'
import LucideGlobe from '~icons/lucide/globe'
import LucideHistory from '~icons/lucide/history'
import LucideLayers from '~icons/lucide/layers'
import LucideLayoutDashboard from '~icons/lucide/layout-dashboard'
import LucideListChecks from '~icons/lucide/list-checks'
import LucideScrollText from '~icons/lucide/scroll-text'
import LucideServer from '~icons/lucide/server'
import LucideSettings from '~icons/lucide/settings'
import LucideShield from '~icons/lucide/shield'
import LucideTerminal from '~icons/lucide/terminal'
import LucideUsers from '~icons/lucide/users'
import LucideWrench from '~icons/lucide/wrench'

type Icon = FunctionalComponent<SVGAttributes>

export interface NavItem {
  name: string
  label: string
  path: string
  icon: Icon
  /** Shown on the placeholder page until the real screen is built. */
  description: string
}

export interface NavGroup {
  /** null = ungrouped top-level entry (Dashboard). */
  label: string | null
  items: NavItem[]
}

// Sidebar IA from docs/uiux-spec.md B3. Settings is pinned to the sidebar bottom.
export const navGroups: NavGroup[] = [
  {
    label: null,
    items: [
      {
        name: 'dashboard',
        label: 'Dashboard',
        path: '/',
        icon: LucideLayoutDashboard,
        description: 'Fleet health, backup compliance, alerts, and running jobs at a glance.',
      },
    ],
  },
  {
    label: 'Infrastructure',
    items: [
      {
        name: 'servers',
        label: 'Servers',
        path: '/servers',
        icon: LucideServer,
        description: 'Connected Ubuntu servers with live resource usage and SSH status.',
      },
      {
        name: 'benches',
        label: 'Benches',
        path: '/benches',
        icon: LucideLayers,
        description: 'Frappe benches grouped by server, with versions, ports, and site counts.',
      },
      {
        name: 'sites',
        label: 'Sites',
        path: '/sites',
        icon: LucideGlobe,
        description: 'All sites across benches with health, version, and backup status.',
      },
      {
        name: 'apps',
        label: 'Apps',
        path: '/apps',
        icon: LucideBlocks,
        description: 'App sources and the installed app-by-site version matrix.',
      },
    ],
  },
  {
    label: 'Data Protection',
    items: [
      {
        name: 'backups',
        label: 'Backups',
        path: '/backups',
        icon: LucideArchive,
        description: 'Backup inventory, compliance KPIs, and per-site retention policies.',
      },
      {
        name: 'restore',
        label: 'Restore',
        path: '/restore',
        icon: LucideHistory,
        description: 'Guided restore flow: pick a backup, choose a target, confirm, watch progress.',
      },
    ],
  },
  {
    label: 'Operations',
    items: [
      {
        name: 'jobs',
        label: 'Jobs',
        path: '/jobs',
        icon: LucideListChecks,
        description: 'Live job queue with step timelines and streaming logs.',
      },
      {
        name: 'monitoring',
        label: 'Monitoring',
        path: '/monitoring',
        icon: LucideActivity,
        description: 'Server and site metrics, service health, and alert rules.',
      },
      {
        name: 'logs',
        label: 'Logs',
        path: '/logs',
        icon: LucideFileText,
        description: 'Browse and tail server, bench, and site logs from one place.',
      },
      {
        name: 'terminal',
        label: 'Terminal',
        path: '/terminal',
        icon: LucideTerminal,
        description: 'Interactive SSH terminal sessions, scoped and recorded.',
      },
    ],
  },
  {
    label: 'Automation',
    items: [
      {
        name: 'schedules',
        label: 'Schedules',
        path: '/schedules',
        icon: LucideCalendarClock,
        description: 'Recurring backups, restore tests, and maintenance windows.',
      },
      {
        name: 'tools',
        label: 'Tools',
        path: '/tools',
        icon: LucideWrench,
        description: 'Per-server stack checklist: detected versions vs recommended, one-click installs.',
      },
      {
        name: 'ai-agents',
        label: 'AI Agents',
        path: '/ai-agents',
        icon: LucideBot,
        description: 'Scoped AI CLI sessions with pre-change backups and diff review.',
      },
    ],
  },
  {
    label: 'Governance',
    items: [
      {
        name: 'users-roles',
        label: 'Users & Roles',
        path: '/users-roles',
        icon: LucideUsers,
        description: 'User accounts, invitations, and the role permission matrix.',
      },
      {
        name: 'audit-log',
        label: 'Audit Log',
        path: '/audit-log',
        icon: LucideScrollText,
        description: 'Immutable record of every state-changing action, exportable for compliance.',
      },
      {
        name: 'security',
        label: 'Security',
        path: '/security',
        icon: LucideShield,
        description: 'Active sessions, API tokens, 2FA enforcement, and access policies.',
      },
      {
        name: 'reports',
        label: 'Reports',
        path: '/reports',
        icon: LucideChartBar,
        description: 'Prebuilt fleet, backup-evidence, and activity reports with export.',
      },
    ],
  },
]

export const settingsItem: NavItem = {
  name: 'settings',
  label: 'Settings',
  path: '/settings',
  icon: LucideSettings,
  description: 'White-label branding, storage targets, notifications, and platform defaults.',
}

export const allNavItems: NavItem[] = [...navGroups.flatMap((g) => g.items), settingsItem]
