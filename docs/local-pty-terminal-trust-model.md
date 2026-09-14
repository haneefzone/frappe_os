# Local PTY terminal — trust model (DOO-1203)

This is the ISO-review write-up for the WS terminal's **local branch**: an
interactive PTY on a `connection_type='local'` Server. Read it before the code.

## What changes

Before DOO-1199 the browser SSH terminal (FDM 1.5) only spoke to remote hosts
over AsyncSSH. DOO-1199 introduced `Server.connection_type='local'`, a server
that *is* the machine FDM runs on. This card gives the WS terminal a second
backend: an interactive shell launched with `pty.openpty()` +
`asyncio.create_subprocess_exec`, bridged to the same WebSocket the SSH path
uses.

The SSH path is untouched. The only branch point is *how the session is
opened*: `connection_type == 'local'` → local PTY; anything else → the existing
AsyncSSH block, byte-for-byte. Both backends present the identical duck-typed
process interface (`stdin.write` / async-iterable `stdout` /
`change_terminal_size` / `terminate`), so the ticket flow, RBAC gate, audit
row, idle-timeout, resize handling and finalisation are shared, not forked.

## The blast-radius problem

A remote SSH terminal opens a shell on *another* machine; the credential, the
host-key pin and the network boundary all sit between the operator and FDM's
own process. A **local** PTY has none of that: the shell runs on the control
panel host, in the control panel's own process tree. So the safety story is not
"who can we reach" — it is "what can this shell do to the host FDM lives on."

## Invariants

1. **Runs as the FDM service account, never root.** The PTY is launched as the
   OS user the platform process already runs as — no `sudo`, no privilege gain
   over the platform itself. The open is refused outright if that user is
   `root` (`LocalExecRefused`): a local terminal must never be an unrestricted
   root shell. This is the terminal analogue of CLAUDE.md gotcha #1 (bench
   refuses to run as root) and of DOO-1199's `wrap_local_argv` root refusal.

2. **`deploy/fdm-elevate` stays the only privilege path.** We do not wrap the
   shell in `sudo`, and we do not grant it any capability the service account
   lacks. If an operator needs a privileged action they invoke the
   argument-validated, time-boxed `fdm-elevate` helper *from inside* the shell,
   exactly as they would over SSH. The terminal adds no new escalation surface.

3. **`run_as` is honoured through the shared guard.** The interactive terminal
   takes no `run_as` from the client (the create payload is `server_id` only),
   so it launches as the service account. The argv is still built through
   `local_guard.wrap_local_argv(argv, run_as=None)`, so if a future caller ever
   supplies a `run_as` it flows through the same `sudo -n -u` drop and the same
   `run_as=="root"` refusal the job executor uses — there is one code path for
   "run a local command as a user," not two.

4. **Starts in the service account's home, not FDM's tree.** The shell's cwd is
   the service account home directory, never FDM's install root. This mirrors
   DOO-1199's AC8 intent (a local backend manages *other* benches, not the
   control panel itself); it is a default, not a jail — an interactive shell can
   `cd` anywhere the service account already can, precisely as an SSH shell can.

5. **No secrets, no host key.** A local server has no `SSHCredential`, so there
   is nothing to decrypt and no host key to pin. The ticket flow is unchanged:
   the single-use Redis ticket still gates the WS handshake, and the audit
   `TerminalSession` row still records who opened the shell and for how long.

## Residual risk (stated for the reviewer)

A local PTY is, by design, a shell on the FDM host as the FDM user. Anyone with
`terminal:access` (Developer+) on a registered local server can therefore read
and write anything that OS user can — including, if the OS permissions allow it,
FDM's own files. We do **not** sandbox that at the PTY layer: an interactive
shell that cannot `cd` or `cat` freely is not a usable terminal, and the job
executor's AC8 path guard exists precisely because *jobs* are the thing that
must be constrained, not an operator's explicit interactive session. The
mitigations we rely on are: (a) `terminal:access` RBAC, (b) never-root, (c) no
added escalation beyond `fdm-elevate`, and (d) the audit row. Deployments that
want stronger isolation should run FDM under a dedicated unprivileged service
account with no write access to the platform tree — which is the recommended
production posture regardless of this feature.
