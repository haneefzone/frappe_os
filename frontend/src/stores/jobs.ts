/**
 * The jobs store: one source of truth for the live Jobs table and the global
 * job tray. It polls `GET /api/jobs` on an adaptive interval — fast while work
 * is in flight, slow when idle — and reference-counts its consumers so the poll
 * loop only runs while something is watching (the tray, mounted app-wide, keeps
 * it alive; pages add themselves on top).
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { type Job, jobsApi } from '../api/jobs'
import { isTerminal } from '../lib/jobs'

const FAST_MS = 3000
const IDLE_MS = 8000

export const useJobsStore = defineStore('jobs', () => {
  // Newest-first list of recent jobs, kept small for the tray/table.
  const jobs = ref<Job[]>([])
  const loaded = ref(false)
  const error = ref<string | null>(null)

  const running = computed(() => jobs.value.filter((j) => !isTerminal(j.status)))
  const runningCount = computed(() => running.value.length)

  let consumers = 0
  let timer: ReturnType<typeof setTimeout> | null = null
  let inFlight = false

  function upsert(list: Job[]) {
    // Merge fetched rows over any locally-added ones, preserving newest-first.
    const byId = new Map<number, Job>()
    for (const j of list) byId.set(j.id, j)
    for (const j of jobs.value) if (!byId.has(j.id)) byId.set(j.id, j)
    jobs.value = [...byId.values()].sort((a, b) => b.id - a.id).slice(0, 100)
  }

  async function refresh() {
    if (inFlight) return
    inFlight = true
    try {
      const list = await jobsApi.list({ limit: 50 })
      jobs.value = list
      loaded.value = true
      error.value = null
    } catch (e) {
      error.value = e instanceof Error ? e.message : 'Could not load jobs.'
    } finally {
      inFlight = false
    }
  }

  function schedule() {
    if (consumers <= 0) return
    const delay = runningCount.value > 0 ? FAST_MS : IDLE_MS
    timer = setTimeout(async () => {
      await refresh()
      schedule()
    }, delay)
  }

  /** Register a consumer; returns a release fn. The first consumer kicks off an
   *  immediate refresh + the poll loop; the last release stops it. */
  function use(): () => void {
    consumers += 1
    if (consumers === 1) {
      void refresh().then(schedule)
    }
    let released = false
    return () => {
      if (released) return
      released = true
      consumers -= 1
      if (consumers <= 0 && timer !== null) {
        clearTimeout(timer)
        timer = null
      }
    }
  }

  /** Insert/replace one job (e.g. right after launching it) so the tray and
   *  table reflect it before the next poll lands. */
  function merge(job: Job) {
    upsert([job])
  }

  return { jobs, loaded, error, running, runningCount, refresh, use, merge }
})
