<template>
  <div class="flex h-full flex-col">
    <header class="flex items-center gap-3 border-b border-line px-8 py-5">
      <button
        type="button"
        class="fdm-focus rounded p-1 text-ink-3 transition hover:bg-raised hover:text-ink-1"
        aria-label="Back to sites"
        @click="router.push('/sites')"
      >
        <LucideArrowLeft class="h-4 w-4" />
      </button>
      <StatusDot v-if="site" :status="siteDot(site.status)" />
      <h1 class="truncate text-lg font-semibold text-ink-1">{{ site?.name ?? 'Site' }}</h1>
      <EnvironmentBadge v-if="site" :env="site.server_env_tag" />
      <StatusBadge
        v-if="site?.maintenance_mode"
        status="warn"
        label="Maintenance"
      />
    </header>

    <!-- Tab navigation -->
    <nav
      role="tablist"
      aria-label="Site sections"
      class="flex shrink-0 border-b border-line px-8"
    >
      <button
        v-for="tab in TABS"
        :key="tab.key"
        role="tab"
        type="button"
        :aria-selected="activeTab === tab.key"
        class="fdm-focus -mb-px border-b-2 px-4 py-3 text-label font-medium transition"
        :class="
          activeTab === tab.key
            ? 'border-ink-1 text-ink-1'
            : 'border-transparent text-ink-3 hover:text-ink-2'
        "
        @click="switchTab(tab.key)"
      >
        {{ tab.label }}
      </button>
    </nav>

    <!-- Overview tab -->
    <div v-if="activeTab === 'overview'" class="min-h-0 flex-1 overflow-y-auto p-8">
      <p v-if="loadError" class="text-label text-err" role="alert">{{ loadError }}</p>

      <div v-else-if="loading" class="max-w-3xl rounded-lg border border-line bg-surface">
        <div class="border-b border-line px-4 py-2.5"><div class="h-3.5 w-24 animate-pulse rounded bg-raised" /></div>
        <div class="divide-y divide-line">
          <div v-for="i in 6" :key="i" class="flex items-center justify-between px-4 py-2">
            <div class="h-3 w-28 animate-pulse rounded bg-raised" />
            <div class="h-3 w-40 animate-pulse rounded bg-raised" />
          </div>
        </div>
      </div>

      <div v-else-if="site" class="grid max-w-4xl gap-6 lg:grid-cols-2">
        <!-- Health card (uptime + response time) — session 2.7 -->
        <section class="rounded-lg border border-line bg-surface lg:col-span-2">
          <div class="flex items-center justify-between border-b border-line px-4 py-2.5">
            <h2 class="text-label font-semibold text-ink-1">Health</h2>
            <div v-if="canOperate" class="flex items-center gap-2">
              <span class="text-meta text-ink-3">Uptime checks</span>
              <Button
                variant="subtle"
                theme="gray"
                size="sm"
                :label="site.uptime_enabled ? 'Disable' : 'Enable'"
                :loading="uptimeToggling"
                :disabled="uptimeToggling"
                @click="toggleUptime"
              />
            </div>
          </div>

          <div v-if="!site.uptime_enabled" class="px-4 py-4">
            <p class="text-label text-ink-3">
              Uptime checking is off for this site. Turn it on to record external
              HTTP checks and response times.
            </p>
          </div>

          <div v-else class="grid gap-4 p-4 sm:grid-cols-3">
            <!-- Current status -->
            <div class="flex flex-col gap-1">
              <span class="text-meta uppercase tracking-wide text-ink-3">Status</span>
              <span class="flex items-center gap-2 text-label text-ink-1">
                <StatusDot :status="healthDot(site.health)" />
                {{ currentlyUpLabel }}
              </span>
              <span v-if="uptime?.summary.last_status_code != null" class="text-meta text-ink-3">
                HTTP {{ uptime.summary.last_status_code }}
              </span>
            </div>

            <!-- Uptime percentages -->
            <div class="flex flex-col gap-1">
              <span class="text-meta uppercase tracking-wide text-ink-3">Uptime</span>
              <span class="text-label text-ink-1">
                {{ pctLabel(uptime?.summary.uptime_24h_pct) }}
                <span class="text-meta text-ink-3">24h</span>
              </span>
              <span class="text-label text-ink-1">
                {{ pctLabel(uptime?.summary.uptime_30d_pct) }}
                <span class="text-meta text-ink-3">30d</span>
              </span>
            </div>

            <!-- Response time + sparkline -->
            <div class="flex flex-col gap-1">
              <span class="text-meta uppercase tracking-wide text-ink-3">Response time</span>
              <span class="font-mono text-label text-ink-1">{{ lastLatencyLabel }}</span>
              <div class="mt-1 h-8">
                <Sparkline
                  v-if="latencySeries.length > 1"
                  :data="latencySeries"
                  status="info"
                  filled
                  show-last
                  :width="140"
                  :height="32"
                />
                <span v-else class="text-meta text-ink-3">Collecting samples…</span>
              </div>
            </div>
          </div>

          <p
            v-if="site.uptime_enabled && site.check_url"
            class="border-t border-line px-4 py-2 text-meta text-ink-3"
          >
            Checking <span class="font-mono text-ink-2">{{ site.check_url }}</span>
          </p>
        </section>

        <!-- Overview -->
        <section class="rounded-lg border border-line bg-surface">
          <h2 class="border-b border-line px-4 py-2.5 text-label font-semibold text-ink-1">Overview</h2>
          <dl class="divide-y divide-line">
            <div v-for="spec in specs" :key="spec.label" class="flex items-center justify-between px-4 py-2">
              <dt class="text-meta uppercase tracking-wide text-ink-3">{{ spec.label }}</dt>
              <dd class="max-w-[62%] truncate text-label text-ink-1" :title="spec.value">{{ spec.value }}</dd>
            </div>
            <!-- Environment classification row (DOO-988) -->
            <div class="flex items-center justify-between px-4 py-2.5">
              <dt class="text-meta uppercase tracking-wide text-ink-3">Environment</dt>
              <dd class="flex items-center gap-2">
                <EnvironmentBadge :env="site.environment" />
                <select
                  v-if="canOperate"
                  :value="site.environment"
                  :disabled="envClassifying"
                  class="fdm-focus rounded border border-line bg-base px-2 py-1 text-meta text-ink-1 disabled:opacity-50"
                  aria-label="Change environment classification"
                  @change="classifyEnv(($event.target as HTMLSelectElement).value as SiteEnvironment)"
                >
                  <option value="dev">dev</option>
                  <option value="staging">staging</option>
                  <option value="prod">prod</option>
                </select>
                <span v-if="envClassifying" class="text-meta text-ink-3">Saving…</span>
              </dd>
            </div>
          </dl>
          <!-- Nudge: unclassified sites silently skip the prod guardrail (DOO-988) -->
          <div
            v-if="site.environment === 'dev'"
            class="border-t border-warn/30 bg-warn/5 px-4 py-3 text-meta text-warn"
            role="note"
          >
            This site is classified <strong>dev</strong>. If it serves real users, change it to
            <strong>prod</strong> so the safe-update pipeline requires a sign-off before promoting.
          </div>
        </section>

        <!-- Quick actions -->
        <section class="rounded-lg border border-line bg-surface">
          <h2 class="border-b border-line px-4 py-2.5 text-label font-semibold text-ink-1">Quick actions</h2>
          <div class="space-y-4 p-4">
            <!-- Open site -->
            <div>
              <a
                v-if="site.url"
                :href="site.url"
                target="_blank"
                rel="noopener"
                class="fdm-focus inline-flex items-center gap-2 rounded-lg border border-line px-3 py-2 text-label font-medium text-ink-1 transition hover:border-line-strong hover:bg-raised"
              >
                Open site
                <LucideExternalLink class="h-3.5 w-3.5" />
              </a>
              <span v-else class="text-label text-ink-3">Open site — port unknown (run a discovery).</span>
              <p class="mt-1 text-meta text-ink-3">
                Opens <span class="font-mono">{{ site.url ?? '—' }}</span>. The dev server matches on
                the <strong>Host header</strong> ({{ site.name }}); if it 404s, add
                <code class="font-mono">{{ site.name }}</code> to your hosts file or send the header.
              </p>
            </div>

            <!-- Scheduler toggle -->
            <div class="flex items-center justify-between gap-3 border-t border-line pt-4">
              <div>
                <p class="text-label font-medium text-ink-1">Scheduler</p>
                <p class="text-meta text-ink-3">{{ schedulerLabel(site.scheduler_enabled) }}</p>
              </div>
              <Button
                v-if="canOperate"
                variant="subtle"
                theme="gray"
                :label="site.scheduler_enabled ? 'Disable' : 'Enable'"
                :loading="busy === 'scheduler'"
                :disabled="!!busy"
                @click="toggleScheduler"
              />
            </div>

            <!-- Maintenance toggle -->
            <div class="flex items-center justify-between gap-3 border-t border-line pt-4">
              <div>
                <p class="text-label font-medium text-ink-1">Maintenance mode</p>
                <p class="text-meta text-ink-3">{{ site.maintenance_mode ? 'On — the site is offline to users' : 'Off' }}</p>
              </div>
              <Button
                v-if="canOperate"
                :variant="site.maintenance_mode ? 'subtle' : 'solid'"
                theme="gray"
                :label="site.maintenance_mode ? 'Turn off' : 'Turn on'"
                :loading="busy === 'maintenance'"
                :disabled="!!busy"
                @click="toggleMaintenance"
              />
            </div>

            <!-- Maintenance actions (session 1.10) — each launches a job -->
            <div v-if="canOperate" class="border-t border-line pt-4">
              <p class="text-label font-medium text-ink-1">Maintenance</p>
              <p class="mt-0.5 text-meta text-ink-3">
                Each action runs as a job — you'll land on its live log.
              </p>
              <div class="mt-2 flex flex-wrap gap-2">
                <Button
                  v-if="canBackup"
                  variant="subtle"
                  theme="gray"
                  label="Back up"
                  :disabled="!!busy || maintLaunching || backingUp"
                  :loading="backingUp"
                  @click="backupNow"
                />
                <Button
                  variant="subtle"
                  theme="gray"
                  label="Migrate"
                  :disabled="!!busy || maintLaunching"
                  @click="askMaint('migrate')"
                />
                <Button
                  variant="subtle"
                  theme="gray"
                  label="Clear cache"
                  :disabled="!!busy || maintLaunching"
                  @click="askMaint('clear-cache')"
                />
                <Button
                  variant="subtle"
                  theme="gray"
                  label="Clear website cache"
                  :disabled="!!busy || maintLaunching"
                  @click="askMaint('clear-website-cache')"
                />
              </div>
            </div>
          </div>
        </section>

        <!-- Installed apps (full width) -->
        <section class="rounded-lg border border-line bg-surface lg:col-span-2">
          <div class="flex items-center justify-between border-b border-line px-4 py-2.5">
            <h2 class="text-label font-semibold text-ink-1">Installed apps</h2>
            <Button
              v-if="canManage"
              variant="subtle"
              theme="gray"
              label="Install app"
              :disabled="!!busy"
              @click="openInstall"
            >
              <template #prefix><LucidePackagePlus class="h-3.5 w-3.5" /></template>
            </Button>
          </div>

          <p v-if="appsError" class="px-4 py-3 text-label text-err" role="alert">{{ appsError }}</p>

          <div v-else-if="appsLoading" class="divide-y divide-line">
            <div v-for="i in 3" :key="i" class="flex items-center gap-6 px-4 py-3">
              <div class="h-3.5 w-32 animate-pulse rounded bg-raised" />
              <div class="h-3.5 w-20 animate-pulse rounded bg-raised" />
              <div class="h-3.5 w-16 animate-pulse rounded bg-raised" />
            </div>
          </div>

          <EmptyState
            v-else-if="siteApps.length === 0"
            :icon="LucidePackage"
            title="No apps installed"
            message="Use the Install app button above to add a Frappe app to this site."
          />

          <table v-else class="w-full text-left">
            <thead>
              <tr class="border-b border-line text-meta uppercase tracking-wide text-ink-3">
                <th class="px-4 py-2 font-medium">App</th>
                <th class="px-4 py-2 font-medium">Branch</th>
                <th class="px-4 py-2 font-medium">Version</th>
                <th class="px-4 py-2 font-medium">Installed</th>
                <th v-if="canRemove" class="px-4 py-2 text-right font-medium">Actions</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-line text-label">
              <tr v-for="a in siteApps" :key="a.id">
                <td class="px-4 py-2.5 font-medium text-ink-1">{{ a.app_name }}</td>
                <td class="px-4 py-2.5 font-mono text-ink-2">{{ a.branch ?? '—' }}</td>
                <td class="px-4 py-2.5">
                  <span
                    v-if="a.version"
                    class="inline-block rounded-full border border-line bg-raised px-2 py-0.5 font-mono text-meta text-ink-1"
                  >
                    {{ a.version }}
                  </span>
                  <span v-else class="text-ink-3">—</span>
                </td>
                <td class="px-4 py-2.5 text-ink-3" :title="absoluteTime(a.installed_at)">
                  {{ relativeTime(a.installed_at) }}
                </td>
                <td v-if="canRemove" class="px-4 py-2.5">
                  <div class="flex justify-end">
                    <Button
                      variant="subtle"
                      theme="red"
                      size="sm"
                      label="Remove"
                      :disabled="!!busy"
                      @click="askUninstall(a)"
                    />
                  </div>
                </td>
              </tr>
            </tbody>
          </table>
        </section>

        <!-- Config drift for this site (session 6.7) -->
        <section class="rounded-lg border border-line bg-surface lg:col-span-2">
          <div class="flex items-center justify-between border-b border-line px-4 py-2.5">
            <h2 class="text-label font-semibold text-ink-1">Config drift</h2>
            <span v-if="siteDriftBaselines.length" class="text-meta text-ink-3">
              {{ siteDriftBaselines.filter(b => b.status === 'drifted').length }} drifted of {{ siteDriftBaselines.length }}
            </span>
          </div>
          <div v-if="driftLoading" class="flex flex-wrap gap-2 p-4">
            <div v-for="i in 3" :key="i" class="h-6 w-28 animate-pulse rounded-full bg-raised" />
          </div>
          <div v-else-if="siteDriftBaselines.length === 0" class="px-4 py-3 text-label text-ink-3">
            No config baselines tracked for this site yet.
          </div>
          <div v-else class="flex flex-wrap gap-2 p-4">
            <DriftChip
              v-for="b in siteDriftBaselines"
              :key="b.id"
              :baseline="b"
              @click="activeDriftId = b.id"
            />
          </div>
        </section>
      </div>
    </div>

    <!-- Domains & SSL tab -->
    <div v-else-if="activeTab === 'domains'" class="min-h-0 flex-1 overflow-y-auto p-8">
      <div class="max-w-5xl">
        <!-- Header row: title + add domain button -->
        <div class="mb-4 flex items-center justify-between">
          <div>
            <h2 class="text-section font-semibold text-ink-1">Domains & SSL</h2>
            <p class="text-label text-ink-3">Custom hostnames with nginx vhost and Let's Encrypt certificates.</p>
          </div>
          <Button
            v-if="canSslManage"
            variant="subtle"
            theme="gray"
            label="Add domain"
            @click="openAddDomain"
          >
            <template #prefix><LucidePlus class="h-3.5 w-3.5" /></template>
          </Button>
        </div>

        <p v-if="domainsError" class="mb-3 text-label text-err" role="alert">{{ domainsError }}</p>

        <!-- Loading skeleton -->
        <div v-if="domainsLoading" class="rounded-lg border border-line bg-surface">
          <div class="border-b border-line px-4 py-2.5">
            <div class="h-3.5 w-24 animate-pulse rounded bg-raised" />
          </div>
          <div class="divide-y divide-line">
            <div v-for="i in 3" :key="i" class="flex items-center gap-6 px-4 py-3">
              <div class="h-3 w-40 animate-pulse rounded bg-raised" />
              <div class="h-3 w-16 animate-pulse rounded bg-raised" />
              <div class="h-3 w-24 animate-pulse rounded bg-raised" />
            </div>
          </div>
        </div>

        <!-- Empty state -->
        <EmptyState
          v-else-if="domains.length === 0"
          :icon="LucideGlobe"
          title="No domains"
          message="Add a hostname to generate a nginx vhost and issue an SSL certificate."
        />

        <!-- Domains table -->
        <div v-else class="rounded-lg border border-line bg-surface">
          <table class="w-full text-left">
            <thead>
              <tr class="border-b border-line text-meta uppercase tracking-wide text-ink-3">
                <th class="px-4 py-2 font-medium">Domain</th>
                <th class="px-4 py-2 font-medium">DNS</th>
                <th class="px-4 py-2 font-medium">SSL</th>
                <th class="px-4 py-2 font-medium">Last checked</th>
                <th class="px-4 py-2 font-medium">Error</th>
                <th v-if="canSslManage" class="px-4 py-2 text-right font-medium">Actions</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-line text-label">
              <tr v-for="d in domains" :key="d.id">
                <!-- Domain name + primary badge -->
                <td class="px-4 py-2.5">
                  <span class="font-mono text-ink-1">{{ d.domain }}</span>
                  <span
                    v-if="d.is_primary"
                    class="ml-2 inline-block rounded-full border border-line bg-raised px-1.5 py-0.5 text-meta text-ink-3"
                  >
                    primary
                  </span>
                </td>

                <!-- DNS status -->
                <td class="px-4 py-2.5">
                  <span class="flex items-center gap-1.5">
                    <StatusDot :status="dnsDot(d.dns_ok)" size="sm" />
                    <span class="text-ink-2">{{ dnsLabel(d.dns_ok) }}</span>
                  </span>
                </td>

                <!-- SSL status badge -->
                <td class="px-4 py-2.5">
                  <StatusBadge :status="sslStatus(d)" :label="sslLabel(d)" />
                </td>

                <!-- Last checked timestamp -->
                <td
                  class="px-4 py-2.5 text-ink-3"
                  :title="d.last_checked ? absoluteTime(d.last_checked) : undefined"
                >
                  {{ d.last_checked ? relativeTime(d.last_checked) : '—' }}
                </td>

                <!-- Last error (truncated) -->
                <td
                  class="max-w-xs truncate px-4 py-2.5 text-ink-3"
                  :title="d.last_error ?? undefined"
                >
                  {{ d.last_error ?? '—' }}
                </td>

                <!-- Action buttons (ssl:manage only) -->
                <td v-if="canSslManage" class="px-4 py-2.5">
                  <div class="flex justify-end gap-1.5">
                    <Button
                      variant="subtle"
                      theme="gray"
                      size="sm"
                      label="Test DNS"
                      :disabled="domainLaunching === d.id"
                      @click="launchTestDns(d)"
                    />
                    <Button
                      variant="subtle"
                      theme="gray"
                      size="sm"
                      label="Render vhost"
                      :disabled="domainLaunching === d.id"
                      @click="launchRenderVhost(d)"
                    />
                    <Button
                      variant="subtle"
                      theme="gray"
                      size="sm"
                      label="Issue certificate"
                      :disabled="domainLaunching === d.id"
                      @click="openIssueCert(d)"
                    />
                    <Button
                      variant="subtle"
                      theme="red"
                      size="sm"
                      label="Remove"
                      :disabled="domainLaunching === d.id"
                      @click="askRemoveDomain(d)"
                    />
                  </div>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- Add domain modal -->
    <Teleport to="body">
      <Transition
        enter-active-class="transition duration-150 ease-out"
        enter-from-class="opacity-0"
        leave-active-class="transition duration-150 ease-out"
        leave-to-class="opacity-0"
      >
        <div
          v-if="addDomainOpen"
          class="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
          @click.self="closeAddDomain"
        >
          <div
            role="dialog"
            aria-modal="true"
            aria-label="Add domain"
            class="w-full max-w-md rounded-lg border border-line bg-raised"
          >
            <div class="border-b border-line px-5 py-4">
              <h2 class="text-section font-semibold text-ink-1">Add domain</h2>
              <p class="mt-0.5 text-label text-ink-2">Bind a hostname to {{ site?.name }}.</p>
            </div>

            <div class="space-y-4 px-5 py-4">
              <div>
                <label class="mb-1 block text-meta font-medium uppercase tracking-wide text-ink-2" for="add-domain-hostname">
                  Hostname
                </label>
                <input
                  id="add-domain-hostname"
                  v-model.trim="addDomainForm.domain"
                  v-bind="modalInput"
                  placeholder="erp.example.com"
                  autocomplete="off"
                  autocapitalize="off"
                  spellcheck="false"
                  @input="addDomainForm.domainError = ''"
                />
                <p v-if="addDomainForm.domainError" class="mt-1 text-label text-err" role="alert">
                  {{ addDomainForm.domainError }}
                </p>
                <p v-else-if="addDomainForm.domain && !isValidHostname" class="mt-1 text-label text-warn">
                  Must be a valid lowercase hostname, e.g. erp.example.com.
                </p>
              </div>

              <label class="flex cursor-pointer items-center gap-2.5">
                <input v-model="addDomainForm.isPrimary" type="checkbox" class="h-4 w-4 rounded border-line accent-white" />
                <span class="text-label text-ink-1">Make primary</span>
                <span class="text-meta text-ink-3">(replaces current primary)</span>
              </label>
            </div>

            <div class="flex justify-end gap-2 border-t border-line px-5 py-3.5">
              <Button variant="subtle" theme="gray" label="Cancel" :disabled="addDomainLoading" @click="closeAddDomain" />
              <Button
                variant="solid"
                theme="gray"
                label="Add domain"
                :loading="addDomainLoading"
                :disabled="!isValidHostname || addDomainLoading"
                @click="submitAddDomain"
              />
            </div>
          </div>
        </div>
      </Transition>
    </Teleport>

    <!-- Issue certificate modal -->
    <Teleport to="body">
      <Transition
        enter-active-class="transition duration-150 ease-out"
        enter-from-class="opacity-0"
        leave-active-class="transition duration-150 ease-out"
        leave-to-class="opacity-0"
      >
        <div
          v-if="issueCertOpen"
          class="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
          @click.self="issueCertOpen = false"
        >
          <div
            role="dialog"
            aria-modal="true"
            aria-label="Issue certificate"
            class="w-full max-w-md rounded-lg border border-line bg-raised"
          >
            <div class="border-b border-line px-5 py-4">
              <h2 class="text-section font-semibold text-ink-1">Issue certificate</h2>
              <p class="mt-0.5 text-label text-ink-2">
                Request a Let's Encrypt certificate for
                <span class="font-mono text-ink-1">{{ issueCertTarget?.domain }}</span>.
              </p>
            </div>

            <div class="space-y-4 px-5 py-4">
              <div>
                <label class="mb-1 block text-meta font-medium uppercase tracking-wide text-ink-2" for="issue-cert-email">
                  Contact email
                </label>
                <input
                  id="issue-cert-email"
                  v-model.trim="issueCertEmail"
                  v-bind="modalInput"
                  type="email"
                  placeholder="admin@example.com"
                  autocomplete="email"
                />
              </div>
              <p class="text-label text-ink-3">
                DNS must already point at this server. The certbot
                <span class="font-mono">--webroot</span> flow runs against the Frappe site's
                document root.
              </p>
            </div>

            <div class="flex justify-end gap-2 border-t border-line px-5 py-3.5">
              <Button variant="subtle" theme="gray" label="Cancel" :disabled="issueCertLoading" @click="issueCertOpen = false" />
              <Button
                variant="solid"
                theme="gray"
                label="Issue certificate"
                :loading="issueCertLoading"
                :disabled="!issueCertEmail || issueCertLoading"
                @click="submitIssueCert"
              />
            </div>
          </div>
        </div>
      </Transition>
    </Teleport>

    <!-- Remove domain confirm -->
    <ConfirmModal
      v-model="removeDomainOpen"
      title="Remove domain"
      :message="removeDomainTarget ? `Remove ${removeDomainTarget.domain} from ${site?.name}.` : ''"
      verb="Remove domain"
      :loading="removeDomainLoading"
      :consequences="[
        'Deletes the domain record from FDM Platform.',
        'The nginx vhost and any Let\'s Encrypt certificate remain on the server until removed out of band.',
      ]"
      @confirm="confirmRemoveDomain"
    />

    <!-- Install picker modal -->
    <Teleport to="body">
      <Transition
        enter-active-class="transition duration-150 ease-out"
        enter-from-class="opacity-0"
        leave-active-class="transition duration-150 ease-out"
        leave-to-class="opacity-0"
      >
        <div
          v-if="installOpen"
          class="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
          @click.self="closeInstall"
        >
          <div
            role="dialog"
            aria-modal="true"
            aria-label="Install app"
            class="w-full max-w-md rounded-lg border border-line bg-raised"
          >
            <div class="border-b border-line px-5 py-4">
              <h2 class="text-section font-semibold text-ink-1">Install app</h2>
              <p class="mt-0.5 text-label text-ink-2">Install a Frappe app on {{ site?.name }}.</p>
            </div>

            <div class="max-h-[60vh] space-y-4 overflow-y-auto px-5 py-4">
              <!-- Mode toggle -->
              <div class="grid grid-cols-2 gap-2" role="radiogroup" aria-label="Install method">
                <button
                  v-for="opt in installModes"
                  :key="opt.value"
                  type="button"
                  role="radio"
                  :aria-checked="installMode === opt.value"
                  class="fdm-focus rounded-lg border px-3 py-2 text-left text-label transition"
                  :class="installMode === opt.value
                    ? 'border-line-strong bg-surface text-ink-1'
                    : 'border-line text-ink-2 hover:border-line-strong'"
                  @click="installMode = opt.value"
                >
                  <span class="block font-medium text-ink-1">{{ opt.label }}</span>
                  <span class="block text-meta text-ink-3">{{ opt.hint }}</span>
                </button>
              </div>

              <!-- Saved source / marketplace -->
              <template v-if="installMode === 'source'">
                <div>
                  <label class="mb-1 block text-meta font-medium uppercase tracking-wide text-ink-2" for="pick-source">
                    Saved source
                  </label>
                  <select id="pick-source" v-model.number="installForm.sourceId" v-bind="modalInput">
                    <option :value="null">Marketplace name (type below)</option>
                    <option v-for="s in sources" :key="s.id" :value="s.id">{{ s.name }} — {{ s.repo_url }}</option>
                  </select>
                </div>
                <div v-if="installForm.sourceId == null">
                  <label class="mb-1 block text-meta font-medium uppercase tracking-wide text-ink-2" for="mkt-name">
                    Marketplace app name
                  </label>
                  <input id="mkt-name" v-model.trim="installForm.marketplace" v-bind="modalInput" placeholder="erpnext" />
                </div>
              </template>

              <!-- GitHub URL -->
              <template v-else>
                <div>
                  <label class="mb-1 block text-meta font-medium uppercase tracking-wide text-ink-2" for="gh-url">
                    Repository URL
                  </label>
                  <input id="gh-url" v-model.trim="installForm.repoUrl" v-bind="modalInput" placeholder="https://github.com/frappe/erpnext" />
                </div>
                <p class="rounded-lg border border-warn/40 bg-warn/10 px-3 py-2.5 text-label text-warn">
                  Compatibility with this Frappe version is unverified — test before installing on
                  production.
                </p>
              </template>

              <!-- Branch (both modes) -->
              <div>
                <div class="mb-1 flex items-center justify-between">
                  <label class="block text-meta font-medium uppercase tracking-wide text-ink-2" for="branch-pick">
                    Branch
                  </label>
                  <Button
                    v-if="installMode === 'github'"
                    variant="subtle"
                    theme="gray"
                    size="sm"
                    :label="fetchingBranches ? 'Fetching…' : 'Fetch branches'"
                    :loading="fetchingBranches"
                    :disabled="!installForm.repoUrl"
                    @click="fetchBranches"
                  />
                </div>
                <select v-if="branches.length" id="branch-pick" v-model="installForm.branch" v-bind="modalInput">
                  <option v-for="b in branches" :key="b" :value="b">{{ b }}</option>
                </select>
                <input
                  v-else
                  id="branch-pick"
                  v-model.trim="installForm.branch"
                  v-bind="modalInput"
                  placeholder="Optional, e.g. version-15"
                />
                <p v-if="branchError" class="mt-1 text-label text-err" role="alert">{{ branchError }}</p>
              </div>
            </div>

            <div class="flex justify-end gap-2 border-t border-line px-5 py-3.5">
              <Button variant="subtle" theme="gray" label="Cancel" :disabled="installing" @click="closeInstall" />
              <Button
                variant="solid"
                theme="gray"
                label="Install app"
                :loading="installing"
                :disabled="!canInstall"
                @click="submitInstall"
              />
            </div>
          </div>
        </div>
      </Transition>
    </Teleport>

    <!-- Uninstall confirm (type the app name) -->
    <ConfirmModal
      v-model="uninstallOpen"
      title="Remove app"
      :message="uninstallTarget ? `Uninstall “${uninstallTarget.app_name}” from ${site?.name}.` : ''"
      verb="Remove app"
      variant="destructive"
      :target-name="uninstallTarget?.app_name"
      :loading="!!busy"
      :consequences="[
        'Runs bench uninstall-app on this site.',
        'App data and tables are removed from the site.',
      ]"
      @confirm="confirmUninstall"
    />

    <!-- Maintenance confirm (migrate / clear-cache / clear-website-cache) -->
    <ConfirmModal
      v-model="maintOpen"
      :title="maintConfig.title"
      :message="maintConfig.message"
      :verb="maintConfig.verb"
      :consequences="maintConfig.consequences"
      :loading="maintLaunching"
      @confirm="confirmMaint"
    />

    <DriftDrawer
      :baseline-id="activeDriftId"
      @close="activeDriftId = null"
      @accepted="loadSiteDrift"
    />
  </div>
</template>

<script setup lang="ts">
import { Button } from 'frappe-ui'
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import LucideArrowLeft from '~icons/lucide/arrow-left'
import LucideExternalLink from '~icons/lucide/external-link'
import LucideGlobe from '~icons/lucide/globe'
import LucidePackage from '~icons/lucide/package'
import LucidePackagePlus from '~icons/lucide/package-plus'
import LucidePlus from '~icons/lucide/plus'
import { appsApi, parseBranchesLine, type AppSource, type InstalledApp } from '../api/apps'
import { backupsApi } from '../api/backups'
import { ApiError } from '../api/client'
import { domainsApi, HOSTNAME_RE, type DomainOut } from '../api/domains'
import { jobsApi, streamJobLogs } from '../api/jobs'
import { sitesApi, type Site, type SiteEnvironment, type UptimeSeries } from '../api/sites'
import { updatesApi } from '../api/updates'
import { driftApi, type DriftBaseline } from '../api/drift'
import ConfirmModal from '../components/ConfirmModal.vue'
import DriftChip from '../components/DriftChip.vue'
import DriftDrawer from '../components/DriftDrawer.vue'
import EmptyState from '../components/EmptyState.vue'
import EnvironmentBadge from '../components/EnvironmentBadge.vue'
import Sparkline from '../components/Sparkline.vue'
import StatusBadge from '../components/StatusBadge.vue'
import StatusDot from '../components/StatusDot.vue'
import { toast } from '../components/toast'
import type { Status } from '../components/types'
import {
  healthDot,
  HEALTH_LABEL as healthLabel,
  pctLabel,
  schedulerLabel,
  siteStatusDot as siteDot,
} from '../lib/sites'
import { absoluteTime, relativeTime } from '../lib/servers'
import { useAuthStore } from '../stores/auth'

const route = useRoute()
const router = useRouter()
const siteId = Number(route.params.id)
const auth = useAuthStore()
const canOperate = auth.hasPermission('site:operate')
const canManage = auth.hasPermission('app:manage')
const canRemove = auth.hasPermission('danger')
const canBackup = auth.hasPermission('backup:create')
const canSslManage = auth.hasPermission('ssl:manage')
const backingUp = ref(false)

const envClassifying = ref(false)

async function classifyEnv(env: SiteEnvironment) {
  if (!site.value || envClassifying.value) return
  envClassifying.value = true
  try {
    await updatesApi.setEnvironment(site.value.id, { environment: env })
    site.value = { ...site.value, environment: env }
    toast.success(`Environment set to ${env}.`)
  } catch (error) {
    toast.error(error instanceof Error ? error.message : 'Could not update environment.')
  } finally {
    envClassifying.value = false
  }
}

// -- Tabs --------------------------------------------------------------------
type TabKey = 'overview' | 'domains'
const TABS: { key: TabKey; label: string }[] = [
  { key: 'overview', label: 'Overview' },
  { key: 'domains', label: 'Domains & SSL' },
]
const activeTab = ref<TabKey>('overview')

function switchTab(key: TabKey) {
  activeTab.value = key
  if (key === 'domains' && domains.value.length === 0 && !domainsLoading.value) {
    void loadDomains()
  }
}

const site = ref<Site | null>(null)
const loading = ref(true)
const loadError = ref('')
const busy = ref<'' | 'scheduler' | 'maintenance' | 'app'>('')

const modalInput = {
  class:
    'fdm-focus w-full rounded-lg border border-line bg-base px-2.5 py-1.5 text-label text-ink-1 placeholder:text-ink-3 focus:border-line-strong',
}

const specs = computed(() => {
  const s = site.value
  if (!s) return []
  return [
    { label: 'Bench', value: s.bench_name },
    { label: 'Server', value: `${s.server_name} (${s.server_hostname})` },
    { label: 'Scheduler', value: schedulerLabel(s.scheduler_enabled) },
    { label: 'Maintenance', value: s.maintenance_mode ? 'On' : 'Off' },
    { label: 'Health', value: healthLabel[s.health] },
    { label: 'Status', value: s.status === 'active' ? 'Active' : 'Missing' },
    { label: 'Web port', value: s.webserver_port != null ? String(s.webserver_port) : '—' },
    { label: 'Discovered', value: s.discovered_at ? `${relativeTime(s.discovered_at)} (${absoluteTime(s.discovered_at)})` : 'never' },
  ]
})

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    site.value = await sitesApi.get(siteId)
    void loadSiteDrift()
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : 'Could not load this site.'
  } finally {
    loading.value = false
  }
}

// -- Uptime (session 2.7) ----------------------------------------------------
const uptime = ref<UptimeSeries | null>(null)
const uptimeToggling = ref(false)

/** Latency series for the response-time sparkline (up samples with a value). */
const latencySeries = computed(() =>
  (uptime.value?.samples ?? [])
    .filter((s) => s.latency_ms != null)
    .map((s) => s.latency_ms as number),
)

const currentlyUpLabel = computed(() => {
  const up = uptime.value?.summary.currently_up
  if (up == null) return 'No checks yet'
  return up ? 'Up' : 'Down'
})

const lastLatencyLabel = computed(() => {
  const ms = uptime.value?.summary.last_latency_ms
  return ms != null ? `${Math.round(ms)} ms` : '—'
})

async function loadUptime() {
  try {
    uptime.value = await sitesApi.uptime(siteId, 24)
  } catch {
    // Non-fatal: the health card just shows "collecting samples".
  }
}

async function toggleUptime() {
  if (!site.value || uptimeToggling.value) return
  uptimeToggling.value = true
  try {
    const updated = await sitesApi.setUptimeConfig(siteId, {
      enabled: !site.value.uptime_enabled,
    })
    site.value = updated
    toast.success(`Uptime checks ${updated.uptime_enabled ? 'enabled' : 'disabled'}.`)
    if (updated.uptime_enabled) await loadUptime()
  } catch (error) {
    toast.error(error instanceof Error ? error.message : 'Could not update uptime checks.')
  } finally {
    uptimeToggling.value = false
  }
}

// -- Installed apps ----------------------------------------------------------
const siteApps = ref<InstalledApp[]>([])
const appsLoading = ref(true)
const appsError = ref('')

async function loadApps() {
  appsLoading.value = true
  appsError.value = ''
  try {
    // The endpoint is bench-scoped; filter to this site client-side.
    const all = await appsApi.listInstalled()
    siteApps.value = all.filter((a) => a.site_id === siteId)
  } catch (error) {
    appsError.value = error instanceof Error ? error.message : 'Could not load installed apps.'
  } finally {
    appsLoading.value = false
  }
}

// -- Config drift (session 6.7) -----------------------------------------------
const allServerDrift = ref<DriftBaseline[]>([])
const driftLoading = ref(false)
const activeDriftId = ref<number | null>(null)

const siteDriftBaselines = computed(() =>
  allServerDrift.value.filter((b) => b.site_id === siteId),
)

async function loadSiteDrift() {
  if (!site.value) return
  driftLoading.value = true
  try {
    allServerDrift.value = await driftApi.list({ server_id: site.value.server_id })
  } catch {
    // Non-fatal
  } finally {
    driftLoading.value = false
  }
}

const TERMINAL = new Set(['success', 'failure', 'cancelled'])

/** Poll a launched job to a terminal state; returns the final status. */
async function pollJob(id: number): Promise<string> {
  let status = 'pending'
  for (let i = 0; i < 200 && !TERMINAL.has(status); i++) {
    await new Promise((r) => setTimeout(r, 1000))
    status = (await jobsApi.get(id)).status
  }
  return status
}

// -- Install picker ----------------------------------------------------------
type InstallMode = 'source' | 'github'

const installModes: { value: InstallMode; label: string; hint: string }[] = [
  { value: 'source', label: 'Marketplace / saved source', hint: 'A known app source' },
  { value: 'github', label: 'GitHub URL', hint: 'An arbitrary repo' },
]

const installOpen = ref(false)
const installing = ref(false)
const installMode = ref<InstallMode>('source')
const sources = ref<AppSource[]>([])
const branches = ref<string[]>([])
const fetchingBranches = ref(false)
const branchError = ref('')

const installForm = reactive({
  sourceId: null as number | null,
  marketplace: '',
  repoUrl: '',
  branch: '',
})

const canInstall = computed(() => {
  if (installMode.value === 'github') return installForm.repoUrl.length > 0
  return installForm.sourceId != null || installForm.marketplace.length > 0
})

async function openInstall() {
  installMode.value = 'source'
  branches.value = []
  branchError.value = ''
  Object.assign(installForm, { sourceId: null, marketplace: '', repoUrl: '', branch: '' })
  installOpen.value = true
  try {
    sources.value = await appsApi.listSources()
  } catch {
    // Non-fatal: the user can still install via a marketplace name / URL.
  }
}

function closeInstall() {
  if (!installing.value) installOpen.value = false
}

async function fetchBranches() {
  if (fetchingBranches.value || !installForm.repoUrl || !site.value) return
  fetchingBranches.value = true
  branchError.value = ''
  branches.value = []
  try {
    const job = await appsApi.listBranches({
      server_id: site.value.server_id,
      repo_url: installForm.repoUrl,
    })
    let found: string[] | null = null
    await streamJobLogs(job.id, 0, {
      onLog: (frame) => {
        const parsed = parseBranchesLine(frame.content)
        if (parsed) found = parsed
      },
      onEnd: (info) => {
        if (!found && info.status !== 'success') branchError.value = `Branch listing ended: ${info.status}.`
      },
    })
    if (found) {
      branches.value = found
      if (found.length && !found.includes(installForm.branch)) installForm.branch = found[0]
    } else if (!branchError.value) {
      branchError.value = 'No branches returned — check the URL.'
    }
  } catch (error) {
    branchError.value = error instanceof Error ? error.message : 'Could not list branches.'
  } finally {
    fetchingBranches.value = false
  }
}

async function submitInstall() {
  if (installing.value || !canInstall.value) return
  installing.value = true
  busy.value = 'app'
  try {
    const payload =
      installMode.value === 'github'
        ? { source: installForm.repoUrl, branch: installForm.branch || undefined }
        : installForm.sourceId != null
          ? { app_source_id: installForm.sourceId, branch: installForm.branch || undefined }
          : { source: installForm.marketplace, branch: installForm.branch || undefined }
    const job = await appsApi.install(siteId, payload)
    installOpen.value = false
    const status = await pollJob(job.id)
    if (status === 'success') toast.success('App installed.')
    else if (TERMINAL.has(status)) toast.error(`Install ended: ${status}.`)
    else toast.info('Still installing — refresh shortly.')
    await loadApps()
  } catch (error) {
    const message =
      error instanceof ApiError && error.status === 409
        ? 'A job is already running on this site.'
        : error instanceof Error
          ? error.message
          : 'Could not start the install.'
    toast.error(message)
  } finally {
    installing.value = false
    busy.value = ''
  }
}

// -- Uninstall ---------------------------------------------------------------
const uninstallOpen = ref(false)
const uninstallTarget = ref<InstalledApp | null>(null)

function askUninstall(app: InstalledApp) {
  uninstallTarget.value = app
  uninstallOpen.value = true
}

async function confirmUninstall() {
  const target = uninstallTarget.value
  if (!target || busy.value) return
  busy.value = 'app'
  try {
    const job = await appsApi.uninstall(siteId, target.app_name, target.app_name)
    uninstallOpen.value = false
    const status = await pollJob(job.id)
    if (status === 'success') toast.success('App removed.')
    else if (TERMINAL.has(status)) toast.error(`Uninstall ended: ${status}.`)
    else toast.info('Still running — refresh shortly.')
    await loadApps()
  } catch (error) {
    const message =
      error instanceof ApiError && error.status === 409
        ? 'A job is already running on this site.'
        : error instanceof Error
          ? error.message
          : 'Could not start the uninstall.'
    toast.error(message)
  } finally {
    busy.value = ''
  }
}

async function runToggle(kind: 'scheduler' | 'maintenance', launch: () => Promise<{ id: number }>) {
  if (busy.value) return
  busy.value = kind
  try {
    const job = await launch()
    let status = 'pending'
    for (let i = 0; i < 40 && !TERMINAL.has(status); i++) {
      await new Promise((r) => setTimeout(r, 800))
      status = (await jobsApi.get(job.id)).status
    }
    site.value = await sitesApi.get(siteId)
    if (status === 'success') toast.success(`${kind === 'scheduler' ? 'Scheduler' : 'Maintenance'} updated.`)
    else if (TERMINAL.has(status)) toast.error(`Job ended: ${status}.`)
    else toast.info('Still running — refresh shortly.')
  } catch (error) {
    const message =
      error instanceof ApiError && error.status === 409
        ? 'A job is already running on this site.'
        : error instanceof Error
          ? error.message
          : 'Could not run the action.'
    toast.error(message)
  } finally {
    busy.value = ''
  }
}

function toggleScheduler() {
  if (!site.value) return
  const enable = !site.value.scheduler_enabled
  runToggle('scheduler', () => sitesApi.setScheduler(siteId, enable))
}

function toggleMaintenance() {
  if (!site.value) return
  const enable = !site.value.maintenance_mode
  runToggle('maintenance', () => sitesApi.setMaintenance(siteId, enable))
}

// -- Maintenance actions (session 1.10) --------------------------------------
type MaintKind = 'migrate' | 'clear-cache' | 'clear-website-cache'

const MAINT_META: Record<
  MaintKind,
  { title: string; verb: string; message: string; consequences: string[] }
> = {
  migrate: {
    title: 'Migrate site',
    verb: 'Run migrate',
    message: 'Run pending schema migrations (bench migrate) on this site.',
    consequences: [
      'Applies pending database patches — this can take a while.',
      'Best run during a maintenance window on production.',
    ],
  },
  'clear-cache': {
    title: 'Clear cache',
    verb: 'Clear cache',
    message: 'Flush this site’s Redis and in-process caches (bench clear-cache).',
    consequences: ['Clears cached metadata; the next requests rebuild it.'],
  },
  'clear-website-cache': {
    title: 'Clear website cache',
    verb: 'Clear website cache',
    message: 'Flush this site’s website/page cache (bench clear-website-cache).',
    consequences: ['Clears rendered web pages; they re-render on the next visit.'],
  },
}

const maintOpen = ref(false)
const maintLaunching = ref(false)
const maintKind = ref<MaintKind>('migrate')
const maintConfig = computed(() => MAINT_META[maintKind.value])

function askMaint(kind: MaintKind) {
  maintKind.value = kind
  maintOpen.value = true
}

async function backupNow() {
  if (backingUp.value || busy.value) return
  backingUp.value = true
  try {
    const job = await backupsApi.create(siteId, { with_files: true })
    router.push(`/jobs/${job.id}`)
  } catch (error) {
    const message =
      error instanceof ApiError && error.status === 409
        ? 'A job is already running on this site.'
        : error instanceof Error
          ? error.message
          : 'Could not start the backup.'
    toast.error(message)
  } finally {
    backingUp.value = false
  }
}

async function confirmMaint() {
  if (maintLaunching.value) return
  maintLaunching.value = true
  try {
    const launch =
      maintKind.value === 'migrate'
        ? sitesApi.migrate
        : maintKind.value === 'clear-cache'
          ? sitesApi.clearCache
          : sitesApi.clearWebsiteCache
    const job = await launch(siteId)
    maintOpen.value = false
    router.push(`/jobs/${job.id}`)
  } catch (error) {
    const message =
      error instanceof ApiError && error.status === 409
        ? 'A job is already running on this site.'
        : error instanceof Error
          ? error.message
          : 'Could not start the action.'
    toast.error(message)
  } finally {
    maintLaunching.value = false
  }
}

// -- Domains & SSL -----------------------------------------------------------
const domains = ref<DomainOut[]>([])
const domainsLoading = ref(false)
const domainsError = ref('')
const domainLaunching = ref<number | null>(null)

async function loadDomains() {
  domainsLoading.value = true
  domainsError.value = ''
  try {
    domains.value = await domainsApi.list(siteId)
  } catch (error) {
    domainsError.value = error instanceof Error ? error.message : 'Could not load domains.'
  } finally {
    domainsLoading.value = false
  }
}

function dnsDot(dnsOk: boolean | null): Status {
  if (dnsOk === null) return 'muted'
  return dnsOk ? 'ok' : 'err'
}

function dnsLabel(dnsOk: boolean | null): string {
  if (dnsOk === null) return 'Unknown'
  return dnsOk ? 'OK' : 'Error'
}

function sslStatus(d: DomainOut): Status {
  if (d.cert_status === 'error') return 'err'
  if (d.cert_status === 'issued') {
    if (d.days_left == null || d.days_left > 30) return 'ok'
    if (d.days_left > 7) return 'warn'
    return 'err'
  }
  return 'muted'
}

function sslLabel(d: DomainOut): string {
  if (d.cert_status === 'error') return 'Error'
  if (d.cert_status === 'issued') {
    if (d.days_left == null) return 'Issued'
    if (d.days_left > 30) return 'Issued'
    return `Expires in ${d.days_left}d`
  }
  return 'No cert'
}

async function launchTestDns(d: DomainOut) {
  if (domainLaunching.value === d.id) return
  domainLaunching.value = d.id
  try {
    const job = await domainsApi.testDns(siteId, d.id)
    router.push(`/jobs/${job.id}`)
  } catch (error) {
    toast.error(error instanceof Error ? error.message : 'Could not start DNS check.')
    domainLaunching.value = null
  }
}

async function launchRenderVhost(d: DomainOut) {
  if (domainLaunching.value === d.id) return
  domainLaunching.value = d.id
  try {
    const job = await domainsApi.renderVhost(siteId, d.id)
    router.push(`/jobs/${job.id}`)
  } catch (error) {
    const message =
      error instanceof ApiError && error.status === 409
        ? 'An nginx/SSL job is already running on this server.'
        : error instanceof Error
          ? error.message
          : 'Could not start vhost render.'
    toast.error(message)
    domainLaunching.value = null
  }
}

// Add domain modal
const addDomainOpen = ref(false)
const addDomainLoading = ref(false)
const addDomainForm = reactive({ domain: '', isPrimary: false, domainError: '' })

const isValidHostname = computed(() => HOSTNAME_RE.test(addDomainForm.domain))

function openAddDomain() {
  addDomainForm.domain = ''
  addDomainForm.isPrimary = false
  addDomainForm.domainError = ''
  addDomainOpen.value = true
}

function closeAddDomain() {
  if (!addDomainLoading.value) addDomainOpen.value = false
}

async function submitAddDomain() {
  if (!isValidHostname.value || addDomainLoading.value) return
  addDomainLoading.value = true
  addDomainForm.domainError = ''
  try {
    await domainsApi.add(siteId, {
      domain: addDomainForm.domain,
      is_primary: addDomainForm.isPrimary,
    })
    addDomainOpen.value = false
    toast.success(`Domain ${addDomainForm.domain} added.`)
    await loadDomains()
  } catch (error) {
    const message =
      error instanceof ApiError && error.status === 409
        ? 'That hostname is already registered.'
        : error instanceof Error
          ? error.message
          : 'Could not add domain.'
    addDomainForm.domainError = message
  } finally {
    addDomainLoading.value = false
  }
}

// Issue certificate modal
const issueCertOpen = ref(false)
const issueCertLoading = ref(false)
const issueCertTarget = ref<DomainOut | null>(null)
const issueCertEmail = ref('')

function openIssueCert(d: DomainOut) {
  issueCertTarget.value = d
  issueCertEmail.value = ''
  domainLaunching.value = null
  issueCertOpen.value = true
}

async function submitIssueCert() {
  if (!issueCertEmail.value || !issueCertTarget.value || issueCertLoading.value) return
  issueCertLoading.value = true
  try {
    const job = await domainsApi.issueCert(siteId, issueCertTarget.value.id, {
      email: issueCertEmail.value,
    })
    issueCertOpen.value = false
    router.push(`/jobs/${job.id}`)
  } catch (error) {
    const message =
      error instanceof ApiError && error.status === 409
        ? 'An nginx/SSL job is already running on this server.'
        : error instanceof Error
          ? error.message
          : 'Could not issue certificate.'
    toast.error(message)
  } finally {
    issueCertLoading.value = false
  }
}

// Remove domain modal
const removeDomainOpen = ref(false)
const removeDomainLoading = ref(false)
const removeDomainTarget = ref<DomainOut | null>(null)

function askRemoveDomain(d: DomainOut) {
  removeDomainTarget.value = d
  removeDomainOpen.value = true
}

async function confirmRemoveDomain() {
  const target = removeDomainTarget.value
  if (!target || removeDomainLoading.value) return
  removeDomainLoading.value = true
  try {
    await domainsApi.remove(siteId, target.id)
    removeDomainOpen.value = false
    toast.success(`Domain ${target.domain} removed.`)
    domains.value = domains.value.filter((d) => d.id !== target.id)
  } catch (error) {
    toast.error(error instanceof Error ? error.message : 'Could not remove domain.')
  } finally {
    removeDomainLoading.value = false
  }
}

onMounted(() => {
  load()
  loadApps()
  loadUptime()
})
</script>
