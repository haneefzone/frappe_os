# Master-key escrow runbook

**Audience: the platform operator / custodians. You do not need to be a developer to follow this.**
**Classification: operational security. Store this document with your other critical operations records.**

This runbook explains the two secrets that hold the FDM platform together, how to
keep safe copies of them (*escrow*), how to prove those copies actually work, and
exactly what to do the day a server is lost. Read it once in full before you file
your escrow copies. Then use the [quarterly checklist](#6-quarterly-escrow-verification-checklist)
every three months.

---

## 1. The two secrets, in plain language

The platform stores a lot of other people's secrets on your behalf: the SSH keys
that log into every managed server, the S3/object-storage credentials that hold
your offsite backups, API tokens, and more. It never stores them in the clear —
every one of them is **encrypted** before it is written to the database.

Two operator-held secrets sit above all of that:

| Secret | Env var | What it does | What it is NOT |
|---|---|---|---|
| **Master key** | `FDM_SECRET_KEY` | The single key that encrypts/decrypts every stored SSH key, storage credential and API token. | Not recoverable by anyone — not Duncan & Ross, not any vendor, not Anthropic. |
| **Backup passphrase** | `FDM_BACKUP_PASSPHRASE` | A **separate** key that encrypts the platform's *self-backup* archive at rest. | Not the master key. Kept and escrowed separately, on purpose. |

### 1.1 What dies if you lose the master key

If `FDM_SECRET_KEY` is lost or changed and you have no escrowed copy:

- **Every stored SSH credential becomes permanently unreadable.** The platform can
  no longer log into any managed server. You would have to re-add every server's
  key by hand.
- **Every stored storage credential becomes unreadable** — including the ones that
  reach your *offsite backups*. The backups still exist in your bucket, but the
  platform can no longer fetch them automatically.
- **Every stored API token becomes unreadable.**

The encrypted data is not corrupted — it is simply locked, and the only key that
opens it is gone. **There is no recovery path. No support ticket fixes this.** This
is by design: the point of encryption is that nobody without the key can read the
secrets, and that includes you.

### 1.2 Why a *separate* backup passphrase exists (the loud dependency)

The platform can back *itself* up (database + configuration) — see the
**Settings → Platform self-backup** panel. That self-backup archive is encrypted at
rest, because it contains the platform's whole database.

Here is the trap we deliberately avoid: if the self-backup were encrypted with the
master key, then the day you lost the master key you would need the self-backup to
recover it — but you could not open the self-backup without the master key you
just lost. A backup you can only open with the thing it is meant to recover is
worthless in the exact emergency it exists for.

So the self-backup archive is encrypted with a **separate** secret,
`FDM_BACKUP_PASSPHRASE`, that you hold and escrow independently. On restore you
supply the backup passphrase to open the archive; that gets the database back; and
then you supply the escrowed **master key** so the platform can read the SSH and
storage credentials inside it. **Two secrets, escrowed separately, both required.**

> The self-backup archive **never contains** either secret. The `.env` file inside
> it has both `FDM_SECRET_KEY` and `FDM_BACKUP_PASSPHRASE` stripped out (replaced by
> a breadcrumb comment noting they were excluded and must be restored from escrow).
> Neither secret is ever written to the database, a log line, a job parameter, an
> exported artifact, or an error message.

---

## 2. Escrow procedure — how to keep safe copies

**Goal:** at least two independent, offline copies of *each* secret, held by two
different people, in two different physical locations, so that no single lost
laptop, disk failure, or departing employee can take a secret out of reach.

### 2.1 Print / write down the current values

On the platform host, the two secrets live in the platform's `.env` file (typically
`/opt/fdm/.env` or the repo `.env`). Read them:

```
grep -E '^(FDM_SECRET_KEY|FDM_BACKUP_PASSPHRASE)=' /opt/fdm/.env
```

Copy each value **exactly**, including any trailing characters. The master key is a
Fernet key (44 characters ending in `=`). Do not paste these into email, chat,
a ticket, or any cloud note.

### 2.2 Seal and store — the two-custodian rule

Prepare **two sealed envelopes** for each secret (four envelopes total, or one
sealed envelope per custodian containing both secrets — your choice, as long as no
single location holds the only copy):

- **Custodian A** — e.g. the platform owner / CTO. Stores their sealed copy in
  location 1 (e.g. an on-site fire safe).
- **Custodian B** — e.g. a second officer / operations lead in a different reporting
  line. Stores their sealed copy in location 2 (e.g. an off-site safe-deposit box
  or a corporate password vault they alone control).

Record who the two custodians are and where each copy lives:

| | Custodian (name + role) | Location | Date sealed | Date last verified |
|---|---|---|---|---|
| Copy 1 | _______________________ | ________ | __________ | __________ |
| Copy 2 | _______________________ | ________ | __________ | __________ |

Keep the master key and the backup passphrase copies **physically separated** where
practical, so a single compromised safe does not surrender both.

### 2.3 Acknowledge escrow in the platform

Once the sealed copies are filed, an Admin opens **Settings → Platform self-backup**
and ticks **"Escrow confirmed"** on the master-key warning banner. This:

- clears the persistent, non-dismissable warning banner, and
- writes an **audit-log entry** recording *who* confirmed and *when*.

The banner is intentionally impossible to dismiss any other way — it is a standing
reminder that the platform is only as safe as your escrow.

---

## 3. Key rotation and its blast radius

Rotate a secret when a custodian leaves, when you suspect exposure, or on your own
schedule (many operators rotate annually).

### 3.1 Rotating the backup passphrase (`FDM_BACKUP_PASSPHRASE`) — low blast radius

The backup passphrase only encrypts *future* self-backup archives.

1. Choose a new strong passphrase.
2. Update `FDM_BACKUP_PASSPHRASE` in `.env` and restart the platform services.
3. Run a fresh **Platform self-backup** and let its **verify** job pass.
4. Re-escrow the new passphrase (Section 2) and note the rotation date.

> Older self-backup archives are still encrypted with the *old* passphrase. Keep the
> previous passphrase in escrow until every archive that used it has aged out of
> retention. Only then destroy the old escrow copies.

### 3.2 Rotating the master key (`FDM_SECRET_KEY`) — high blast radius

The master key encrypts **every Fernet-encrypted column in the database** (SSH
keys, storage credentials, API tokens, AI provider keys, …). Changing it means
**re-encrypting every one of those columns**: read each value with the *old* key,
write it back with the *new* key. If you simply change the key without
re-encrypting, every stored secret becomes unreadable — the same outcome as losing
it.

This is a **maintenance-window operation**, not a live toggle:

1. Announce a maintenance window; stop the workers so nothing writes new secrets
   mid-rotation.
2. Take a fresh, **verified** platform self-backup first (your rollback point).
3. Run the re-encryption routine: it walks every Fernet-encrypted column, decrypts
   with the current `FDM_SECRET_KEY`, and re-encrypts with the new one, inside one
   transaction. *(If your build does not ship this as a management command, treat
   this as a scripted engineering task run against the maintenance-window database —
   it is a straightforward decrypt-with-old / encrypt-with-new pass over the known
   set of encrypted columns, which are enumerated in the models under
   `app/models/` that use `SecretsService`.)*
4. Swap `FDM_SECRET_KEY` to the new value in `.env`; restart services.
5. Smoke-test: open a server's detail page and run **Test Connection** — a green SSH
   check proves an SSH credential decrypted under the new key.
6. Re-escrow the new master key (Section 2). Keep the old key in escrow until you
   have a verified self-backup taken *after* the rotation, then destroy old copies.

---

## 4. Restore drill — the only real proof

Escrow you have never tested is a guess. Perform this drill at least once after
first setup, after any master-key rotation, and once a year thereafter. It proves
the whole chain: a fresh machine, plus your two escrowed secrets, brings the
platform back and can read a real SSH credential again.

> A machine-checked dry run of the cryptographic core of this drill
> (pg_dump → archive → encrypt → upload → download → checksum → decrypt →
> `pg_restore --list`) runs on every build in `tests/test_platform_backup.py`. The
> drill below is the *operator* version end-to-end on real hosts; the latest
> executed run is recorded in [Appendix A](#appendix-a-latest-restore-drill-record).

### Steps

1. **Fresh host.** Provision a clean server (or VM). Do **not** copy the old `.env`.
2. **Install** the FDM platform on it (the standard one-command installer), but do
   not start using it yet.
3. **Fetch the latest verified self-backup archive.** From your Admin session on a
   surviving host, **Settings → Platform self-backup → Download** the newest archive
   whose verify status is *verified*; or pull the object from your storage bucket
   directly. It is the `…​.tar.gz.enc` encrypted file.
4. **Supply the escrowed backup passphrase** and decrypt + unpack the archive:
   - Set `FDM_BACKUP_PASSPHRASE` on the fresh host to the escrowed value.
   - Decrypt using the archive's stored `kdf_salt` (shown on the backup row / in the
     backup metadata) — the platform derives the archive key from *passphrase +
     salt*. This yields `platform-db.dump` plus the `config/` set.
5. **Restore the database:** `pg_restore` the `platform-db.dump` into a fresh
   Postgres, and put the restored `.env`/`deploy/` config in place (remember: the
   `.env` in the archive has both secrets stripped — you re-supply them from escrow).
6. **Supply the escrowed master key.** Set `FDM_SECRET_KEY` on the fresh host to the
   escrowed value and start the platform.
7. **Confirm a real SSH credential decrypts.** Log in as Admin, open any managed
   server, and run **Test Connection**. A green **SSH ✓** means a stored SSH key was
   decrypted with the escrowed master key — **the drill has succeeded.** Record the
   result in Appendix A and stamp `restore_tested_at` on the backup you used.

If **Test Connection** fails to decrypt, your escrowed master key does not match the
one that encrypted the database. **Stop and resolve the escrow discrepancy now** —
you have found it during a drill instead of during a real disaster.

---

## 5. Locked-out admin — TOTP recovery

If the sole admin is locked out because they lost their TOTP device and have no backup codes,
run the following **on the platform host** (requires shell access and the platform's virtualenv):

```bash
python -m app.manage disable-2fa --email <admin-email>
```

This clears the confirmed-TOTP flag for that account so the next login skips the TOTP prompt.
The admin must then immediately re-enrol their TOTP device via **Settings → Security**.

> **Access required:** a shell on the platform host and the ability to activate the virtualenv
> (typically `source /opt/fdm/.venv/bin/activate` or equivalent). This is an emergency-operator
> operation — it is **not** available through the web UI.
>
> **Audit trail:** the command writes an AuditLog entry (`action=auth.mfa_disable`).

---

## 6. Quarterly escrow-verification checklist

Run every quarter. Tick each box; file the completed checklist with your ISO 27001
operations-security evidence (A.8 — asset/operations security; key-management
controls). Losing a quarter's check is itself a finding.

- [ ] **Both secrets are still escrowed in two locations.** Physically confirm both
      sealed copies of `FDM_SECRET_KEY` and of `FDM_BACKUP_PASSPHRASE` exist and are
      intact. (Section 2.2 table updated with today's date.)
- [ ] **Custodians are current.** Both named custodians still hold their role and
      still have access to their copy. If a custodian has left, **rotate** the
      affected secret (Section 3) and re-escrow — do not simply reassign a sealed
      envelope a departed person may have opened.
- [ ] **A recent self-backup exists and is *verified*.** In Settings → Platform
      self-backup, the newest backup ran within your RPO and its verify job passed.
- [ ] **The escrow acknowledgement is in place and audited.** The Settings banner is
      cleared and the audit log shows the confirming admin + timestamp.
- [ ] **A restore drill has been run within the last 12 months** (Section 4,
      Appendix A). If not, schedule one this quarter.
- [ ] **No secret has leaked into the clear.** Spot-check that `.env` files are
      `chmod 600`, that neither secret appears in any exported backup artifact
      (`grep` the decrypted archive), and that neither appears in job logs.

Signed off by: __________________________  Date: ______________

---

## Appendix A. Latest restore-drill record

> Paste the output of the most recent restore drill here (Section 4), or the
> machine-checked cryptographic dry run recorded during the 6.3 build. Include the
> backup id, its `kdf_salt`, the checksum-match line, the `pg_restore --list` entry
> count, and the SSH-decrypt confirmation.

**Full live drill — 2026-09-11, DOO-257.** Real `pg_dump`/`pg_restore` (matching
client/server major version), the real `platform.self_backup` /
`platform.self_backup_verify` jobs run through `JobRunner` end to end, and the
real local MinIO target (`fdm-backups` bucket on `127.0.0.1:9101`) named in the
acceptance criteria — no fakes/stubs anywhere in this run. The uploaded object
was independently re-fetched straight from MinIO (outside the job) before the
verify job ran, and every job log line was scanned for both secret values.

```
== 1. Running REAL platform.self_backup job (real pg_dump + real MinIO) ==
  job status         : success
  backup status       : success
  encrypted sha256    : a8d4e76593df25d0b3f3ec6f585ffce1e153962f60231f6d0c90ff9a58bd9ca2
  plaintext sha256    : 3bbd5064e098a27602a9b0255b25973a098f0f0b33ba34ce6f7c958176d77231
  kdf_salt            : 25ef799cabb95874ff3ab041a5f07c1b
  object_key          : platform-backup-1/platform-backup-1.tar.gz.enc

== 2. Independently fetching the uploaded object straight from MinIO ==
  downloaded bytes    : 2212
  FDM_SECRET_KEY value in encrypted object? False
  FDM_BACKUP_PASSPHRASE value in encrypted object? False

== 3. Running REAL platform.self_backup_verify job (download+checksum+pg_restore --list) ==
  verify job status   : success
  verify_status       : verified
  verified_at         : 2026-09-11 03:09:37.023626

== 4. RESTORE on a fresh scratch DB using the ESCROWED passphrase ==
  archive decrypted with escrowed passphrase: OK (3170 byte dump recovered)
  restored .env has FDM_SECRET_KEY stripped : True
  real pg_restore into fresh DB exit code    : 0

== 5. PROOF: the SSH credential row survived pg_dump -> pg_restore and decrypts with the ESCROWED key ==
  decrypt restored row with escrowed FDM_SECRET_KEY: SUCCESS
  recovered plaintext head: -----BEGIN OPENSSH PRIVATE KEY-----
  wrong-key decrypt: correctly REFUSED (SecretKeyError)

== DRILL RESULT: PASS (real pg_dump, real MinIO upload/download, real pg_restore, real decrypt) ==
```

Also grepped: the job's `LogEntry` rows (both jobs) and the raw bytes of the
uploaded MinIO object never contain the `FDM_SECRET_KEY` or
`FDM_BACKUP_PASSPHRASE` values — asserted in-script, not just eyeballed.

> Prior record (2026-07-24, cryptographic dry run with `pg_dump`/`pg_restore`
> stubbed — client binaries were absent in that build workspace) is superseded by
> the full live run above. A real production host still needs a **Test
> Connection** green tick and provisioned MinIO egress before this becomes the
> operator's actual restore procedure; see Section 4 for the manual steps.

