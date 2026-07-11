<template>
  <Teleport to="body">
    <Transition
      enter-active-class="transition duration-150 ease-out"
      enter-from-class="opacity-0"
      leave-active-class="transition duration-150 ease-out"
      leave-to-class="opacity-0"
    >
      <div v-if="open" class="fixed inset-0 z-50 flex justify-end bg-black/60" @click.self="$emit('close')">
        <Transition
          enter-active-class="transition duration-150 ease-out"
          enter-from-class="translate-x-full"
          leave-active-class="transition duration-150 ease-out"
          leave-to-class="translate-x-full"
          appear
        >
          <aside
            ref="el"
            class="flex h-full w-full max-w-[560px] flex-col border-l border-line bg-base"
            role="dialog"
            aria-modal="true"
            aria-label="Changelog"
          >
            <!-- Header -->
            <header class="flex items-center justify-between border-b border-line px-5 py-4">
              <div class="min-w-0">
                <h2 class="truncate text-section font-semibold text-ink-1">
                  {{ preview?.app_name ?? 'Changelog' }}
                </h2>
                <p v-if="preview" class="text-meta text-ink-2">
                  <span class="font-mono">{{ preview.installed_ref ?? '—' }}</span>
                  <template v-if="preview.latest_ref">
                    → <span class="font-mono text-ink-1">{{ preview.latest_ref }}</span>
                  </template>
                </p>
              </div>
              <button
                type="button"
                class="fdm-focus rounded p-1 text-ink-3 transition hover:bg-raised hover:text-ink-1"
                aria-label="Close"
                @click="$emit('close')"
              >
                <LucideX class="h-4 w-4" />
              </button>
            </header>

            <!-- Body -->
            <div class="min-h-0 flex-1 overflow-y-auto p-5">
              <!-- Loading -->
              <div v-if="loading" class="space-y-3">
                <div v-for="i in 4" :key="i" class="rounded-lg border border-line bg-surface p-3">
                  <div class="h-3.5 w-24 animate-pulse rounded bg-raised" />
                  <div class="mt-2 h-3 w-40 animate-pulse rounded bg-raised" />
                </div>
              </div>

              <p v-else-if="error" class="text-label text-err" role="alert">{{ error }}</p>

              <template v-else-if="preview">
                <EmptyState
                  v-if="preview.releases.length === 0"
                  :icon="LucideFileText"
                  title="No release notes"
                  :message="
                    preview.behind_by === 0
                      ? 'This app is up to date.'
                      : 'No release entries are available for this range.'
                  "
                />

                <ol v-else class="space-y-3">
                  <li
                    v-for="rel in preview.releases"
                    :key="rel.tag"
                    class="rounded-lg border border-line bg-surface p-3"
                  >
                    <div class="flex items-center justify-between gap-3">
                      <div class="min-w-0">
                        <p class="truncate font-medium text-ink-1">{{ rel.version || rel.tag }}</p>
                        <p v-if="rel.version && rel.version !== rel.tag" class="font-mono text-meta text-ink-3">
                          {{ rel.tag }}
                        </p>
                      </div>
                      <a
                        v-if="rel.notes_url"
                        :href="rel.notes_url"
                        target="_blank"
                        rel="noopener noreferrer"
                        class="fdm-focus inline-flex shrink-0 items-center gap-1 rounded text-label text-run transition hover:underline"
                      >
                        Notes
                        <LucideExternalLink class="h-3.5 w-3.5" />
                      </a>
                    </div>
                  </li>
                </ol>
              </template>
            </div>

            <!-- Footer links -->
            <footer
              v-if="preview && (preview.compare_url || preview.releases_url)"
              class="flex flex-wrap justify-end gap-3 border-t border-line px-5 py-3.5 text-label"
            >
              <a
                v-if="preview.compare_url"
                :href="preview.compare_url"
                target="_blank"
                rel="noopener noreferrer"
                class="fdm-focus inline-flex items-center gap-1 rounded text-run transition hover:underline"
              >
                Compare on GitHub
                <LucideExternalLink class="h-3.5 w-3.5" />
              </a>
              <a
                v-if="preview.releases_url"
                :href="preview.releases_url"
                target="_blank"
                rel="noopener noreferrer"
                class="fdm-focus inline-flex items-center gap-1 rounded text-run transition hover:underline"
              >
                All releases
                <LucideExternalLink class="h-3.5 w-3.5" />
              </a>
            </footer>
          </aside>
        </Transition>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import LucideExternalLink from '~icons/lucide/external-link'
import LucideFileText from '~icons/lucide/file-text'
import LucideX from '~icons/lucide/x'
import type { ChangelogPreview } from '../api/updateAdvisor'
import { useFocusTrap } from '../composables/useFocusTrap'
import EmptyState from './EmptyState.vue'

const props = defineProps<{
  open: boolean
  loading: boolean
  error: string
  preview: ChangelogPreview | null
}>()

defineEmits<{ close: [] }>()

const el = ref<HTMLElement>()
const { activate, deactivate } = useFocusTrap(el)
watch(() => props.open, (open) => (open ? activate() : deactivate()), { flush: 'post' })
</script>
