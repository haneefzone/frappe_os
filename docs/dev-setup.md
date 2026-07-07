# Dev & test workflow

> Seeded by the Technical Architect for DOO-42 (test-target provisioning).
> Session 0.4 extends this doc with the full control-plane dev workflow;
> the sections below are the authoritative test-target conventions.

## Topology

Everything runs on the operator's Windows machine under Hyper-V, on the
**Default Switch** NAT network (`172.30.0.0/20` — addresses are
DHCP-assigned and can change across Windows host reboots; re-check with
`ip -4 addr` after a reboot):

| Machine | Role | Rules |
|---|---|---|
| Windows host | Hyper-V host, takes/restores checkpoints | — |
| `dev` VM (`172.30.14.160` at time of writing) | Paperclip + FDM control-plane dev + shared study bench | Study bench is **READ-ONLY**. Never run destructive tests here. |
| `fdm-test` VM | Throwaway destructive-test target | Nothing valuable ever lives here. Rebuild/restore freely. |

## fdm-test VM spec

- Ubuntu Server 24.04 LTS, Gen 2 VM, Default Switch
- 4 vCPU, 6–8 GB RAM (dynamic OK), 60 GB disk
  (it must host one or more Frappe benches — see plan session 1.6)
- Created by the operator; provisioned by running
  `scripts/provision-test-target.sh` once from the VM console

## SSH access

- Service account: `fdm` (passwordless sudo — throwaway VM policy)
- Key on the dev VM: `~/.ssh/fdm_test_target_ed25519` (private),
  `.pub` alongside; the pubkey is embedded in the provisioning script
- Connect: `ssh -i ~/.ssh/fdm_test_target_ed25519 fdm@<fdm-test-ip>`
- The FDM control plane registers this host in the Server registry
  (plan session 1.2) using the same key

## Snapshot-before-destructive-test workflow

Destructive tests (backup, restore-over, drop-site) are only repeatable
because the target can be reset. From **PowerShell (admin) on the
Windows host**:

```powershell
# One-time, right after provisioning succeeds:
Checkpoint-VM -Name fdm-test -SnapshotName baseline

# After installing benches/sites worth keeping as a new starting point:
Checkpoint-VM -Name fdm-test -SnapshotName "bench-ready"

# Reset before/after a destructive test run:
Restore-VMSnapshot -VMName fdm-test -Name "bench-ready" -Confirm:$false
Start-VM -Name fdm-test
```

Notes:

- The VM's IP usually survives a checkpoint restore (same MAC → same
  DHCP lease), but verify after restore and update the Server registry
  entry if it changed.
- Restoring rolls back the SSH host key too, so known-host pinning
  stays valid. If the VM is *rebuilt* from scratch instead, the host
  key changes — re-run the provisioning script and re-pin.

## Throwaway-VM policy

- Never store credentials, client data, or unpushed work on `fdm-test`.
- Any state worth keeping is a checkpoint, not a promise.
- If the VM gets into a weird state, restore `baseline` rather than
  debugging it.
