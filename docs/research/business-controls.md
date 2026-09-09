# OSEE finance operating model and control architecture

Research date: 7 September 2026. This is a design note, not a statement of the company's current books, contracts, tax elections, or bank entitlements. Public descriptions were retrieved through web search; direct page fetches intermittently timed out. All proposed policies and acceptance targets below are architecture recommendations requiring confirmation during implementation discovery.

## 1. User-confirmed business model and public context

Later user clarification supersedes generic partner assumptions in the initial public research:

- OSEE's main current lines are official TOEFL ITP tests, official TOEFL iBT tests, and English courses. The user confirms OSEE is an authorized IIEF test center.
- The 40+ mitra are **wholesale resellers**: they buy from OSEE at agreed **ITP-only** prices in the user-confirmed range of Rp500,000–Rp530,000 and independently set their own onward selling price. They are not to be modeled as commission agents by default.
- Mitra **prepay each order before the test**. Credit terms and pooled deposit/wallet balances are optional future capabilities, not confirmed current operations or phase-one requirements. An order-linked payment received before delivery still needs correct advance/deferred-revenue accounting.
- IIEF purchase prices often change; for **ITP only**, the user described a commonly current amount around Rp450,000 “after tax.” That phrase establishes a quoted commercial amount, not a verified VAT base, withholding amount, tax rate, or recoverable-tax conclusion. Validate the components against the effective provider document.
- Applicability within exact ITP variants/formats, validity dates, reseller agreements and tax inclusion still needs confirmation. Do not copy these confirmed ITP price/cost ranges to iBT or to courses; each has a separate product and provider-cost profile.
- The supplied `2024 Invoice.pdf` is explicitly historical and current purchase prices differ. This note does not inspect or extract the PDF. Any later extracted figures must be dated historical evidence, not seeded as current price rules.

The following public descriptions are background evidence, subject to the more precise user-confirmed current scope above.

| Public evidence | Consequence for the design |
|---|---|
| The current OSEE homepage describes an authorized TOEFL test center under PT Langkah Pintar Nusantara, operating since 2014, offering TOEFL, TOEIC, preparation, and online courses. It advertises 45+ branches/partners. | Model the legal entity separately from the OSEE trading brand; prioritize test-session cash and costs, with courses as an additional revenue model. Treat each actual branch and independent partner differently. |
| Registration describes online remote-proctored ITP and paper-based testing for institutional partners, with variable session pricing and several result/certificate deliverables. | Store test format, session, participant, selling partner, service components, and promised delivery milestones. A price must come from a dated product/contract snapshot. |
| OSEE's About page describes digital marketing and translation/interpreting activities. | Provide service-line dimensions and configurable project/milestone billing. Confirm whether these activities use the same legal entity and should enter phase one. |
| IIEF's September 2026 public schedule lists OSEE as a Yogyakarta test institution. | There is independent primary-provider evidence for test operations; this does not establish OSEE's wholesale fees or contracts. |

Sources: [OSEE homepage](https://onestopenglisheducation.com/), [OSEE registration](https://onestopenglisheducation.com/registration/), [OSEE About](https://onestopenglisheducation.com/about/), [IIEF test schedules](https://www.iief.or.id/toefl-itp-test-schedules).

The website contains different partner counts, including 30+, 35, and 45+; the user confirms 40+ resellers. Do not encode the marketing count or classify resellers as owned branches/subsidiaries. Registration schedules can be stale. Exact ITP format/price validity/tax components, iBT pricing, prepayment cutoff/refund terms, headcount, actual software, transaction volumes, and accounting framework remain unconfirmed. The user supplied the existence of a BNI Direct account; no connection entitlement has been established by this work.

An affiliated site publicly identifies PT TITC Global Eduka and a test relationship with OSEE. This is a reason to ask which legal entities are in scope, not permission to consolidate them. [TITC legality](https://titc.or.id/legality/).

## 2. Architectural objective and boundary

Finance should record or approve each real-world fact once. The same fact should update the invoice, cash allocation, delivery obligation, payable, accounting ledger, tax workpaper, and management report through explicit rules. Monthly and annual reports should be reproducible outputs of approved records, not independent spreadsheets maintained by finance.

Distinguish these facts throughout the model: a booking exists; the customer owes an invoiced amount; money arrived; money is allocated to a booking; service was delivered; revenue was earned under an approved policy; a tax event occurred; a tax payment settled; a return was accepted. No single status called `paid` or `complete` should conflate these facts.

Start with finance plus the minimum operational evidence that makes finance automatic. Include enrollment/order capture, session delivery confirmation, vendor service approval, and partner settlement. A full LMS, timetable optimizer, marketing suite, and HR performance platform are separate later investments unless an existing system can send needed events.

The business must select its reporting framework and accounting policies with its accountant. The entries below illustrate an accrual model and exclude tax unless explicitly stated. Invoice timing, contractual rights, components, and recognition rules must be configurable; these examples are not conclusions about the company's accounting obligations.

## 3. Dimensions that must survive into reporting

| Dimension | Use and guardrail |
|---|---|
| Legal entity | Mandatory on every financial record; bank accounts, tax registrations, periods, approvals, and books belong to it. Never net distinct entities implicitly. |
| Brand/business line | TOEFL ITP, iBT, TOEIC, courses, translation, digital marketing if confirmed; management dimension rather than legal ownership. |
| Branch/location | Internal operating unit with named owner; distinguish physical venue from tax reporting location. |
| Reseller/partner | Default: external wholesale customer who independently resells and prepays each order; agreement, price tier, funding allocation and payment cutoff govern fulfillment. Other roles are explicit contract exceptions. |
| Channel | Website/direct, partner, corporate, campaign where available; preserve the source ID to audit changes. |
| Product and version | Separate ITP, iBT and course SKUs/formats; fee components, provider, recognition rule, cost rule and tax profile reference. Wholesale selling price and provider cost have distinct temporal versions. |
| Session/cohort/project | Delivery and cost attribution, seat utilization, contribution margin; not required on unrelated overhead. |
| Customer/payer/participant | Different parties: a parent, employer, or partner may pay for many participants. Avoid making each bank sender a new student. |
| Counterparty tax profile | Person/entity, residency, identifiers, effective dates, evidence status, selected rule set; collected only where needed. |
| Contract/obligation | What was promised, quantity, dates, cancellation policy, principal/agent assessment, corporate PO/BAST references when used. |
| Accounting date, event date, tax period | Retain each separately; they can differ. Never derive all dates from a bank posting. |
| Cost center and allocation version | Direct costs attach to session/project; overhead allocation is transparent and separately displayed. |

Keep the chart of accounts compact. Do not create a new ledger account per participant, partner, or session; use subledgers and dimensions. Required dimensions should depend on transaction type, so bank charges do not require a test participant.

## 4. Core business flows

### 4.1 Direct test booking and provider costs

1. A booking captures product/version, session, participant, bill-to party, price/discount approval, cancellation terms, and source reference. Booking alone is not a cash receipt.
2. Issue a payment request or invoice according to the approved accounting policy. Maintain an invoice number separately from a booking code.
3. The bank statement/import confirms cash. A unique reference or approved allocation links it to the invoice; a uploaded screenshot is supporting evidence and cannot establish settlement.
4. If cash is received before an invoice, post it to a customer advance or unallocated-receipt account, then reclassify through an allocation workflow. Do not create unexplained income.
5. Where an invoiced service remains undelivered, one illustrative posting is debit receivables and credit deferred revenue; receipt debits bank and credits receivables. This design separates earning from collection.
6. An authorized operations person confirms the contractual delivery milestone. A recognition job releases the eligible deferred balance and accrues the related provider/proctor/session costs once.
7. The finance policy decides whether testing, score access, certificates, and courier services are one combined obligation or distinct obligations; the software must not invent this assessment. Store the policy version and evidence used.
8. Provider invoices reconcile to purchased/consumed seats, session rosters, or vouchers. Record ordered, received/available, reserved, consumed, expired, and credited quantities where those concepts exist in the contract. Do not assume vouchers are physical inventory or recognize provider costs only when cash leaves the bank.

### 4.2 Course packages and installments

A package has an entitlement quantity and recognition schedule. Sessions delivered, attendance corrections, authorized cancellations, substitutions, freezes, extensions, and transfers affect the operational entitlement ledger. Recognition follows the approved performance policy: per delivered lesson, elapsed service period, or another documented measure. A payment plan governs due dates and overdue balances independently of revenue recognition.

Illustrative package: Rp3,000,000 invoiced for 12 equal lessons under a policy that recognizes each lesson delivered. Invoice creates Rp3,000,000 receivables and deferred revenue. A Rp1,000,000 receipt clears part of receivables. Four eligible lessons release Rp1,000,000 to revenue. Remaining receivable is Rp2,000,000 and deferred revenue Rp2,000,000. If no invoice is yet legally due, use the approved advance/contract accounting path instead of manufacturing a receivable.

Unused package expiry and no-shows are distinct events. Require a policy and contract evidence before releasing unused credits to income. A reschedule does not generate a second sale or a second provider-cost accrual.

### 4.3 Wholesale reseller sales, corporate billing, and exceptional partner arrangements

The default reseller order is a sale by OSEE to the reseller at the agreed wholesale price. The reseller is the bill-to debtor; participants are delivery beneficiaries and do not become OSEE retail debtors merely because their names appear on a roster. The reseller's own markup belongs to its independent resale business. Do not create an OSEE retail invoice at the reseller's onward price, then deduct an invented commission. Recognize OSEE's wholesale revenue and its provider expense separately under the approved principal policy and delivery timing. Tax timing and turnover treatment remain independently approved tax rules.

ITP-only example, excluding an unresolved tax decomposition: if the agreed OSEE wholesale amount is Rp510,000 and the comparable provider cost is Rp450,000, the preliminary spread is Rp60,000 before other costs. A reseller's independent onward price does not increase that spread or OSEE's revenue. Label the spread provisional until price/cost tax bases and recoverability are comparable. These numbers do not establish an iBT price.

Corporate customers may separately have contracts, purchase orders, delivery acceptance, batch participants and credit terms if confirmed. Current resellers prepay per order. One invoice can cover many participants; one bank transfer can fund several orders only when its allocation covers each order explicitly. Partial payment keeps the affected order unfunded; it does not create automatic credit permission. A reseller statement shows opening balance, OSEE wholesale orders/invoices, order-linked advances, approved credit notes/refunds, cash and supported withholding settlement components, and closing balance. Bank reconciliation allocates only actual cash; invoice settlement may additionally include approved noncash components and cannot exceed invoice outstanding value.

If a participant pays OSEE on behalf of a reseller, allocate the receipt to the reseller's obligation only with a documented payer relationship and allocation approval. Do not silently reinterpret it as a separate retail sale. Where OSEE also has direct retail orders, use a separate channel, bill-to party, price book and contract snapshot.

Perform a principal-versus-agent review once for the default standard reseller/provider contract and version it; do not force finance through an open-ended classification on every routine order. Reopen the review for materially different arrangements: genuine sales agency/commission, collection on behalf of another principal, consignment, contract novation, or changes in control/responsibility. Only those approved exceptions use agency/commission settlement components. Cash collection alone does not establish principal status.

Where a customer withholds tax, gross receivable settlement may comprise cash plus a supported tax-credit component. Missing withholding evidence becomes an owned exception; it must not silently become a discount or bad debt. An annual tax credit is claimed only through the tax control workflow. Track affiliate or intercompany flows separately, with due-to/due-from balances and a matching reference in both books.

### 4.3.1 Temporal price books and immutable order snapshots

Separate reseller sales prices from provider purchase costs. Required records and fields:

| Record | Minimum fields and purpose |
|---|---|
| `ResellerAgreement` | Reseller/entity IDs, agreement version, permitted products/formats, wholesale role, cancellation rules, price-tier reference, per-order prepayment rule/cutoff, validity and approval/evidence. Credit/deposit settings are future optional extensions disabled in the initial scope. |
| `SalesPriceVersion` | Entity, product/format, reseller-specific or tier scope, currency, unit selling amount, tax-inclusion treatment, quantity band if used, effective-from/to, approval, source document and version. Explicit precedence: agreed reseller override over tier; no ambiguous overlapping winner. |
| `ProviderCostVersion` | Provider, product/format, currency, quoted gross amount, confirmed base/tax components or unresolved status, applicable commercial trigger, validity, quote expiry, quantity band, source document/date, approval and version. Never infer a new current cost from an undated old invoice. |
| `ProviderCostLot` | Actual purchase/commitment batch or voucher entitlement when one exists: product, provider contract and accepted cost version, available quantity, consumed/reserved quantity, expiry, purchase/order/invoice references, tax facts and cost status. Do not fabricate lots when the provider bills per delivered test without purchased entitlements. |
| `OrderLineSnapshot` | Bill-to reseller, participant references, product/format, quantity, agreed selling unit amount, discount/tax breakdown, price version, service date, cancellation terms, prepayment rule/version and cutoff, provider estimated or locked cost reference, expected margin status, approval and snapshot version. |
| `CostReservation` | Order line, provider lot or valid quote/contract reference, quantity, expiry, reservation state, cost status (`estimate`, `contractually_locked`, `invoiced`), and consumed/released links. A soft internal estimate is never presented as a provider price guarantee. |
| `ActualCostAllocation` | Provider invoice line, delivered order/session/lot, allocated quantity and cost components, estimate/accrual reference, variance amount/reason and approval. Preserves actual cost without editing the accepted selling price. |

Validate price versions at quote creation and again at order confirmation. Confirm a reseller agreement and price precedence; snapshot the accepted quote/order so later edits affect future sales only. A changed provider price creates a new version. If an order is already contractually accepted at an older selling price, flag the margin impact; do not automatically reprice the reseller or rewrite historic revenue.

Provider contract terms choose whether cost is determined at reservation, purchase, confirmation, test delivery, or another defined event. Record that trigger. A reservation can guarantee cost only if the provider actually guarantees it. Otherwise, display provisional margin, recheck before the provider commitment, and route insufficient/unknown margin through an approved exception. Margin thresholds, if used, are business policy rather than hard-coded rupiah values.

On delivery, consume the linked entitlement/commitment once and recognize or accrue cost under the approved accounting policy. Match the provider's final bill to the accrual/lot and post a linked variance or adjustment; never add the whole invoice as a second cost. A change after a closed period follows the controlled correction policy. Cancellation/rescheduling releases or transfers reservations and applies supplier and reseller credit terms separately. Quantity reservations need transactional constraints to prevent overselling the same available entitlement.

### 4.3.2 Current reseller workflow: prepayment per order

Resolve the ITP/iBT-specific agreed selling price, freeze the accepted order, issue its payment request or invoice under the approved accounting policy, and allocate confirmed bank cash to that order. An uploaded transfer screenshot supports investigation but is not proof of settlement. Invoice/receivable creation, cash collection, funded status, provider commitment, service delivery and revenue recognition remain separate events.

Funded status requires the full obligation to be covered by allocated cash plus any legally applicable, supported withholding component; an unexplained shortfall remains unpaid. Check the funding gate before the test under the user-confirmed rule. Whether to demand funding before committing to IIEF is a separate operating-policy decision; do not claim the user has confirmed that earlier cutoff. Show upcoming unfunded orders, stale bank data, expiring quotes, unconfirmed provider cost, missing participant data and capacity conflicts. An exception requires an explicit authorized order-specific decision and is never labeled “paid.”

Money received before a valid receivable exists is an order-linked customer advance; an invoice that establishes an unconditional receivable for undelivered service can instead use AR/deferred revenue. Apply only the selected accounting path and retain the undelivered obligation. Never count an advance twice as both unused funding and invoice settlement. One order may have several receipts, and one receipt may fund several orders, with allocation locks and outstanding checks. Show staff `Menunggu pembayaran`, `Kurang bayar`, `Pembayaran terverifikasi`, `Siap tes`, `Harga perlu dikonfirmasi`, and `Margin perlu ditinjau`.

### 4.3.3 Optional later extension: pooled advances or credit

Do not build a wallet or reseller credit-management workflow as a prerequisite for phase-one reporting. Add it only if OSEE later adopts those commercial terms. If introduced, customer advances must reconcile to the liability subledger; refundable cash and promotional credits stay distinct. Future credit exposure should include open AR plus confirmed uninvoiced commitments less usable unallocated advances once. Move exposure from commitment to AR atomically to prevent double counting and lock concurrent reservations against headroom. These are future design safeguards, not current OSEE practices.

### 4.4 Refunds, chargebacks, and corrections

Refunds link to the original booking, invoice, receipt allocation, delivery/recognition history, and policy. Calculate the refundable service portion, any contractually allowed fees, and tax correction needs; present the calculation before approval. Split into request, eligibility reviewed, amount approved, payment requested, bank accepted, settled, and reconciled states.

For an unearned service, reverse the relevant liability/receivable through a credit note and refund accounting. For an earned service, the accountant-approved correction may differ. Never delete the original receipt or overwrite a posted invoice. Returned transfers and chargebacks are new bank events that reverse the relevant settlement, preserve the original reference, and reopen collections if appropriate.

### 4.5 Tutor, proctor, translator, and supplier expenses

Approved work evidence (lesson, session, timesheet, milestone or goods/service receipt) creates a draft obligation or accrual. A bill is matched to contract/rate card, delivery evidence, and duplicate checks. Classify the supplier relationship and tax treatment before payment. An individual tutor and a corporate vendor must not automatically receive the same withholding code.

Store service period, tax event date, invoice date, due date, gross basis, applicable tax components, net payable, beneficiary snapshot, and evidence. If an invoice arrives after closing but services were delivered, use an accrual and an approved subsequent reversal/match. Maintain employee payroll through a separate payroll/tax interface if payroll is initially out of scope.

## 5. Minimum accounting and control invariants

1. Every posted journal balances by entity and currency policy; amounts use decimal arithmetic and an explicit rounding policy.
2. The general ledger is the only book of record. All reports trace to posted journals or clearly labeled operational forecasts. Do not maintain a second independently editable tax-report ledger.
3. Posted accounting is immutable; corrections create linked reversals and amendments. Closed periods reject ordinary backdating. Reopening requires a reason and independent authorization and triggers report restatement visibility.
4. One economic event posts once. Import file hashes, bank IDs, source IDs, and versioned idempotency keys prevent retries and duplicate webhook events from creating duplicate receipts or costs.
5. Allocation totals never exceed available receipt, invoice, or payable balances. Cross-customer, cross-currency, and cross-entity allocation needs an explicit controlled workflow.
6. Cash transactions remain distinct from bank statements. An imported statement line is evidence for reconciliation, not authority to book the same cash again.
7. Unearned revenue rolls forward from opening balance, new obligations, recognition, refunds/corrections, and closing balance; it reconciles to the ledger and outstanding operational obligations.
8. AP/AR, fixed assets, advances, inventory/vouchers if used, tax liabilities, and received tax-credit subledgers reconcile to their control accounts each close.
9. Tax calculation uses a versioned approved rule and evidence effective for the transaction, not today's mutable supplier settings. Uncertain classifications are blocked from tax finalization, with an owner and reason.
10. Payment initiation, bank acceptance, bank settlement, ledger posting, and reconciliation have independent states. Failed or unknown external outcomes cannot be silently marked settled or blindly retried.
11. Vendor master changes, especially beneficiary changes, invalidate pending approvals affected by the change. Approval applies to a locked payment snapshot and total/hash, not to an editable screen.
12. Documents, journals, approvals, exports, and acknowledgments carry actor, time, correlation ID, version, and attachment hash. Report reruns state their cutoff and source snapshot.

## 6. Roles and compensating controls for a small finance team

| Role | Normal actions | Restriction |
|---|---|---|
| Enrollment/customer service | Create bookings, customer details, payment requests, refund requests; see relevant payment status | No bank credentials, tax rule edits, journal posting override, or refund release |
| Operations/service owner | Confirm lessons/tests/project delivery and bill evidence | No approval of their own vendor payment |
| Finance preparer | Review imports, allocate receipts, draft bills, reconcile bank, prepare close and tax packs | No sole approval of own high-risk vendor change/payment or filed-return alteration |
| Finance manager/accountant | Approve accounting policy exceptions, tax classifications, period close and reporting | Approval scope and limits are explicit; no shared account |
| Owner/director/bank approver | Approve payment batch and designated statutory submission | Readable cash impact and exceptions, separate bank authorization |
| Tax adviser/reviewer | Limited access to tax workpapers, supporting ledgers, adjustments and exports | No unrestricted bank execution or irrelevant student records |
| Technical administrator | Operate infrastructure and access provisioning | No financial approval role; production data access is logged and restricted |

If staffing cannot separate every duty, the owner reviews a scheduled exception report of beneficiary changes, manual journals, refunds, write-offs, reopened periods, and unmatched bank activity. This compensating control is recorded in the system. Approval limits are configurable by transaction type and amount; finance must choose them instead of developers inventing fixed rupiah limits.

## 7. User experience for nontechnical finance

The default interface should use Bahasa Indonesia with familiar local terms and currency/date formats. Use seven clear areas: `Hari ini`, `Uang masuk`, `Tagihan & pembayaran`, `Pajak`, `Tutup buku`, `Laporan`, and `Dokumen`. Hide technical integration logs from the everyday flow while exposing readable status and a support action.

`Hari ini` prioritizes owned work rather than charts: incoming payments needing identification, expenses missing documentation, approvals awaiting the director, taxes approaching an internal review date, and a bank feed that has stopped updating. Each card shows amount, period, reason, responsible person, and one next action.

For a matched payment, show “Rp500.000 dari BNI cocok dengan Tagihan INV-0001” and the matching reasons: reference, amount, date, payer. For ambiguity, show candidate invoices, supporting document preview, and `Pilih tagihan` or `Minta informasi`; do not auto-select the highest-scoring name match. Batch approval includes per-item exceptions and a preview of totals.

Tax terminology stays visible where necessary: “Bukti potong belum diterima” is more actionable than “Tax credit validation failed.” A side panel explains what is missing and the consequence. OCR/AI may propose extracted fields or category suggestions, with confidence and evidence. Deterministic validations and authorized review govern accounting/tax/payment results.

Monthly close is a short guided checklist with step counts and accountable owners: bank complete; receipts identified; vendor bills/accruals complete; services and deferred revenue checked; taxes reconciled; statements reviewed; period locked. A green status must mean the check passed at a known cutoff, not merely that a button was clicked.

Every statement supports drill-down: report line → account movements → journal → invoice/contract/bank line → attachment/approval. Provide downloadable PDF/XLSX packages with consistent totals, clear draft/final/version labels, and explanations of month-to-month changes. Use accessible contrast, keyboard navigation, plain errors, saved drafts, and easy correction paths. Mobile supports document capture and approvals; desktop supports reconciliation and close.

## 8. Monthly close and annual preparation

The monthly schedule below is an internal service target, not a statement of statutory deadlines. Finance should choose a close day and earlier tax review buffers according to actual filing obligations.

| Stage | Automatically prepared | Human responsibility and blocking exceptions |
|---|---|---|
| Daily | Import health, bank control totals, candidate matches, due payables, missing tax profile/evidence | Resolve ambiguous cash, confirm service delivery, approve changed counterparties |
| Month end | Cutoff list of bookings, delivered/unbilled services, unused obligations, consumed/unbilled provider seats | Confirm completeness and timing; resolve delayed operational evidence |
| Early close | AP/AR aging, bank reconciliation, accruals, prepaids, depreciation, revenue schedules | Review adjustments, bank timing differences and suspense balances |
| Tax review | Withholding event register, tax liability rollforward, received-credit register, reconciliation and export draft | Resolve missing identifiers/documents, policy exceptions, tax mapping and amendments |
| Management review | Trial balance, P&L, balance sheet, cash flow, cash forecast, variance notes | Approve books, explain material changes, review margins and overdue exposures |
| Lock/archive | Versioned close pack and data cutoff, signatures, attachments and report hashes | Authorized close/reopen; accepted statutory receipts recorded independently |

The monthly management pack should include cash and cash forecast, income statement with prior-month/budget comparison, balance sheet, cash flow, AR/AP aging, deferred-revenue rollforward, test/session/partner contribution margins, refund trends, tax status, and unresolved exceptions. Contribution margins must distinguish direct contribution from allocated-overhead margin and disclose the allocation method.

Annual preparation starts on day one: retain monthly ledger snapshots, tax-credit evidence, asset book and fiscal bases, disallowable-expense classifications, tax payment confirmations, financing/interest details, related-party schedules, losses/credits carried forward where applicable, equity movements, and prior-year return/amendment information. Each annual return field maps to an approved ledger source, fiscal adjustment, master-data field, or explicitly required questionnaire answer. The system must identify unsupported fields, not insert a convenient zero.

Final annual output is a reproducible workpaper package: opening balance reconciliation, final trial balance, financial statements, commercial-to-fiscal reconciliation, depreciation differences, credit/payment schedules, selected return mappings, supporting attachments, reviewer sign-off, export version, and filing acknowledgment. Statutory calculations and formats belong to the separately maintained Indonesian tax adapter and must be reviewed against current primary rules.

## 9. Migration and cutover

1. Agree legal entities, accounting framework, fiscal year, reporting currency, approved account map, dimensions, materiality, tax policy, and start date.
2. Inventory all inputs: existing books/spreadsheets, bank accounts and statements, outstanding invoices/bills, deposits, active course packages, future test bookings, unused vouchers/provider advances, payroll outputs, fixed assets, tax returns and evidence. Do not assume the BNI account is the entire business.
3. Deduplicate parties carefully using business IDs and reviewed matches. Participant names alone do not uniquely identify payers. Preserve source records and an import mapping.
4. Load opening trial balance and supporting open-item schedules, with clear migration-only document types. Load outstanding deferred obligations and tax credits individually; an unexplained opening total cannot support later automation.
5. Reconcile opening bank balances, AR/AP, deferred revenue, assets, provider prepayments, tax liabilities/credits, and retained earnings. Require sign-off; record any authorized adjustment.
6. If starting midyear and expecting the current year's annual return, import complete fiscal-year transactions or controlled year-to-date account/tax schedules plus drill-down evidence. An opening balance alone cannot produce a defensible full-year return.
7. Run a representative historical month and current live period in parallel. Compare statements and tax workpapers to approved expected outputs, documenting actual prior-book errors rather than forcing the new system to reproduce them.
8. Freeze legacy entry at cutover, communicate the system of record, import final deltas, and reconcile counts and totals. Retain old data read-only and establish rollback/recovery from verified backups.

## 10. Business acceptance scenarios and success measures

| Scenario | Pass condition |
|---|---|
| Same bank file imported twice and overlapping API dates | One canonical bank transaction and one cash posting; duplicate is traceable |
| Two customers pay identical amounts on the same date | No unsafe automatic allocation; finance resolves with evidence |
| One company pays 20 participants across three invoices | Receipt split correctly, each participant status correct, receivable total exact |
| Partial payment, overpayment, wrong reference | Remaining AR/credit/unallocated amount correct with no fabricated revenue |
| Prepaid test straddles month end | Cash, receivable, deferred revenue, recognition, and provider cost fall in approved periods |
| Test canceled or rescheduled after provider commitment | No second sale; refund/fee/provider-credit treatment follows versioned contract |
| Lesson package, installment plan, freeze, transfer, expiry | Entitlement and accounting rollforwards reconcile; unauthorized expiry income blocked |
| Approved agency exception deducts commission and customer withholding | Exception contract's gross/net policy correct; settlement components and missing-credit evidence visible; standard wholesale orders unaffected |
| Reseller buys at Rp510,000 and independently resells higher | OSEE invoices only its wholesale amount to the reseller; no invented commission or OSEE receivable for the reseller's markup |
| ITP price book changed while an accepted order exists | Accepted order retains its selling price and terms; future quotes use the new valid version; iBT prices remain independent |
| Provider cost changes after quote but before commitment | Contract-defined trigger and locked/estimated status select the correct cost; margin risk visible and exceptions approved |
| Two orders reserve the same final provider entitlement or cash allocation | At most the available quantity/cash is committed; the other request receives a readable exception |
| Mitra pays only part of an order before the test | Order remains visibly unfunded; delivery follows the prepayment gate or an explicit order-specific authorized exception |
| Provider bill differs from cost accrual | Linked variance posted once; accepted reseller price unchanged; no duplicate provider expense |
| Historical 2024 invoice entered during migration | Evidence and costs remain in the correct historical period; no current provider price overwritten |
| Tutor is an individual, supplier is a company | Distinct tax-profile decision paths; unresolved classification cannot finalize tax output |
| Beneficiary changed after approval | Prior approval invalidated and payment held until new approval |
| Bank times out after payment request | Status becomes unknown/pending inquiry; no duplicate blind retry |
| Bill arrives after month close | Controlled accrual/matching or approved correction; locked period protected |
| Historical tax rule changes | Prior transaction snapshot/reports remain reproducible; amendment is separately versioned |
| Annual pack generated after midyear migration | Full-year source totals reconciled; missing opening/YTD evidence explicitly blocks finalization |
| Finance preparer attempts own payment release | Role/approval policy rejects it and logs the attempt |
| Report regenerated from a locked snapshot | Same deterministic totals and linked source versions |

Proposed targets to validate using real anonymized samples: every posted record traceable to evidence and actor; bank and subledger reconciliation differences explained to the rupiah; zero duplicate postings in retry/replay tests; all submitted tax outputs have an approval and acknowledgment; standard monthly pack produced without copying amounts between spreadsheets. Measure auto-match precision separately from auto-match rate: never increase automation by accepting false positive matches. Track hours of finance work per close, time to resolve exceptions, percentage of complete tax profiles, and missed/late filings. Do not promise a specific straight-through automation percentage before measuring real data quality.

## 11. Build versus configure: narrow decision framework

Evaluate an established ledger before commissioning a new accounting engine. OSEE's distinctive work is its simple user interface, operational evidence, partner settlement, BNI integration and Indonesian tax workflow. Rebuilding posting, reversals, aging, asset accounting, and period controls creates a larger verification and maintenance burden.

ERPNext's official documentation describes deferred-income scheduling, bank-statement import/reconciliation, and payment allocation including one-to-many, many-to-one, and partial allocations. These are useful baseline capabilities, not proof of OSEE-specific recognition or Indonesian return coverage. ERPNext's time-based deferred accounting still needs a design for test completion and lesson-driven recognition. [Deferred revenue](https://docs.frappe.io/erpnext/deferred-revenue), [bank reconciliation](https://docs.frappe.io/erpnext/bank-reconciliation), [payment reconciliation](https://docs.frappe.io/erpnext/payment-reconciliation).

Odoo's current fiscal-localization documentation describes country packages and configuration needs. Its Indonesian localization documentation identifies accounting, e-Faktur, and Coretax e-Faktur XML-generation modules, with upload to Coretax and Coretax-issued invoice numbers. This does not establish an end-to-end PPh 23/annual corporate-return or BNI Direct connector capability. Official English Odoo 18 documentation and current translated Odoo 19 documentation both describe this distinction. [Odoo country localization](https://www.odoo.com/documentation/19.0/applications/finance/fiscal_localizations.html), [Odoo 18 Indonesia](https://www.odoo.com/documentation/18.0/applications/finance/fiscal_localizations/indonesia.html), [Odoo 19 Indonesia documentation](https://www.odoo.com/documentation/19.0/ro/applications/finance/fiscal_localizations/indonesia.html).

Use a fixed proof-of-fit pack of the acceptance scenarios above. Score: accounting control fit; tax-year/schema coverage and who maintains it; supported BNI access; nontechnical task completion; upgrade-safe customization; data export and auditability; hosting/privacy/security operations; implementation/support capacity in Indonesia; three-year cost including tax updates and recovery support. Verify licensing, edition, hosting/API/customization restrictions, extension quality, and exact tested versions before selection.

Conditional recommendation: configure one ledger platform and build OSEE-specific modules/adapters if the proof of fit passes. Choose a custom ledger only when documented requirements cannot be met reasonably and the company accepts ongoing accounting-engine validation and regulatory maintenance. Whichever platform wins, use exactly one posting authority; a custom portal must call supported business APIs and never write independently to a parallel general ledger or directly modify backend accounting tables.

## 12. Discovery questions that materially change the architecture

- Which legal entities, NPWP/NITKU registrations, branches, and bank accounts belong in phase one? Is PT Langkah Pintar Nusantara the sole filer, and is the intended annual return corporate?
- For the confirmed ITP-only Rp500,000–Rp530,000 selling range and roughly Rp450,000 provider quote, which exact ITP variants/formats and validity dates apply? What are the verified tax components and provider price-lock trigger? What separate iBT prices/costs and fulfillment rules apply?
- For confirmed per-order reseller prepayment, what is the exact cutoff before a test, and does OSEE want an earlier funding gate before provider commitment? Who bears provider price changes, cancellation/no-show charges, or refunds after order acceptance? Are any contracts actual agency exceptions to the wholesale default?
- What are the current accounting framework, revenue policies, tax regime/status, fiscal year, last filed year, and outstanding corrections?
- How many monthly bank lines, invoices, participants, partners, suppliers, employees, and finance users must the solution support? What are peak registration loads?
- What system/form currently records bookings, attendance, test completion, certificates, customer consent, and corporate service acceptance?
- Which BNI services and file formats are currently enabled, and which approvers release payments? Can finance supply a redacted sample statement and settlement file during implementation discovery?
- Is the immediate aim the next month and current year's annual return? How complete and clean are year-to-date books, tax certificates, and opening balances?
