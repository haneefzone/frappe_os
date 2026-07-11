<template>
  <div class="flex flex-col gap-3 rounded-lg border border-line bg-surface p-4">
    <!-- Title row -->
    <div class="flex items-start justify-between gap-3">
      <div class="min-w-0">
        <div class="flex flex-wrap items-center gap-2">
          <h3 class="truncate text-body font-semibold text-ink-1">{{ agent.name }}</h3>
          <span
            class="shrink-0 rounded-full border border-line bg-raised px-2 py-0.5 text-meta text-ink-2"
          >
            {{ AGENT_KIND_LABEL[agent.kind] }}
          </span>
        </div>
        <p class="mt-1 truncate font-mono text-meta text-ink-3" :title="agent.working_dir">
          {{ agent.working_dir }}
        </p>
      </div>
      <component :is="agent.kind === 'claude-code' ? LucideBot : LucideTerminal" class="h-5 w-5 shrink-0 text-ink-3" />
    </div>

    <!-- State pills -->
    <div class="flex flex-wrap items-center gap-1.5">
      <span
        class="inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-meta"
        :class="agent.read_only
          ? 'border-line bg-raised text-ink-2'
          : 'border-warn/35 bg-warn/10 text-warn'"
      >
        {{ agent.read_only ? 'Read-only' : 'Read-write' }}
      </span>
      <span
        v-if="agent.pre_change_backup"
        class="inline-flex items-center gap-1 rounded-full border border-line bg-raised px-2 py-0.5 text-meta text-ink-2"
      >
        <LucideArchive class="h-3 w-3" /> Pre-change backup
      </span>
      <span class="text-meta text-ink-3">
        {{ agent.allowed_server_ids.length }}
        {{ agent.allowed_server_ids.length === 1 ? 'server' : 'servers' }}
      </span>
    </div>

    <!-- Actions -->
    <div class="mt-1 flex items-center gap-2 border-t border-line pt-3">
      <Button
        v-if="canOperate"
        variant="solid"
        theme="gray"
        size="sm"
        label="Start session"
        :disabled="agent.allowed_server_ids.length === 0"
        :title="agent.allowed_server_ids.length === 0 ? 'No servers are allowed for this agent yet.' : undefined"
        @click="$emit('start', agent)"
      >
        <template #prefix><LucidePlay class="h-3.5 w-3.5" /></template>
      </Button>
      <div class="ml-auto flex items-center gap-1">
        <Button
          v-if="canManage"
          variant="subtle"
          theme="gray"
          size="sm"
          label="Edit"
          @click="$emit('edit', agent)"
        />
        <Button
          v-if="canManage"
          variant="subtle"
          theme="gray"
          size="sm"
          label="Delete"
          @click="$emit('delete', agent)"
        />
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { Button } from 'frappe-ui'
import LucideArchive from '~icons/lucide/archive'
import LucideBot from '~icons/lucide/bot'
import LucidePlay from '~icons/lucide/play'
import LucideTerminal from '~icons/lucide/terminal'
import type { AgentConfig } from '../api/aiAgents'
import { AGENT_KIND_LABEL } from '../lib/aiAgents'

defineProps<{
  agent: AgentConfig
  canManage: boolean
  canOperate: boolean
}>()

defineEmits<{
  start: [agent: AgentConfig]
  edit: [agent: AgentConfig]
  delete: [agent: AgentConfig]
}>()
</script>
