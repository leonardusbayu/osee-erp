# Deployment and recovery

This package prepares a cloud host for OSEE. It has not been deployed to a public server: the server, DNS name, certificate issuance, host firewall, external backups, and company acceptance remain unconfirmed. It does not connect to BNI. BNI API approval is pending; the current authorized bank workflow is manually imported Excel/CSV e-statements.

## Components and trust boundary

`compose.yaml` runs PostgreSQL 17.11, a non-root Waitress application, and Caddy 2.11.4. Only Caddy publishes ports 80/443. PostgreSQL and the application are on an internal network. Caddy overwrites forwarded protocol/address headers; Waitress accepts only `X-Forwarded-For` and `X-Forwarded-Proto` from its exact configured peer address, with one trusted hop. Django does not trust raw forwarded headers. Adding a CDN, load balancer, or another proxy requires a new reviewed trust configuration; do not set the proxy to `*`.

WhiteNoise serves collected public static assets. Uploaded source documents use the separate persistent private volume and permission-checked download views. No public media route or proxy cache is configured. The application has a read-only root filesystem and bounded temporary storage. The default internal network blocks direct external AI/bank traffic; enabling an external integration requires deliberate controlled egress as well as the application's disclosure gates.

`/health/` reports process liveness. `/ready/` returns 200 only when the database is reachable, current migrations are applied, and private storage supports a temporary write/read/delete. Failures return generic 503 without credentials or addresses. Caddy does not expose either route publicly; its internal health probe and the container healthcheck use readiness. A healthy readiness result does not certify tax/business correctness or backup freshness.

## First deployment on a Linux cloud host

Use a dedicated host with Docker Engine and Compose v2. Review current image advisories and pin the accepted image digests in release records after pulling/building. The application dependencies are version-pinned; image tags remain mutable and are not a supply-chain attestation. Restrict SSH to approved administrators; open public ports 80/443 only. Do not publish application port 8000 or PostgreSQL 5432.

1. Choose the exact ERP hostname and cloud server with the owner. Configure its A/AAAA DNS records and reachable ports 80/443. Remove an incorrect AAAA record rather than leaving clients routed to an unconfigured host. The examples use `example.invalid`, which cannot obtain a public certificate.
2. Transfer a reviewed source release excluding `.env`, `.local`, development databases, real document files, and virtual environments. Preserve the Git commit identifier and source/image hashes alongside backups. A local private Git repository now exists; configure a protected remote or independent release backup before relying on recovery from a lost workstation/server.
3. Copy `deploy/.env.example` to `deploy/.env` and enter the actual hostname and operational email. Generate new secrets **on the deployment host** with `python deploy/init_secrets.py` after installing the pinned requirements in an administration environment. The script never overwrites existing secrets or prints values. Back up the Django secret and OTP encryption key separately in a protected vault. Losing/changing the OTP key prevents decryption of existing MFA secrets.
4. Keep `deploy/secrets` accessible only to its owner (0700 on Linux). Files are 0444 inside that protected directory so the separately unprivileged application and PostgreSQL container identities can read only the individual secrets mounted into their services. On Windows, protect the equivalent directory with an administrator-reviewed ACL. Never commit, send in chat, or include these files in the image.
5. From the repository root, run the following on the new host. These commands initialize a new deployment; they are **not** a migration of the existing company database.

```bash
dc() { docker compose --env-file deploy/.env -f deploy/compose.yaml "$@"; }
dc config --quiet
dc build --pull
dc up -d db
dc run --rm app python manage.py migrate --noinput
dc run --rm app python manage.py check --deploy --fail-level WARNING
dc up -d app proxy
dc ps
```

The production settings reject missing/weak secrets, absent PostgreSQL credentials, wildcard hosts, an invalid persistent OTP key, and private storage placed inside public static files. Secure cookies, HTTPS redirect, HSTS and MFA requirements are on. Demo/local company setup is off. Do not work around a startup failure by enabling `DJANGO_DEBUG` in the cloud. Initial company/account provisioning must use the reviewed operator workflow; development loopback setup is intentionally unavailable.

The current company database is SQLite. Native backups below do **not** convert SQLite into PostgreSQL. Use the separately guarded transfer procedure below before moving that company, and obtain Finance's agreement on balances. Do not start an empty cloud company and claim the existing records have been transferred.

## SQLite-to-PostgreSQL handover

`scripts/sqlite_to_postgres.py` implements an offline transfer to an explicitly selected **empty** PostgreSQL database and empty private-media directory. It takes a private native snapshot without modifying the original SQLite database, applies pending migrations to that private copy, exports every Django row without filtering inactive memberships, recreates the PostgreSQL schema, then imports exact IDs and values. It preserves sessions, audit history, password hashes, encrypted MFA state, automatic many-to-many IDs, and migration records. It resets PostgreSQL sequences and compares canonical per-table row hashes/counts for every mapped table and SHA-256 for every copied private file. An unmapped table, changed migration source, populated target, missing file, or mismatch prevents a successful result. The source file hash must remain unchanged.

Use a frozen reviewed source release and a maintenance window. Create the empty target database with an approved database administrator; **do not run `migrate` on it first**, because this tool refuses existing tables. Keep the destination app stopped until verification and UAT finish. Example from a trusted administration environment with network access to the new database:

```powershell
.\.venv\Scripts\python.exe scripts/sqlite_to_postgres.py `
  --source-sqlite 'D:/private-source/osee.sqlite3' --source-media 'D:/private-source/uploads' `
  --workspace 'D:/private-transfer/unique-transfer-20260909' --target-media 'D:/private-target/uploads' `
  --target-host '127.0.0.1' --target-port 5432 --target-database 'osee_new' --target-user 'approved_admin' `
  --target-password-file 'D:/protected-secrets/target-postgres-password' --quiesced
```

The example paths/host are placeholders, not a configured cloud destination. Prefer running on the target host so database credentials do not cross an unreviewed network. The private transfer workspace includes a complete snapshot, fixtures, logs, inventories and `verification.json`; it contains confidential data and must use restricted access and encrypted storage. Errors may leave an incomplete destination; the script does not delete or overwrite it automatically. Investigate and use a new empty target for retry. Preserve the original Django secret and OTP encryption key separately in the cloud secret vault before login: the script intentionally does not print or transport secrets. Preserved sessions only remain usable with the original signing key; operator-approved session revocation may be chosen after transfer, with a separate audit.

The verification result is evidence of transfer fidelity, not acceptance of the source's financial/tax correctness or a public cutover. Review all source exceptions and source/ledger distinctions after transfer. Compare Finance control totals and role/MFA/private-download behavior, retain a rollback copy, then approve DNS/traffic changes.

## Lost-password / lost-all-MFA recovery

The owner can manage eligible team passwords through the application. Losing an owner's password and all MFA recovery codes requires the separate offline `recover_account` management command. This is intentionally a server-operator privilege, not an unauthenticated web recovery link.

```bash
dc run --rm app python manage.py recover_account \
  --company-id 123 --username exact-account-name \
  --reason 'Identity verified under approved ticket ABC-123 by the named operator' --reset-mfa
```

Use a private interactive terminal. The command requires exact company/account selection, a typed confirmation, and two non-echoing password prompts; it rejects piped input and has no password argument. It does not reactivate disabled accounts or memberships. Resetting MFA is explicit; omit `--reset-mfa` for password-only recovery. It invalidates existing sessions and forces a password change, and production forces MFA enrollment again when cleared. The global identity can affect several company memberships, so it records an audit event in every affected company, with actor `None`, OS operator identity, reason and recovery flags; it never claims that an application owner authenticated the command. Passwords, OTP secrets and recovery codes are absent from output and audit detail.

Grant server access only to named administrators and retain SSH/sudo/operator approval logs. Inside a container the OS name can be the shared service user, so correlate the recorded ticket with the named external operator. Verify the person's identity through the approved independent process before confirming; possession of server access can bypass application authentication and therefore needs stronger operational control. Communicate temporary credentials only through the approved private channel. Do not run this command against a real account as a deployment smoke test.

## Backup procedure

The portable archive contains a native database snapshot, private document bytes, their relative paths/sizes/SHA-256 hashes, and a UTC manifest. SQLite uses the online backup API plus integrity/foreign-key checks. PostgreSQL uses `pg_dump` custom format. Creation always requires `--quiesced`, refuses an existing output path, rejects linked media paths, and verifies its archive before returning success.

The database and media are not one transactional storage engine. Put the site into a maintenance window and stop **all** application/background writers before using the flag, including operator import commands. This package currently has no worker service; add future workers to the stop procedure. Preserve the archive's output hash separately in trusted backup records. The archive itself is not encrypted and a hash is not an authenticity signature: use encrypted off-host/object storage with restricted access and retention/versioning, plus a separately protected key vault. A copy on the same disk is not disaster recovery.

Example for the deployed PostgreSQL stack (choose a unique filename):

```bash
sudo install -d -m 0700 -o 1000 -g 1000 deploy/backups
dc stop app
dc run --rm --no-deps -v "$PWD/deploy/backups:/backups" app \
  python scripts/backup.py create /backups/osee-YYYYMMDD-HHMM.zip \
  --postgres-database osee --media /app/private/uploads --quiesced
dc start app
dc ps
```

If creation fails, keep the last verified backup and investigate; a failed run is never a new restore point. Restart the original application after the attempt because creating a backup does not modify its business records. Upload the successful archive to the approved encrypted off-host destination and verify its returned/downloaded bytes before marking the off-host backup complete. Retention, alerting and scheduler ownership must be configured on the chosen server; no external destination is guessed or provisioned here.

For a local SQLite backup, stop its launcher first and use explicit source paths:

```powershell
.\.venv\Scripts\python.exe scripts/backup.py create 'D:/approved-backups/osee-YYYYMMDD-HHMM.zip' --sqlite '.local/osee.sqlite3' --media '.local/uploads' --quiesced
.\.venv\Scripts\python.exe scripts/backup.py verify 'D:/approved-backups/osee-YYYYMMDD-HHMM.zip'
```

## Restore drill and rollback

Restore only a trusted archive whose separately recorded hash matches. Database dumps execute schema/data commands; never restore an arbitrary uploaded archive. The restore tool validates all manifest entries and hashes before writing, rejects absolute/traversal/duplicate/link entries, and refuses to overwrite an existing SQLite database, nonempty PostgreSQL database, or nonempty media directory. PostgreSQL restore uses a single transaction and stops on errors. Any interrupted restore remains an incomplete target; use a fresh isolated target after investigation, and never direct the tool at the live database.

```powershell
.\.venv\Scripts\python.exe scripts/backup.py restore 'D:/approved-backups/osee-YYYYMMDD-HHMM.zip' --sqlite '.local/recovery-drill/database.sqlite3' --media '.local/recovery-drill/uploads'
```

For cloud recovery, use a new host or a separate Compose project and new volumes, restore the original protected secrets, and keep public traffic off. A parallel project on the same host needs a non-overlapping `OSEE_NETWORK_SUBNET` and matching `OSEE_TRUSTED_PROXY_IP`; its proxy cannot bind the live host's 80/443. Start only its database, mount the archive read-only, and run `scripts/backup.py restore ... --postgres-database osee --media /app/private/uploads` from a one-off application container. Restore before running migrations: the target must be empty. Then apply reviewed migrations, run checks/readiness, compare table/control totals and every document hash, and perform role/MFA/private-download/finance UAT before DNS/traffic cutover. Native PostgreSQL archives require a compatible PostgreSQL client/server; they do not restore into SQLite.

Record restore start/end, latest captured business timestamp, missing interval (RPO), elapsed recovery time (RTO), schema/release version, counts, document hashes, and reviewer. Drill regularly on a separate machine or volume. Do not automatically send bank payments or tax filings after recovery; reconcile authoritative external effects first when those connectors are introduced. Rollback means restoring a compatible code+database+media+secret set; rolling back code alone after a schema change is not a safe plan.

## Verification completed here

- Final complete application suite: **346 tests passed** on the isolated PostgreSQL runtime (82.652 seconds). Critical Ruff correctness checks and Python compilation passed across application, deployment and scripts. The default test run is separate from the opt-in backup/transfer rehearsals below.
- SQLite: 17 evidence/deployment tests and 5 backup tests passed.
- Offline account recovery: 4 synthetic tests passed, covering MFA reset, password-only reset, session invalidation, multi-company audit, and rejected noninteractive/disabled-account recovery.
- Isolated PostgreSQL 17.4 installed on this workstation: 18 evidence/deployment tests passed, including a concurrent 24-request login-quota test. A real `pg_dump`/`pg_restore` round trip preserved a decimal value and document hash and refused a nonempty destination. Only new synthetic databases were used; 17.4 is not the approved cloud version.
- Docker Compose configuration validated without a daemon. Official Caddy 2.11.4 Windows executable matched its published SHA-512 checksum and validated the Caddyfile without starting a public service.
- An all-table synthetic SQLite-to-empty-PostgreSQL transfer rehearsal passed, including inactive membership, sessions, MFA state, precise timestamps, M2M ID gaps, Unicode/JSON/decimal values and private PDF bytes. The original SQLite file was unchanged and the populated target refused a second transfer. Verification manifests are private test artifacts under `.local/fix-audit-2026-09-09/postgres/transfer-verification/`.
- Cloud container build/runtime, PostgreSQL 17.11 image, public TLS issuance, provider firewall, off-host recovery and live company migration remain release checks. Starting Docker Desktop was blocked by automatic approval review; no bypass was used.
- `.github/workflows/quality.yml` defines isolated PostgreSQL tests, migration drift checks, Python compilation, critical Ruff checks, dependency consistency, production Django checks, container build, PostgreSQL backup/recovery and SQLite transfer rehearsal. It has not run on a hosted CI service; the repository currently has no configured remote.

## Primary references checked 9 September 2026

- [Waitress proxy trust arguments](https://docs.pylonsproject.org/projects/waitress/en/latest/arguments.html) and [reverse-proxy behavior](https://docs.pylonsproject.org/projects/waitress/en/stable/reverse-proxy.html).
- [Caddy forwarded header defaults](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy) and [official 2.11.4 release](https://github.com/caddyserver/caddy/releases/tag/v2.11.4).
- [WhiteNoise Django setup](https://whitenoise.readthedocs.io/en/stable/django.html).
- [PostgreSQL current release notes](https://www.postgresql.org/docs/release/) and [pg_dump](https://www.postgresql.org/docs/17/app-pgdump.html).
- [pypdf strict parsing](https://pypdf.readthedocs.io/en/stable/user/robustness.html). Uploads now validate structure, reject encrypted/active/embedded PDFs, and fully decode bounded PNG/JPEG images. This is not a malware-clean certificate.
- New direct dependency versions were verified through the official PyPI JSON API. OSV returned no known matching advisory for pypdf 6.18.0, WhiteNoise 6.12.0, pyotp 2.10.0, cryptography 50.0.1, qrcode 8.2, openpyxl 3.1.5 and xlrd 2.0.2 on this date; this is not a vulnerability-free guarantee or an OS/container scan.
- The complete 23-version runtime requirements list was also queried against OSV with zero matching advisories on this date. Exact result metadata is retained privately under `.local/fix-audit-2026-09-09/dependencies-osv.json`.
