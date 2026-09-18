# OSEE ERP — local finance and Director workspace

A working local Django application for PT Langkah Pintar Nusantara, with Indonesian screens for finance, tax preparation, and Director planning. The Director module connects source-data review, growth monitoring, budgets, cash scenarios, marketing observations, and recorded business decisions. Bank connectivity, official tax filing, and several accounting workflows still need implementation and validation.

Start with the [developer handoff](docs/DEVELOPER-HANDOFF.md) for a fresh clone, setup commands, the code map and remaining work. This private repository contains source and internal design documents; company databases, original financial documents and credentials are not included.

The broader design is in [the finance ERP architecture](docs/FINANCE-ERP-ARCHITECTURE.md). Read the [9 September remediation results](docs/REMEDIATION-2026-09-09.md) and [Finance operating guide](docs/FINANCE-OPERATING-GUIDE.md) for the updated behavior. [Implementation status](docs/IMPLEMENTATION-STATUS.md) also retains earlier implementation history.

The [Director module architecture](docs/DIRECTOR-MODULE-ARCHITECTURE.md) describes the broader target. This checkout implements an initial local module at **/director/**; live marketing connectors, verified cash availability, and automated execution are outside its current scope.

## Start on Windows

1. Double-click **Start OSEE.cmd** in this folder. The first run needs internet access to install the pinned Python dependencies. Python 3.12 or newer is needed; the launcher also checks for the bundled Codex Python runtime.
2. Open **http://127.0.0.1:8000** in your browser. The server runs in the background; the launcher window can be closed. Double-click **Stop OSEE.cmd** to stop it. After restarting Windows, run **Start OSEE.cmd** again.
3. If company data has already been imported, choose **Aktifkan akun pemilik** and create your personal username/password. This opens the prepared company without copying demo records. On a fresh installation without imported company data, choose **Buka workspace demo** or **Siapkan workspace perusahaan**.

The demo account uses the local demo button, not a shared password. Demo access and initial setup are restricted to local requests and development mode. Setup creates one company workspace with initial product records and no financial transactions; its prices must be configured. It does not copy demo transactions or treat demo tax decisions as company policy. Subsequent use of that workspace requires the account you created.

The launcher creates `.venv`, applies migrations, and runs Django's development server on `127.0.0.1:8000` as a hidden background process. Readiness is announced after the server responds. Repeated starts reuse the verified running OSEE process; unrelated processes occupying port 8000 are left alone. Demo seeding is skipped once real source data exists. This does not deploy the application or connect to BNI or DJP. Local server logs are in `.local/logs/`; process identity is in `.local/server-state.json`. Stop and start OSEE after code changes because this launcher disables automatic reload. A manual foreground `manage.py runserver` session still ends when its terminal closes.

On the original company installation, the **Data 2026** screen contains the supplied recap's reported monthly totals, source-column prices, dated quantity cells, original PDFs, and review exceptions. It preserves date conflicts, ambiguous merged cells, and missing values. The supplier invoice is a draft bill. Recap entries are not fabricated customer invoices, bank transactions, or tax calculations. Until books are posted, the dashboard opens this imported-data view. The earlier demo is retained in an isolated organization and its account is disabled; it is not part of the company books.

Local records are stored in `.local/osee.sqlite3`; the generated development secret is in `.local/django-secret`. Source-file storage is configured under `.local/uploads`. These files are private local state, excluded from Git. Do not delete `.local` to reset a demo if it also contains company data.

## Try the finance flow

1. Add a reseller/customer, supplier, product, and applicable price. Price versions are effective-dated; an invoice keeps the price it was created with.
2. Create and issue a sales invoice. For the wholesale workflow, invoice issuance records a receivable and deferred revenue. Match the reseller's payment before recording test delivery; delivery recognizes revenue.
3. Record supplier bills using their actual gross amounts and service dates. Supplier VAT is part of the gross bill, not an extra amount added again. Attach PDF/image source evidence to the relevant invoice or bill; tax treatment remains a separate review.
4. Add a bank account and upload an Excel/CSV e-statement. Map columns, inspect the preview and totals, then explicitly confirm the import. Match receipts to invoices, supplier payments, bank fees, transfers or customer advances. Repeated imports are checked for duplicates and conflicting references.
5. Open the commercial reports and resolve reconciliation/close blockers. Download printable invoice or monthly-report PDFs when needed. Monthly closing locks supported accounting activity for that period.
6. Open **Pajak bulanan** to see bill review candidates and draft workpapers, or **SPT Tahunan** to see commercial ledger totals, closed months, and preparation needs. Exported tax CSV files are explicitly drafts.

The bank import supports bounded Excel/CSV files with explicit column mapping; the actual BNI export still requires acceptance against a real statement. It is not a claim that every BNI statement format is supported. Demo prices and the demo's zero-withholding examples are synthetic, not approved OSEE tax decisions.

## Use the Director module

Open **Modul → Direktur**. The pages provide these working flows:

1. **Overview, growth, and data readiness:** inspect monthly source totals, raw partner labels, source exceptions, and available posted-book figures. Source recaps remain separate from recognized revenue and cash. Missing values remain unknown; partial months and incomplete attribution limit comparisons.
2. **Objectives and decisions:** record targets and progress; draft, submit, review, approve, activate, and complete a proposal. Submitted proposals are frozen. Decisions retain responsible people, dates, evidence, risks, versions, and review notes. Approval records business direction; execution notes are entered by people.
3. **Budgets:** submit a monthly channel budget for approval, then record incurred costs, open commitments, reservations, and payment observations. The first three consume the budget; payments do not consume it again. Replace a commitment when it becomes a cost so both are not counted. Corrections preserve the old record.
4. **Cash plans:** enter an assumed opening balance, reserve, outstanding obligations, and dated inflows/outflows. Review thirteen weeks and the next fourteen days. These are planning assumptions; the result is not a verified bank balance or free cash available to spend.
5. **Marketing:** enter observations or import the provided CSV for mitra, Meta Ads, Google Ads, WhatsApp, SEO, and sales. UTF-8 files are limited to 1 MB and 500 rows. Identical repeats are skipped; a conflicting observation rejects the whole import and requires an explicit correction. Blank values stay unknown. There are no live platform connectors or automatic attribution, CAC, or ROAS calculations.
6. **Scenarios and reports:** save a price/cost/volume scenario and its calculated contribution comparison, then export the Director's management snapshot as CSV or PDF. Simulations do not update selling prices or supplier costs. Exports are internal management reports.

The team has three roles: **Owner/Direktur**, **Marketing**, and **Finance**. Create individual accounts through **Pengaturan → Tambah anggota**; only Owner/Direktur can create them. The form defaults to Finance and rejects other role values. Owner/Direktur starts in Director, Marketing in Marketing, and Finance in Finance, unless following a safe explicit login destination. Modules and Settings explain each role's responsibilities and limits. Operasional remains a planned module; its team access will be added with that module.

**SDM and payroll are planned Finance responsibilities**, with payroll approval proposed for Owner/Direktur. Employee and salary data are intended to be restricted to Finance and Owner/Direktur, excluding Marketing. No separate HR login role is planned. The module, payroll processing, and its specific access controls are not implemented yet.

Owner/Direktur can approve Director decisions and budgets and manage company/accounts. Finance can maintain financial records and prepare/review planning and marketing records, but cannot approve Director budgets/decisions, manage accounts/company settings, or close periods. Marketing can manage company marketing data but cannot access Finance, private Director reports, or Director chat. Self-approval still requires an explicit owner exception. Stored legacy `director`, `reviewer`, and `auditor` memberships retain their existing permissions and display as legacy access; they are not new-account choices and are not automatically converted. Owner/Direktur can record a professional tax review bound to its signed evidence and decision. Neither a team role nor an AI response establishes tax competence; Finance prepares records and a documented professional review is required for the approval workflow.

No Director form, CSV import, scenario, or chat action posts financial journals, moves money, activates advertising, changes accepted invoice prices, or files taxes. Budget approval and recorded decision completion do not certify that an external action happened.

## Director adviser and optional AI

**AI Direktur** is private to its conversation creator and company, available to owner/director/finance accounts. By default it returns a labelled **Ringkasan dari data aplikasi**, calculated locally from scoped source facts, available books, approved budgets, reported marketing spend, and saved scenarios. This is a deterministic briefing, not an external model answer.

The optional OpenRouter path uses dedicated configuration, separate from tax chat. It can select and rank findings already produced by the application; it cannot invent new amounts or execute actions. It sends only locally classified intent, permitted aggregate facts, and application-defined finding codes. Original questions, conversation history, company/partner names, bank details, participant identities, uploaded documents, and arbitrary notes stay local. Aggregates are still confidential.

External Director calls are off by default. An active owner must approve the disclosure and positive company/request limits in **AI Direktur → Pengaturan AI**, and deployment configuration must also enable every required gate:

| Setting | Purpose |
| --- | --- |
| `DIRECTOR_OPENROUTER_API_KEY` | Dedicated backend credential for the Director adapter. |
| `DIRECTOR_OPENROUTER_MODEL`, `DIRECTOR_OPENROUTER_PROVIDER` | Explicitly reviewed model and provider endpoint identifiers. |
| `DIRECTOR_AI_PRIVATE_AGGREGATES_ENABLED` | Permit the bounded confidential aggregate contract; default `0`. |
| `DIRECTOR_AI_ENDPOINT_APPROVED` | Record deployment approval of the selected endpoint; default `0`. |
| `DIRECTOR_AI_MAX_REQUEST_COST_USD` | Reviewed maximum cost reserved before each request; default `0`. |
| `DIRECTOR_AI_MONTHLY_BUDGET_USD` | Shared monthly admission limit across this application's organizations; default `0`. |
| `DIRECTOR_AI_USER_MONTHLY_BUDGET_USD` | Per-user monthly admission limit; default `0` in the example. |

The owner policy adds a company budget and per-request cap. The adapter checks current membership and policy, reserves cost before dispatch, validates returned finding/fact IDs, and falls back to the local briefing when configuration or a response is unsuitable. It retains conservative reservations after calls, including uncertain outcomes, and does not automatically retry an uncertain request. Real provider privacy, capability, pricing, and billing controls still need authorized acceptance; local admission limits are not a provider billing guarantee.

## Tax and chat boundaries

OSEE is confirmed as an ordinary PT, currently non-PKP, with reported annual turnover below Rp4.8 billion. That does **not** automatically activate a 0.5% final tax rate. Registration history, effective dates, supporting documents, and a designated tax reviewer's decision remain necessary. Current non-PKP status must not be backdated to earlier invoices without evidence.

PPh 23 withheld from suppliers, potential credits withheld by customers, and the company's own turnover tax are separate records. Workpaper approval, verified payment evidence, and verified filing evidence are also separate. There is no Coretax submission, official DJP XML generation, or automatic tax payment in this release.

**Tanya Pajak** stores private conversations per user and company. Without API configuration it gives sourced **Panduan tersimpan**, clearly labelled as stored guidance. Optional OpenRouter calls explain only curated public process/document topics. Original questions, conversation history, uploaded evidence, and company financial data are not sent externally in this release—even if the private-context flag is changed. Tax rates, eligibility, amounts, deadlines, and payment/filing status are not delegated to the language model.

To configure the optional adapter, copy `.env.example` to an untracked `.env` and set these deployment-reviewed values:

| Setting | Purpose |
| --- | --- |
| `OPENROUTER_API_KEY` | Dedicated backend inference credential; never enter it in chat. |
| `OPENROUTER_MODEL`, `OPENROUTER_PROVIDER` | The single approved model and provider endpoint. |
| `OPENROUTER_MONTHLY_BUDGET_USD` | Local monthly admission budget. |
| `OPENROUTER_MAX_REQUEST_COST_USD` | Reviewed maximum charge reserved per request; default `0` keeps external calls off. |
| `OPENROUTER_PRIVATE_CONTEXT_ENABLED` | Leave `0`; private-context inference is not implemented. |

The adapter restricts routing, denies provider data collection, requests ZDR, disables fallback routing, caps input/output and request counts, and validates structured responses and citation IDs. Invalid or unavailable responses fall back to stored guidance. Reservations are retained after failed requests. The configured cost ceiling and provider-side key budget need real pricing/endpoint validation; these controls do not establish a billing guarantee or tax correctness. Source guidance expires from active chat use after its review window and needs renewed review.

## Developer commands

Run from this repository in PowerShell after the launcher has prepared `.venv`:

```powershell
.\.venv\Scripts\python.exe manage.py check
.\.venv\Scripts\python.exe manage.py test
.\.venv\Scripts\python.exe manage.py test finance taxes webapp
.\.venv\Scripts\python.exe manage.py test director
```

For a manual local start:

```powershell
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py seed_demo
.\.venv\Scripts\python.exe manage.py runserver 127.0.0.1:8000
```

`seed_demo` is development-only and does not overwrite an existing demo. The startup script installs `requirements.txt` again when its fingerprint changes. Values from the process environment take precedence over `.env`.

## Extend and deploy deliberately

`core/` owns organization access, roles, audit records, numbering, and durable domain events. `finance/` owns validated accounting services; `taxes/` owns tax preparation and chat; `imports/` owns immutable source archives and reconciliation observations. `director/` owns scoped planning workflows, deterministic calculators and snapshots, marketing CSV intake, management reports, and the separately gated adviser. `webapp/`, `templates/`, and `static/` provide the interface. Future test operations, learning, partner portal, and payroll modules are placeholders in the module registry.

Extend through organization-scoped services, with financial mutations and their `DomainEvent` written atomically. Consumers must enforce organization scope and idempotency. The event table is an outbox foundation: **no event dispatcher, consumer worker, or retry delivery system is implemented yet**. Do not bypass posting services with direct model/bulk writes.

The Windows launcher is for local development. PostgreSQL concurrency, backup/restore, account lifecycle and MFA were tested during remediation. Cloud configuration now includes Caddy HTTPS, a trusted proxy boundary, private storage and readiness checks; see [deployment and recovery](deploy/README.md). A real cloud host/domain, container runtime, off-host recovery and company cutover still require acceptance. BNI, official DJP outputs/submission and production OpenRouter behavior remain separate integration gates.

## Marketing workspace

Open **Marketing** from the module switcher or visit **http://127.0.0.1:8000/marketing/**. The implemented [Marketing architecture](docs/MARKETING-MULTIDIMENSION-ARCHITECTURE.md) connects campaign/ad observations, lead handling, team responsibility, and Finance-allocated customer receipts.

1. Add team profiles and campaigns in **Iklan & Tim**.
2. Record daily ad reports or download/import the per-campaign CSV template. Empty amounts stay unknown; repeated identical references do not duplicate costs.
3. Record prospects, their source, response time, and next follow-up in **Prospek**.
4. An owner/director/finance user links the correct issued invoice. Actual cash comes from Finance bank allocations, including partial payments.
5. Choose **Analisis** to compare channel, campaign, responsible member, product, segment, region, or ad. Unsupported breakdowns show no fabricated cash/cost ratios.
6. Use **Saran Manager** to review evidence, assign an action with an owner/deadline, and record its outcome.

Cash is **gross allocated customer receipts**, not net cash, bank balance, profit, or causal ad return. Refunds and multi-touch attribution are not implemented. Marketing accounts share the marketing workspace; team profiles are not individual account permissions.

Local recommendations work immediately and are labelled **Saran dari aturan aplikasi**. Optional real AI ranking uses the configured Director OpenRouter endpoint and shared global/user budget controls, but requires the separate `MARKETING_AI_PRIVATE_AGGREGATES_ENABLED=1` deployment flag and an owner's **Pengaturan AI marketing** disclosure approval. All defaults remain off; no live provider request was made during implementation. Free text, names, raw invoices/bank details, and original questions remain local. Platform APIs and WhatsApp sending are not connected.

Run `.\.venv\Scripts\python.exe manage.py test marketing` for the portable Marketing regression suite. The historical browser smoke at `tests/marketing-browser-smoke.cjs` expects a synthetic database on port 8002 and local fixtures under `tmp/` that are not included in this repository. It is not a fresh-clone acceptance command; recreate isolated fixtures before using it and never target company data.
