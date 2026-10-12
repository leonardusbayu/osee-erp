# Monthly tax source package — capability plan

**Baseline investigation, with implementation addendum below.** The original text records a read-only source investigation on 9 October 2026; it is preserved as the proposed design and its original evidence boundary. The supported first implementation subsequently passed combined acceptance and was applied locally; see [monthly release](MONTHLY-TAX-RELEASE-2026-10-09.md). Neither the investigation nor acceptance establishes a professional tax opinion or approved company tax classification. No company data, bank credentials, provider access, product edits, tests, or PostgreSQL runs were used for the original investigation.

## Purpose and company scope

Finance should select a month, obtain an automatically reconciled source package, resolve exceptions, and generate a supported tax draft from professionally reviewed figures without typing the same amount again. The package must preserve the distinction between invoice issuance, earned commercial income, cash collection, and fiscal turnover.

The user identifies OSEE as PT Langkah Pintar Nusantara, an ordinary PT rather than a PT perorangan, non-PKP, with turnover below Rp4.8 billion. Products include official TOEFL ITP, TOEFL iBT, and English courses. Mitra pay per order before testing. These statements define the intended scope; they do not establish registration date, legacy final-tax entitlement, revenue classification, or transaction completeness. Historical illustrative prices and imported recap observations are not authoritative current pricing or taxable-turnover evidence.

## Existing executable seams and present gap

The following contracts were inspected on 9 October 2026. Later changes require rechecking their implementation before building this plan.

| Current file/service | Current behavior and reuse |
| --- | --- |
| `taxes/services.py::monthly_tax_context` | Lists manually prepared obligations and supplier tax-review candidates selected by `Bill.date`. Does not derive monthly sales turnover or a fiscal source bridge. |
| `taxes/forms.py::ObligationForm` | Requires an entered base and source/rule reference. Its optional source invoice is for customer PPh 23, not a monthly final-turnover package. |
| `taxes/workflow.py::prepare_obligation` | Calculates supported reviewed rules from the supplied base. Final-turnover preparation is gated by the period profile. |
| `taxes/workflow.py::profile_for_period`, `profile_gates`, `taxes/rules.py::validate_final_profile` | Reuse period-effective company decisions, annual eligibility review, ordinary-PT legacy entitlement checks, and calculator bounds. Do not bypass these gates. |
| `taxes/workflow.py::record_external_review`, `subject_snapshot`, `_assert_review` | Reuse exact subject/decision digest binding and private PDF evidence. Add a controlled package subject contract. Recording a professional report does not independently verify the professional's credentials. |
| `taxes/knowledge.py::current_sources` | Requires approved, fresh, integrity-matching published source revisions, but applies effective dates against today. A period-applicable source selection contract remains necessary. |
| `taxes/views.py::export` | Exports entered obligations and bill candidates as draft CSV; not a submitted SPT. |
| `templates/app/tax_workspace.html`, `templates/taxes/actions.html` | Existing monthly workspace; currently directs Finance to copy checked figures. Extend this workflow rather than adding a separate dashboard. |
| `finance/services.py::monthly_summary`, `taxes/services.py::annual_readiness` | Provide commercial ledger aggregates. Their revenue totals are explicitly not fiscal turnover. |

There is currently no monthly source-package model, fiscal classification bridge, reviewed completeness record, or package-linked generated obligation. Supplier PPh 23 candidate collection also remains a separate capability: a bill's document date alone does not establish its statutory withholding period.

## Legal boundaries and authoritative references

For the final-tax branch, PMK 164/2023 Article 6 describes monthly gross business consideration before the specified discounts; its Rp500 million relief applies to individuals. The invoice model does not separately record pre-discount contractual gross. A negotiated wholesale price must not be treated as a discount merely because it differs from a catalogue price. [DJP — PMK 164/2023, Article 6](https://pajak.go.id/id/peraturan/tata-cara-pengenaan-pajak-penghasilan-atas-penghasilan-dari-usaha-yang-diterima-atau)

Turnover below Rp4.8 billion is insufficient to choose the ordinary-PT final regime. PP 20/2026 Article II(1)(e) preserves qualifying unexpired legacy entitlement subject to its conditions; the application must retain reviewed historical eligibility and expiry checks. [DJP — PP 20/2026, Article II(1)(e)](https://pajak.go.id/id/peraturan/perubahan-atas-peraturan-pemerintah-nomor-55-tahun-2022-tentang-penyesuaian-pengaturan-di)

Final tax can involve self-payment or qualifying customer withholding. The package must distinguish these paths and their evidence so that customer-withheld final tax does not produce a duplicate self-payment obligation. [DJP — PMK 164/2023, Articles 7–8](https://pajak.go.id/id/peraturan/tata-cara-pengenaan-pajak-penghasilan-atas-penghasilan-dari-usaha-yang-diterima-atau)

These primary texts were consulted for this investigation on 9 October 2026. They do not by themselves establish OSEE's eligibility, contractual classification, permitted tax timing, refund correction treatment, or a current official filing schema. Before activating a calculation path, a professional must confirm period applicability and interactions with amendments, including PMK 81. Do not infer current filing/payment deadlines solely from an older text. PMK 164 is absent from the inspected source catalogue; stage relevant provisions for recorded review, without automatically activating them.

Normal-regime packages remain useful commercial/fiscal reconciliation records. Display final tax as **not applicable** when that regime does not apply; never invent a monthly 22% tax on turnover. Do not reuse final-tax gross as annual Article 31E turnover without a separately reviewed definition.

## Deterministic extraction contract

Input: scoped organization and a validated first-of-month period. Define the exclusive next-month boundary, exact inclusive month-end cutoff, business timezone, extraction version, and canonical decimal representation. Never use floats.

Output: stable ordered facts, commercial revenue bridge, source completeness status, exception list, evidence manifest, and source fingerprint. This service may collect and reconcile facts; it must not classify taxable turnover itself.

| Source | Required facts and boundary |
| --- | --- |
| Posted `JournalLine` on `REVENUE` | Capture each line ID, journal ID/date/status/source key, debit and credit. Book contribution is credit minus debit. Sum exact lines scoped through both line and journal organization. |
| Invoice issuance | Capture original price/quantity/product/buyer/date/service date and issuance journal. Issuance posts receivable/deferred revenue and is not earned revenue. |
| `InvoiceDelivery` | Capture event ID, dated quantity/amount, evidence and journal FK. Validate the legacy `invoice:{pk}:delivery` journal path separately; do not count it again when unit-delivery events already supply that recognition. |
| `InvoiceCredit` | Capture date, kind, amount, quantities, evidence and journal FK. Service credit reduces commercial revenue; unfulfilled credit reduces deferred revenue. Preserve both as separate facts. |
| Invoice cancellation | Capture dated cancellation and posting, including invoices issued before the selected month. Cancellation of unperformed services is not a new deduction from previously earned revenue. |
| Receipts/allocations, applied advances, customer tax settlements | Capture dated gross settlement, bank cash and withheld tax separately. Money received before testing does not establish the same commercial or fiscal recognition date. |
| `CustomerRefund`, `CreditRefund` | Capture dated cash settlement and links to the original cancellation/credit. Do not reduce revenue again merely because cash was returned later. |
| Manual or unmatched revenue journals | Produce explicit orphan-source exceptions. Generic positive and negative entries remain exceptions even when their net amount is zero. |
| Imported/source documents | Preserve source IDs, hashes, integrity status, scope, coverage, duplicate/excluded observations and reviewed inventory. Recap/registration observations must not silently become invoice or revenue postings. |

Every posted revenue line must be assigned exactly once to a validated source event or an explicit orphan exception. A journal can contain several revenue lines: reconcile the signed sum of all its revenue lines to the expected linked event amount, retain individual line identities, and flag unexpected extra lines or mismatches. Do not multiply journal amounts by joining deliveries, credits and payments together. Event mapping is not sufficient if it silently omits a second revenue line. Equal ledger and source totals do not excuse unexplained offsetting entries.

Use explicit journal FKs where available, and validate legacy source-key mappings rather than accepting any matching string. Keep invoice issue date, delivery date, credit date, receipt date, refund date and professionally selected tax date distinct. Determine state as of the cutoff from dated events; current cancelled or delivered status must not erase earlier-period facts. Include carried-forward invoices and advances when events or fiscal decisions affect the selected period.

Never reconstruct historical sale values from today's price, IIEF procurement cost, reseller retail upsell, or an imported participant count. Document completeness across products, branches and channels remains a reviewed company assertion supported by inventories and evidence; software cannot prove absence of off-ERP sales from a balanced ledger alone.

## Package, fiscal bridge and reviewed-source contracts

Add an immutable `MonthlyTaxSourcePackage` with organization, period, revision/predecessor, extraction version, canonical source snapshot/hash, profile-decision reference, legal-source revision/publication references, and review/approval provenance. A reviewed decision payload can initially hold fiscal row classifications; a separate policy engine is not required for the first release.

Each fiscal decision identifies the source rows, applicable income category/regime, contractual gross basis, recognition date/month, included or excluded amount, adjustment reason and period, and supporting evidence. Reconcile the decision rows to the source inventory and commercial bridge. Distinguish bookkeeping corrections from fiscal differences and avoid double-counting a credit already present in book revenue.

Any unresolved timing, gross basis, exclusion, orphan journal or completeness issue yields `taxable_turnover = None`. An incomplete subtotal can be shown with its scope, but must not be presented as the complete tax base. An empty extraction is not a nil declaration. Zero is available only after reviewed completeness, supported classification and eligibility establish a genuine zero for the selected scope.

Retain `profile_for_period` and current profile gates. Add period-applicable legal-source selection recording revision ID, content hash and publication ID; require current review and account for a rule effective during a month. Do not simply apply today's latest source to an earlier period. Approval must bind the exact package, fiscal decisions, completeness record, profile decision and relevant legal revisions.

Fingerprint all material selected ledger lines/events, evidence integrity, coverage decisions, fiscal decisions and profile/legal versions. New backdated postings, relevant evidence changes, source completion or applicable rule changes make an existing package stale. Recompute before approval and draft generation under the existing organization lock. Keep historical approved snapshots viewable; do not silently rewrite them. A filed-period correction requires an explicit recovery path because amended-return history is not modeled.

## Supported generation and nontechnical workflow

1. Finance selects a month and clicks **Siapkan data bulan ini**. Extraction changes no company transactions.
2. The existing monthly page shows **Pendapatan dalam buku**, **Transaksi perlu diperiksa**, and **Dasar pajak yang sudah diperiksa**. Show source links and concrete reasons only where work remains.
3. Finance supplies missing documents. Owner records the professional report and approves the exact package and fiscal decisions using existing review semantics.
4. **Buat draf pajak** uses the approved package amount without retyping it, while retaining existing obligation approval and official evidence verification.
5. Downloaded CSV/source manifest uses the same snapshot and is labeled a draft source package, not SPT/XML, proof of payment, or proof of filing.

Add nullable `TaxObligation.source_package`; preserve legacy rows and do not repurpose its customer-PPh-23 invoice link. Generation must be atomic and idempotent. Detect an existing active manual final obligation for the same period rather than adding another. Explicit review is needed to retire/supersede a conflicting draft; approved or filed history must remain intact.

The inspected final-turnover obligation branch supports self-payment. If qualifying customer final withholding exists, implement its distinct evidence/settlement allocation first or block draft generation until the professional confirms that the month contains only supported self-payment transactions. Customer PPh 23 credits are a different instrument. Do not subtract a withholding from gross sales or create both an own-payment assessment and an already-withheld assessment for the same income.

Finance may prepare; Owner records approval; Marketing must not gain tax access through the new routes or source links. AI may explain local package status and exceptions. This feature must not expand approved external AI disclosure or activate a provider.

## Minimal phased implementation

| Phase | Files/contracts | Release condition |
| --- | --- | --- |
| 1. Source extraction | New `taxes/monthly_sources.py`; read existing Finance public facts/models. | Exact book bridge, source inventory and orphan/multiline exceptions. No automatic fiscal base or tax draft. |
| 2. Reviewed package | New package model/migration and `taxes/monthly_source_workflow.py`; controlled `workflow.subject_snapshot`; period-aware knowledge selection. | Immutable snapshot, reviewed fiscal/completeness decisions, eligibility/source integrity and freshness gates. |
| 3. Supported draft generation | Nullable obligation package FK, conflict/idempotency contract, monthly context/routes/templates/export. | Reviewed self-payment-only final branch generates exact approved figures; final-withholding uncertainty blocks. Normal regime has no invented monthly turnover tax. |
| 4. Broader fiscal reuse | Separately reviewed customer-final withholding and annual source rollup contracts. | Correct instruments, no duplicate assessment, separately defined annual turnover; amendments remain gated until history is modeled. |

## Executable acceptance oracles

These are required future tests, not tests run for this document.

- January invoice, February advance/receipt and March test produce separate dated facts. Book recognition follows the actual posting; taxable turnover stays unavailable until reviewed timing is supplied.
- Partial delivery, later service credit and a still-later cash refund reconcile each monthly revenue-line sum exactly. The refund produces no second revenue reduction.
- An old invoice retains its snapshot after price changes. A catalogue/wholesale difference produces no invented discount or gross-up.
- Every revenue line is represented exactly once. A multiline revenue journal with an unexpected line blocks approval even if a source-key match exists. Generic offsetting orphan entries cannot disappear because their net is zero.
- Missing channel/product/source inventory returns unknown completeness. Reviewed genuine nil activity can yield zero without confusing absence of postings with absence of business.
- Normal, expired legacy, missing registration/history and unreviewed eligibility cannot generate final turnover tax. A valid reviewed ordinary-PT legacy profile remains subject to period/year gates.
- Unapproved, tampered, stale or period-inapplicable legal revisions cannot approve a package. Historical periods retain the correct reviewed source provenance.
- New backdated events/evidence/classification change the fingerprint. A stale approval or concurrent generation rolls back atomically; repeat valid generation returns the existing draft rather than creating another.
- Existing manual final drafts raise a conflict. Customer final withholding cannot create duplicate self-payment liability; customer PPh 23 net bank receipt cannot reduce gross sale value.
- Cross-organization source IDs and POSTs cannot resolve records. Finance can prepare, Owner can record review, Marketing is denied. Exports and AI explanations use the same local snapshot without expanding external disclosure.
- Extraction does not modify invoices, journals, deliveries, bank transactions or company accounting data. Historical approved/unsupported amended records remain viewable without becoming newly approvable.

## Inputs requiring professional/company resolution

- Tax registration date, previous final-facility periods, normal-regime election, prior eligibility and relevant certificates; non-PKP status alone does not resolve income-tax eligibility.
- Principal-versus-agent classification under the IIEF agreements; sale proceeds versus commissions, and separate TOEFL ITP/iBT/course treatment.
- Permitted fiscal timing for prepaid tests and multi-session courses; contractual pricing versus discounts and evidence for any required gross basis.
- Period and treatment of cancellations, post-service credits, refunds and corrections. The sources cited above do not establish a blanket current-month deduction or carryback for every refund.
- Complete off-ERP sales and other receipts; bank interest, owner funding, loans and other income/non-object categories cannot be classified from bank descriptions alone.
- Customer final withholding versus customer PPh 23; supplier withholding timing, individual tutors and potentially foreign payments require their own supported reviewed classifications.
- Definitions and source completeness for annual fiscal turnover and Article 31E; these must not be inferred by reusing the final-tax base.

Until those contracts and gates are implemented and tested, the original goal of eliminating monthly manual taxable-turnover recomputation remains open.

## Implementation addendum — 9 October 2026

The original investigation and its future acceptance language above are preserved as the design baseline. A first supported implementation passed final combined acceptance on the source identity recorded in the monthly release. Its boundary is monthly source extraction, reviewed source-linked fiscal decisions, supported final self-payment drafts, normal-regime reconciliation without invented monthly corporate tax, and unchanged-facts professional review recovery. Customer final withholding, partial advance fiscal mapping, formal amendments and full annual fiscal schedules remain separate work; unresolved cases must stop generation rather than produce guessed amounts.

An Owner may request a new professional review version with a reason and exact current package identity. The new version retains the same financial snapshot and predecessor; it needs a new report bound to its own subject and classification. A repeat of the same pending request returns that version; stale or conflicting requests fail. Earlier versions remain readable and downloadable but cannot create or approve new liabilities.

An active final draft in the affected month or a later package that depends on its fiscal history must be explicitly withdrawn through the existing documented flow before review revision. Approved, paid or officially verified liabilities, and approved/filed annual workpapers for the affected year, require formal correction that this release does not implement. Pending review retains known old fiscal allocations and blocks dependent approvals until the replacement is reviewed; it never treats an already assessed amount as untaxed because a correction is pending.

The approved screen must explain the bridge from included book amounts through each actual signed timing/gross/correction adjustment to the reviewed total. Show reasons, source invoice and private PDF links, without summing contextual receipts or invoice issuance as additional income.

Combined acceptance reran native SQLite/PostgreSQL suites, independent boundary cases, browser mutations with persisted-state assertions at desktop/mobile sizes, and code/schema checks on one frozen source identity. The release documents exact counts and source binding. Component tests do not certify company tax classification, human UAT, official filing or cloud/provider readiness.
