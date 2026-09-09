# Indonesian tax architecture research for OSEE

Research date: 7 September 2026. Scope: finance ERP architecture for PT Langkah Pintar Nusantara, confirmed by the user during research. Both PPh Pasal 23 and the turnover-final regime colloquially called PP23 are required. The user confirms an **ordinary PT (non-perorangan)**, **current non-PKP status**, and annual turnover below Rp4.8 billion; the exact turnover amount/year and historical status effective dates were not specified. This note specifies design requirements; it does not determine final-tax eligibility without registration, detailed turnover history, elections and transaction evidence.

### Current-context amendment — 7 September 2026

**Superseding context:** Earlier discovery questions about whether OSEE is an ordinary PT and currently PKP have now been answered by the user. Generic profile fields and onboarding controls below remain relevant to documentary verification and effective-dated history; they are not open questions about the user's stated present status. Current non-PKP status must not be backdated to the May 2026 invoice or any other historical period without evidence. The ordinary-PT transitional analysis already documented below now applies to the confirmed legal form; registration and eligibility history remain unresolved.

**Ordinary domestic sales:** Configure OSEE's current non-PKP commercial invoices without collecting output VAT or generating Faktur Pajak. PKP registration and collection duties are addressed by PP44/2022 Article2; VAT Law Article14 prohibits a non-PKP from issuing Faktur Pajak. Keep this status distinct from a zero-rated supply or an education exemption. [PP44/2022, Article2](https://www.pajak.go.id/index.php/id/peraturan/penerapan-terhadap-pajak-pertambahan-nilai-barang-dan-jasa-dan-pajak-penjualan-atas), [DJP consolidated VAT Law, Article14](https://www.pajak.go.id/sites/default/files/2021-12/SDSN%20UU%20PPN%20Indo-%20dengan%20tanda%20perubahan_UU%20HPP.pdf)

**Supplier VAT:** Do not create a currently recoverable VAT credit merely because IIEF's invoice states VAT. Non-creditable supplier VAT follows the underlying expense, entitlement/prepayment or asset in the proposed books, with its source component retained. Income-tax deductibility is a separate decision: PP94/2010 Article10 requires supporting payment and business-purpose conditions for the relevant non-creditable tax and capitalization/depreciation or amortization for qualifying expenditure benefiting more than one year. It is not an automatic immediate deduction, and a cost does not reduce a turnover-final tax base. [PP94/2010, Article10](https://stats.pajak.go.id/id/peraturan/penghitungan-penghasilan-kena-pajak-dan-pelunasan-pajak-penghasilan-dalam-tahun-1), [JDIH amendment/status register](https://jdih.kemenkeu.go.id/dok/pp-94-tahun-2010)

**Income-tax obligations remain:** The PPh23 withholding-party definition includes domestic corporate taxpayers without a PKP prerequisite. Current non-PKP status therefore does not disable applicable PPh23 treatment or the corporate annual return workflow. [Income Tax Law Article23, official text](https://pajak.go.id/id/undang-undang-nomor-36-tahun-2008), [PMK81/2024, Articles169 and171](https://www.pajak.go.id/id/peraturan/ketentuan-perpajakan-dalam-rangka-pelaksanaan-sistem-inti-administrasi-perpajakan)

**Boundaries:** Non-PKP does not create a universal exemption from import or foreign-service VAT. VAT Law Articles3A(3) and4 provide the separate scope; retain a reviewed foreign-purchase/PMSE or import path and evidence of who collected or paid the tax. Later PKP registration also needs a fresh review of any transitional input-tax treatment under Article9(9a); never promise a refund of exact historical invoice VAT. [DJP consolidated VAT Law, Articles3A,4 and9](https://www.pajak.go.id/sites/default/files/2021-12/SDSN%20UU%20PPN%20Indo-%20dengan%20tanda%20perubahan_UU%20HPP.pdf)

Monitor whether registration becomes required or is elected, and assess historical exceptions through the reviewer. The user-reported current status is not a finding that registration was never required in an earlier year. For chat and invoices, display “OSEE berstatus non-PKP; invoice ini tidak mengenakan PPN” where appropriate, while storing an explicit non-PKP status reason internally rather than a statutory VAT-facility code.

### Confirmed operating model and supplied historical invoice observation

The user subsequently confirmed a TOEFL ITP wholesale model: IIEF currently costs approximately Rp450,000 per seat described as “after tax”, subject to change; OSEE charges resellers approximately Rp500,000–Rp530,000 for **ITP only**. More than 40 resellers independently set their onward selling prices and **prepay each order before the test**. The exact current Rp450,000 base/tax/withholding breakdown is unresolved. These are user-supplied operating facts, not verified tax treatments or price quotations.

The lead agent inspected a user-supplied **proforma** dated 7 May 2024 and reported: TOEFL ITP Level 1, 13 × Rp380,000 = Rp4,940,000; a separately stated 11% VAT amount of Rp543,400; total Rp5,483,400, or Rp421,800 per seat including that stated historical VAT. This research agent did not independently inspect the image. The document is a historical example and does not establish the current tariff, tax rate, VAT recoverability, PPh23 treatment, or final recognized expense. Treat a proforma as provisional commercial evidence; obtain the final supplier invoice, applicable official tax documentation, settlement and delivery/consumption evidence before assigning their respective verified statuses.

Recommended default workflow for this confirmed model: reseller order → per-order prepayment allocation → deferred order obligation under the approved recognition policy → delivery/right-transfer evidence → recognized wholesale revenue. Record the supplier acquisition, entitlement/advance, consumption and tax components separately. Validate the actual recognition trigger against the contract; cash received before a test does not by itself establish when all contractual obligations are fulfilled. Retain agency treatment for contracts whose substance supports it. Independent reseller upsells should not automatically become OSEE revenue or turnover, and external reseller turnover must not be aggregated as though the reseller were an internal branch.

## 1. Architecture-critical conclusions

1. Implement separate tax obligations for **PPh23 withheld from suppliers**, **PPh23 withheld by customers**, **turnover-based final PPh where eligible**, **PPh21/26 for staff and individual tutors**, and **annual corporate income tax**. They have different accounting and reporting meanings.
2. Do not default this PT to 0.5% final PPh. The regulatory chain is PP23/2018 → PP55/2022 → PP20/2026, including grandfathering. Registration history and eligibility history are mandatory configuration data.
3. Bank deposits are settlement evidence, not automatically sales revenue, taxable turnover, or tax paid. Course deposits, customer withholding, internal transfers, refunds, shareholder funding, and partner collections require different treatment.
4. Generate reports continuously from a controlled ledger and tax subledger. Finance should review exceptions and authorize a finished package instead of reconstructing it in Excel.
5. Build a verified **Coretax XML/export and receipt-import** path first. Treat direct submission through a provider/API as conditional on officially supported access and verified coverage. A generated file is not a submitted return; a bank debit is not a filing receipt.
6. Keep accounting recognition and fiscal timing separately configurable. An advance for future classes may remain deferred revenue in management accounts while the tax analysis follows the applicable tax rules.
7. An education business is not universally VAT-exempt. Determine treatment per legal supplier, licence, product, contract, and bundle.

## 2. Verified rules and their implications

### PPh23: two directions, not one report

PPh23 generally covers specified payments to domestic taxpayers/permanent establishments: 2% for relevant service and non-land/building rental categories; 15% for specified investment/royalty/prize categories, subject to exclusions and current rules. The timing analysis must consider payment, availability for payment, and due dates. The DJP overview is useful for categories but contains historical payment-deadline references, so do not copy its deadline table into software. [DJP PPh23/26 overview](https://www.pajak.go.id/index.php/id/pph-pasal-2326)

PMK141/2015 expressly lists training/courses, certification, and testing among service categories and specifies a gross basis excluding VAT with defined exclusions and evidence requirements. This makes a customer-withholding register relevant to OSEE's corporate sales. Application still depends on the actual supplier, payer, income, and applicable exemption/final-regime documentation. [PMK141/2015 official text](https://jdih.kemenkeu.go.id/api/download/fulltext/2015/141~PMK.03~2015Per.pdf)

**Design:** Maintain `tax_direction = withheld_by_us | withheld_from_us`. The former is normally a payable to the state; the latter may be a recoverable income-tax credit if valid and applicable. Never net these mechanically in the monthly PPh23 payable. Preserve gross invoice, VAT, withholding basis, rate, tax object code, net settlement, and certificate independently.

Illustrative entries only, assuming an eligible 2% service, a Rp10,000,000 base, no VAT for simplicity, and normal creditable treatment:

| Event | Debit | Credit |
|---|---|---|
| Supplier expense is recognized | Service expense Rp10,000,000 | Accounts payable Rp10,000,000 |
| Withholding is recognized at the legally applicable trigger | Accounts payable Rp200,000 | PPh23 payable Rp200,000 |
| Supplier receives net payment | Accounts payable Rp9,800,000 | Bank Rp9,800,000 |
| State payment is confirmed | PPh23 payable Rp200,000 | Bank/tax payment clearing Rp200,000 |
| Customer invoice is issued | Accounts receivable Rp10,000,000 | Revenue/deferred revenue Rp10,000,000 |
| Customer pays net with valid withholding evidence | Bank Rp9,800,000 + PPh23 tax credit Rp200,000 | Accounts receivable Rp10,000,000 |

The customer example is not an instruction to classify every course/test receipt this way. Missing or disputed customer certificates should use a configurable withholding-pending/receivable workflow until resolved, not silently erase receivables.

### PP23/PP55 turnover-final tax: historic and current rules

PP55 repealed PP23 while retaining transitional calculations. Its original maximum durations were seven tax years for individuals, four for specified entities including CVs/cooperatives, and three for ordinary PTs. Registration and prior PP23 usage determine the clock; the clock did not restart with PP55. Its monthly tax base is qualifying gross business turnover. The Rp500 million annual untaxed band belongs to individual taxpayers, not this PT. Final tax may be paid directly or withheld by qualifying counterparties. [PP55/2022, Articles 59–63, 69 and closing provisions](https://www.pajak.go.id/index.php/id/peraturan/penyesuaian-pengaturan-di-bidang-pajak-penghasilan)

**PP20/2026, effective 22 April 2026, changes eligibility.** The 0.5% rate remains; ordinary PTs are outside the new general entity list. Article II(1)(e) nevertheless permits ordinary PTs/CVs/firms/BUMDes whose PP55 final-tax period is unfinished to continue until that existing period ends, while meeting the former criteria. PP20 deletes Article59 and introduces other entity/aggregation rules plus special transitions; do not apply the legacy durations universally to future eligibility. The new general turnover ceiling remains Rp4.8 billion, with specified exclusions and aggregation rules. [PP20/2026 legislative text, especially amended Articles56–58 and Article II](https://pajak.go.id/id/peraturan/perubahan-atas-peraturan-pemerintah-nomor-55-tahun-2022-tentang-penyesuaian-pengaturan-di)

**Design:** Store an approved `TaxRegimeDetermination` per entity/year with legal form, NPWP registration date, prior regime/expiry, turnover evidence, normal-regime election, certificates, legal sources, decision reason, reviewer, and version. Result states should include `eligible`, `ineligible`, and `needs_review`. The UI can retain the familiar label “Pajak omzet (PP23/PP55)” while showing the applicable current legal basis underneath.

For this confirmed ordinary PT, the release gate must document its legal form and establish initial registration date, eligible years already consumed, election history, relevant prior and current gross turnover, and validity of supporting certificates. Do not guess final-tax eligibility from turnover alone.

Implement separate amounts for qualifying final-tax turnover, nonfinal revenue, separately final income, non-taxable income, and non-revenue cash. Store incoming final-withholding certificates and reconcile them to the same income so a self-payment is not generated twice. Do not treat a PPh23 credit as automatically settling final turnover tax.

### Individuals teaching for the company

PMK168/2023 includes individual instructors/trainers among nonemployee recipients and provides specific PPh21 rules, including a 50% gross basis for nonemployees under its conditions. Employment status and tax residence change the analysis; a foreign passport alone does not establish PPh26 treatment. [PMK168/2023 official text](https://pajak.go.id/id/peraturan/petunjuk-pelaksanaan-pemotongan-pajak-atas-penghasilan-sehubungan-dengan-pekerjaan-jasa-1)

**Design:** Classify each payee as employee, nonemployee individual, domestic entity, or nonresident with reviewed tax residence; retain agreements and evidence. Tutor compensation must not inherit a universal PPh23 default just because the expense account says “teaching services.” Use an existing payroll calculation source initially if payroll implementation is outside phase one, but import and reconcile its journals, tax liabilities, and certificates. Foreign vendor/tutor arrangements need a treaty/residency-document decision path where relevant.

### Monthly deadlines and calendar versioning

PMK81/2024 Article94 generally moves covered monthly PPh payments, including PPh23, to the 15th of the following month. Article171 requires covered PPh reporting within 20 days after the tax month, through the appropriate PPh21/26 or Unifikasi return. Articles100/173 address applicable holidays. Articles169–170 set corporate annual filing at four months after year-end; Articles174–175 provide an extension-notification route with supporting requirements. [PMK81/2024 legislative text](https://www.pajak.go.id/id/peraturan/ketentuan-perpajakan-dalam-rangka-pelaksanaan-sistem-inti-administrasi-perpajakan)

JDIH lists PMK1/2026 as the fourth amendment of PMK81. Its text was checked; the amendment addresses other provisions rather than replacing the above general PPh23 deadlines. Use an amendment-aware legal register, not a single unversioned “PMK81” flag. [JDIH PMK81 status](https://jdih.kemenkeu.go.id/dok/pmk-81-tahun-2024/files), [PMK1/2026 text](https://www.pajak.go.id/id/peraturan/perubahan-keempat-atas-peraturan-menteri-keuangan-nomor-81-tahun-2024-tentang-ketentuan)

KEP71/PJ/2026 provides specific sanction relief for 2025 corporate returns and related payments made within one month after their deadline. The DJP announcement explicitly retains the four-month due date. Model this as a period-specific relief policy, not a permanent May filing date. [DJP official KEP71 announcement](https://pajak.go.id/id/pengumuman/kebijakan-penghapusan-sanksi-administratif-atas-keterlambatan-pembayaran-dan-pelaporan-1)

**Design:** Calendar records need legal due date, adjusted due date where legally allowed, internal target, entity fiscal year, tax type, holiday version, special-relief window, and source. Show internal targets a few working days earlier to allow resolution. An overdue liability and eligibility for sanction relief are separate statuses.

### Annual corporate return requires a complete fiscal bridge

Current Coretax guidance starts with the main form, uses answers to activate annexes, and requires financial statements and supporting withholding evidence. Sector selection changes the reconciliation schedule. The guide includes tax calculation, reductions/credits, less/more paid, PPh25 instalments, related-party statements and depreciation/amortization schedules. Authorized individuals prepare the entity return through the designated account/role flow. [Coretaxpedia corporate return workflow](https://www.pajak.go.id/coretaxpedia/lapor-spt-tahunan-badan)

The general corporate rate is 22% from tax year 2022 under the amended income-tax provisions. Pasal31E can reduce the applicable rate for the eligible income portion of qualifying domestic entities; it is not a universal 11% of revenue. [UU HPP official text](https://www.pajak.go.id/id/peraturan/harmonisasi-peraturan-perpajakan), [DJP corporate calculation guidance](https://stats.pajak.go.id/index.php/id/mekanisme-penghitungan-pajak-penghasilan-badan)

**Design:** The annual pack must reconcile:

- Closed commercial trial balance and financial statements.
- Commercial profit to fiscal income through positive/negative adjustments and separately final/non-taxable income.
- Book depreciation to fiscal depreciation/amortization and asset disposals.
- Eligible loss carryforwards by originating year and expiration.
- Verified PPh22/23/24 credits as applicable, PPh25 paid, and PPh29 settlement; maintain final tax in its own schedule.
- Shareholders, capital movements, directors, related parties, loans, receivables, and supporting disclosures.
- Annex requirements for the selected sector and return revision.

Every output cell should drill down to its source account/journal/document or signed tax adjustment. Annual preparation should expose an “evidence missing” list throughout the year.

### Education VAT and test/partner revenue

PP49/2022 Article16 provides exemption for qualifying formal/nonformal education supplied by appropriately licensed educational units. Article16(6) excludes education inseparably bundled with other goods/services. The regulation supports an exemption classification, not a rule that every receipt of an education-branded PT is exempt. [PP49/2022 official text](https://www.pajak.go.id/id/peraturan/pajak-pertambahan-nilai-dibebaskan-dan-pajak-pertambahan-nilai-atau-pajak-pertambahan)

**Design inference for OSEE:** Separate course tuition, TOEFL/test administration, vouchers, certification, registration, books/materials, platform access, shipping, partner commission and corporate training. Capture licence holder, actual legal supplier, delivery obligation, invoice issuer and bundle components. Obtain an approved treatment matrix for each contract/SKU. For partner collections, record whether OSEE is principal or collection agent; this can alter gross-versus-net commercial revenue and the turnover analysis. Do not infer tax turnover solely from the commercial net revenue field. Capture underlying gross consideration and agency liabilities for review. Current non-PKP status is user-confirmed: apply the dated amendment above to current invoicing and supplier VAT. Historical effective dates, documentary status and any future change still require review.

## 3. Required tax data model

This is a recommended design, not a prescribed DJP database schema.

| Record | Minimum fields |
|---|---|
| EntityTaxProfile | Entity/NPWP/NITKU, exact legal form, fiscal year, PKP and effective dates, licences, tax regime determination, registered tax obligations, authorized signers |
| CounterpartyTaxProfile | Legal identity, entity/individual status, NPWP/NIK as text, tax residence, treaty/SKD evidence when relevant, certificate type/number/effective dates, withholding status |
| TaxTreatment | SKU/expense/contract mapping, tax article, object code, direction, basis method, VAT status, rate, gross-up/net agreement, exceptions, legal source, effective dates, reviewer |
| TaxEvent | Source document + line, entity, branch, tax trigger date and reason, accounting date, tax period, base, currency/exchange rate source, tax amount, rounding version, rule version, overrides and reason |
| WithholdingCertificate | Incoming/outgoing, official number/status, issuer/recipient, period, base/tax, source invoice allocation, correction/cancellation links, official document hash |
| TaxObligation | Type/period, original and revised amount, due dates, paid/allocated/remaining, filing package links, relief policy |
| TaxPayment | Billing code, entity/NPWP, tax code, period, amount, bank reference, BPN/NTPN evidence or valid deposit allocation, state reconciliation status |
| TaxReturnPackage | Entity/type/period/revision, frozen ledger watermark, calculation version, XML schema/template version, files/hashes, approval, submission status, official acceptance receipt |
| FiscalAdjustment | Year/period, affected journal/account, difference type, amount, explanation, evidence, tax-return mapping, approval |

NPWP/NIK/NITKU and all external reference codes must be strings. Use decimal money, explicit currency, legally correct tax rounding, and entity-period scoping. Never store credentials, private keys, or authorization codes in ordinary application tables or document exports.

## 4. Coretax integration boundaries

DJP publishes XML templates and Excel converters for bulk imports, including withholding categories and annual annexes. Current annual guidance also provides an XML creation tutorial. These are evidence for a supported file workflow; they do not prove a public direct API is available to this ERP. [DJP XML template catalogue](https://stats.pajak.go.id/index.php/en/node/112031), [DJP annual return guidance and XML tutorial](https://pajak.go.id/lapor-tahunan), [official Unifikasi manual](https://www.pajak.go.id/sites/default/files/2025-01/Buku%20Manual%20Coretax%202024%20-%20Seri%20SPT%20Masa%20Unifikasi.pdf)

Recommended adapter contract:

1. `prepare`: freeze input set; calculate; validate identities, object codes and totals; produce a human-readable summary.
2. `export`: produce the exact supported schema version, cover sheet, financial PDF/annexes and manifest; preserve hashes.
3. `validate_external`: retain Coretax/PJAP validation errors against source records.
4. `approve_and_submit`: authorized person or supported provider submits/signs through its permitted workflow. The ERP must not represent an unsubmitted export as filed.
5. `record_billing`: obtain/store official billing identifiers and associate the correct obligation.
6. `record_payment`: reconcile bank settlement and government payment evidence. Support tax deposit allocation explicitly.
7. `record_acceptance`: store official BPE/receipt, entity, tax period, revision, acceptance timestamp and document hash.
8. `amend`: create a new package and correction chain; preserve earlier official filings.

A provider shortlist should be evaluated later for actual live Coretax coverage of BPPU, incoming credits, annual corporate service-sector annexes, billing, status, signatures, amendments, receipt retrieval, pricing and data access. “Supports e-filing” alone is insufficient. Do not rely on browser scraping with stored Coretax passwords as the production integration.

## 5. Nontechnical operating workflow

Suggested main menus: **Beranda**, **Uang Masuk**, **Tagihan & Biaya**, **Cocokkan Bank**, **Pajak**, **Tutup Bulan**, **Laporan**.

The tax page should say “Siap diperiksa”, “Butuh dokumen”, “Menunggu persetujuan”, “Siap dibayar”, “Pembayaran perlu dicocokkan”, or “Sudah dilaporkan”, with one next action. Keep technical field names in detail views. Explain a rule in a short sentence and link its evidence; do not make finance select a raw tax article on every familiar recurring payment.

Monthly process:

1. Import authoritative bank movements and sales/expense documents.
2. Match settlements to invoices, preserve split/partial/withholding cases.
3. Review new payees, unrecognized deposits, uncertain tax treatments, missing certificates and duplicate documents.
4. Preview financial reports, PPh23/other withholding registers and turnover-final schedule where eligible.
5. Approve the month and tax packages; submit via verified channel; authorize tax payment separately.
6. Reconcile official receipts and payments; lock the month when checks complete.

The month-close checklist should distinguish accounting close, bank completeness, tax preparation, tax payment and tax filing. Missing government receipt must remain visible even if the financial books have been closed.

## 6. Safety and correctness invariants for implementation

- No posted journal can be edited in place; corrections use reversals/new entries with a reason and approver.
- No duplicate obligation when a bank record, invoice, or certificate is reimported.
- Tax timing is recorded independently from settlement and course delivery dates.
- A supplier payment does not erase a withholding payable; a customer withholding does not reduce reported gross revenue by accident.
- Bank statement completeness is monitored by account, date range, balance continuity and source cursor/file hash.
- Incoming withholding without verified applicable evidence cannot silently become an annual tax credit.
- Tax return totals reconcile to the tax subledger and frozen source set.
- `filed` requires official acceptance evidence; `paid` requires validated payment/allocation evidence.
- Corrections to a previously filed period require explicit amendment handling and a comparison report.
- Tax rule changes have effective dates, reviewer approval, examples and regression checks. Do not let an AI model choose a tax regime or rate without a deterministic approved rule.

## 7. Suggested acceptance scenarios

1. Eligible 2% supplier service with partial payment, where the tax trigger precedes full payment.
2. Corporate training invoice paid net of PPh23; certificate arrives a month later.
3. Same bank file uploaded twice, and the same movements later arrive through an API.
4. Payment combines three student invoices and includes an unidentified excess.
5. Tuition advance, course completion in a later month, and partial refund.
6. Ordinary PT with exhausted PP55 eligibility; low turnover must not re-enable 0.5%.
7. Ordinary PT with documented unfinished eligibility on 22 April 2026; correct grandfather expiry.
8. Final-tax income already withheld by a customer; prevent duplicate self-payment.
9. Individual freelance tutor routed to reviewed PPh21 logic, not entity PPh23.
10. Bundled course/test/material transaction flagged for the approved VAT treatment matrix.
11. Closed December ledger corrected after filing; amendment retains original return and receipt.
12. Coretax import rejection or unavailable provider; retain a reviewable export and actionable exception.
13. Bank tax debit recorded but NTPN/BPN or deposit allocation missing; show evidence pending.
14. Annual book-to-fiscal bridge, tax credits and tax payable tie exactly to their supporting schedules.

## 8. Outstanding company-specific facts

The user has confirmed an ordinary PT, current non-PKP status and annual turnover below Rp4.8 billion, but has not specified the exact turnover amount/year or historical status effective dates. Before configuring live calculations, obtain supporting legal-form and current-status documents, NPWP registration date/history, fiscal year, turnover by relevant tax year and regime/election history, prior returns and final-tax certificates, historical PKP/non-PKP dates, education licences, bank-account ownership, branch/NITKU structure, partner agreements, invoice/tax responsibility, staff/tutor residency and contracts, opening asset/fiscal schedules, tax losses and credit carryforwards. For the confirmed ITP wholesale flow, also obtain the current IIEF final invoice and tax breakdown behind the approximate Rp450,000 “after tax” price, supplier tax documentation, reseller price inclusions, and the contractual delivery/cancellation/refund obligations. Turnover below the threshold alone does not establish eligibility for 0.5% final tax. These facts are configuration and migration dependencies; they do not prevent drafting the ERP architecture.

## 9. Source reliability notes

Primary legislative text and official technical manuals were preferred. Some older DJP overview pages still display the pre-2025 10th-day PPh23 payment deadline; PMK81 Article94 is the controlling source used here. PP55's old Article59 must be read together with PP20, and a 2026 press headline about an “extension” must be distinguished from the formal sanction-relief announcement. Source existence does not establish this company's eligibility. Official template/schema versions should be rechecked during implementation and before each release.

## 10. Controls for an owner and finance team without tax knowledge

The user has now clarified that both the owner and finance team have no Indonesian tax expertise. The architecture must therefore supply a tax-review service and a controlled operating process, not merely expose tax settings and ask users to choose correctly. The following are proposed product and service controls, not additional statutory requirements.

### Accountable roles and the decisions each person makes

| Role | May decide or approve | Should not be expected to decide |
|---|---|---|
| Owner / business approver | Which entity operates, whether a transaction happened, commercial contracts, budget, payment release and designated signatories | Tax eligibility, object codes, statutory interpretation or XML correctness |
| Finance operator | Document completeness, payer/order match, amounts/dates against evidence, delivery and cancellation facts, follow-up on missing documents | Whether an unfamiliar service is PPh23/21 or whether an invoice's VAT is recoverable |
| Qualified Indonesian tax-review owner | Documented tax profile, transaction treatment matrix, legal applicability, rule activation, tax exceptions and readiness of the filing calculation | Inventing missing business facts or approving bank payments as an incidental consequence of reviewing tax |
| Controller / accounting reviewer | Reporting basis, commercial recognition, accruals, opening books and book-to-fiscal reconciliation | Treating portal acceptance as a substitute for accounting or tax review |
| Engineering / product operator | Implementing approved policy, schema mappings, calculations, access controls, reproducible validation and recovery | Choosing the company's legal tax treatment independently |
| Authorized tax signer | Submission through the permitted channel, using the reviewer's approved package | Reperforming the specialist's entire tax analysis or relying on an unexplained green status |

The tax-review owner can initially be a contracted adviser with demonstrated Indonesian corporate-tax and Coretax experience. Assign a named primary and substitute, a review scope, a response target and escalation route before live tax activation. Record the reviewer's rationale in the ERP so replacing the adviser does not erase institutional knowledge. Verify competence and authorization during appointment; this document does not designate a provider or imply a new mandatory professional appointment under law.

### Document-led onboarding

Present an upload/checklist flow for existing legal registration documents, tax registration and PKP records if any, prior returns and certificates, turnover by year, supplier/reseller contracts, representative invoices and available payroll records. The operator confirms observable facts such as “Is this the latest document?” and “Does this account belong to this company?” The tax reviewer resolves legal implications and missing-record recovery. Do not ask “Are you eligible under Article X?” as the primary onboarding question.

Each extracted field has a document/page reference and separate statuses: `observed`, `business_fact_confirmed`, `tax_treatment_approved`. OCR confidence must never activate a tax rule. An invoice can have correctly extracted amounts while its tax interpretation remains unresolved. Reuse established recurring classifications; route new products, foreign suppliers, changed contracts and conflicting documents to the reviewer with plain-language explanations.

### Concrete release and operational assurance gates

| Gate | Evidence required to pass | Accountable approval and failure behavior |
|---|---|---|
| G0: Tax service is owned | Named reviewer/substitute, service scope, turnaround targets, escalation before deadlines, role/access setup | Owner appoints the service owner. Unowned tax judgments remain unactivated; document intake can proceed. |
| G1: Company profile established | Documentary legal form, registration/history, fiscal year, relevant annual turnover, PKP status and supported determination for each applicable regime | Tax-review owner signs a dated determination; missing data produces a request for a document, not a guessed zero or rate. |
| G2: Product/counterparty treatment mapped | ITP/iBT/course and supplier/reseller matrix; factual contract obligations; observed invoice components; approved tax basis/timing/evidence; exceptions | Tax and accounting reviewers approve their respective policies. Unknown products or changed facts enter review before tax output is finalized. |
| G3: Opening evidence reconciled | Approved opening books/subledgers, bank balances, final-tax/credit history, supplier/reseller balances, deferred obligations and fiscal schedules | Controller and tax reviewer reconcile relevant balances. A recap alone cannot substitute for books, receipts or tax evidence. |
| G4: Rule and schema release verified | Primary legal source/version, approved interpretation, independent expected-result examples, deterministic calculations, correct rounding, supported form mapping, validation results | Reviewer approves expected outcomes; engineer demonstrates them. No unresolved blocking defects or unexplained differences. A generated test that repeats the production formula is insufficient independent evidence. |
| G5: Shadow operation completed | Proposed initial target: two consecutive monthly cycles reconciled to independently reviewed workpapers; a completed-year rehearsal if reliable history exists; current annual-form rehearsal separately | Controller/tax-review owner accept reconciliations and document resolved historical errors. Previously filed returns are comparison evidence, not unquestionable calculation truth. |
| G6: Each live filing package ready | Frozen source snapshot, complete applicable identity/form fields, resolved blocking exceptions, reconciled tax base/liability/credits, reviewed calculation and output, payment/signature responsibilities | Tax-review owner certifies readiness. Owner receives an understandable summary and controls cash/authorized submission; owner is not asked to validate statutory formulas. |
| G7: Completion confirmed | Correct-entity/period official filing receipt and applicable official payment/allocation evidence, reconciled to the approved package; clear difference or rejection handling | Finance collects/imports evidence; mismatches return to the reviewer. Portal validation/acceptance is not proof that every substantive tax interpretation is correct. |
| G8: Changed rules or facts safely released | Impact analysis, affected periods/transactions, new expected-result cases, reviewer approval, migration/amendment assessment and rollback plan | Freeze only affected unapproved calculations where practical; continue safe intake and unaffected work. Preserve accepted packages and posted facts. |

These gates provide documented assurance and a way to detect/correct errors; they do not promise absolute tax correctness. Distinguish verified structural validity, arithmetic consistency, independent tax review, and official receipt as separate assurance dimensions.

### May 2026 evidence-specific checks

The lead agent's main architecture section 6.10 records a scanned supplier invoice dated 4 May 2026 for 2 May TOEFL ITP HOME EDITION Level 1: 50 × Rp400,000 = Rp20,000,000; shown VAT Rp2,200,000; total Rp22,200,000; calculated gross Rp444,000 per test. This agent read that recorded observation rather than independently inspecting the source image.

Use those values as an extraction/arithmetic example with document provenance. Keep `supplier_stated_vat` separate from the reviewer's approved treatment and from input VAT accepted as recoverable. Never infer the current legal VAT rule, the supplier's withholding treatment, or OSEE's output tax from this single amount. Its invoice date, service date and later payment dates remain distinct.

The reported matching recap quantity is a candidate match. Require actual order allocations, participant/delivery evidence, relevant invoices and bank/payment evidence for their respective reconciliations. A header-price extension or recap “profit” cannot populate the general ledger, cash collected or tax paid as an established fact. Distinguish ITP product family, Home Edition delivery mode and Level 1; a label containing “online” must not silently map to iBT.

### Plain-language exception handling

Show what happened, the amount affected, the next action, who handles it and the deadline. Examples:

- “We found the supplier invoice. We still need its tax document before your tax reviewer can confirm this VAT treatment.”
- “This customer paid less than the order total and reported tax withholding. Upload their withholding document; the tax reviewer will confirm how to record it.”
- “The tax reviewer needs your original company tax-registration document before this turnover-tax calculation can be enabled.”
- “The return file is ready, but the tax office has not yet confirmed receipt.”

Finance receives a document task; the reviewer receives the legal decision. A support action should attach the current evidence and calculation history automatically rather than require the user to restate technical details. Display estimated liabilities distinctly from approved payable amounts and filed figures.

## 11. Later AI-assisted lawful tax and cost planning

The user's intended later AI tax-planning capability should analyze documented options and costs in a separate scenario workspace. This is a proposed system design. It must not make tax eligibility, filing or cash-transfer decisions by itself, and the owner should not be expected to evaluate the legal validity of AI-generated advice.

### Planning inputs and outputs

Use a versioned read-only snapshot of approved company tax status and regime history, reconciled accounts, product margins, tax treatments, unconsumed entitlements, asset schedules, employee/vendor categories, contracts, valid certificates, historical tax payments and forecast commercial activity. Distinguish recorded facts, forecasts, missing evidence and hypothetical assumptions. Do not expose full bank credentials, signing secrets, or unnecessary identity and student data to the model.

Every scenario should contain:

1. Baseline snapshot and date, current approved policy and unresolved assumptions.
2. The real commercial change or documented facility being evaluated and whether it is currently legally available to this entity.
3. Current primary legal references, their effective dates and an explicit statement when eligibility still needs specialist review.
4. A deterministic calculation using the approved calculation engine, showing tax by type, accounting profit, cash timing and compliance/implementation cost separately.
5. Comparisons over an appropriate period, including transition effects, one-off cost, sensitivity to volume/pricing, and evidence requirements.
6. Reviewer conclusion, decision owner, implementation steps, effective date and any continuing obligations.

A smaller current-month cash payment is not automatically a tax saving; it may be a timing difference, credit awaiting recovery, shifted liability or extra compliance cost. Show those differences explicitly. Model commercial price changes and recoverable/nonrecoverable supplier tax separately so a nominal gross spread is not presented as net profit.

Candidate analyses include completeness of documented allowable costs, treatment of supplier tax and valid credits, applicable facilities, true gross/net pricing agreements, procurement/batch economics, and timing of real planned expenditure. If comparing tax regimes, label an unavailable or non-elective regime as a counterfactual, not a recommended selectable option. Any proposal depending on a new commercial structure requires substantive contracts and an independent applicability review before it becomes an actionable plan.

### Separation from operational systems

- AI may explain, identify missing evidence, draft a scenario and call a read-only calculation service. It cannot alter source transactions, identity records, account mappings, tax rules, return packages or bank instructions.
- Tax-document retrieval is source-grounded and versioned. Retrieved content and invoice text are data, not executable instructions. Missing or conflicting current authority results in a reviewer task, not an invented legal answer.
- An AI confidence score is not a release gate. Results remain labelled `Scenario—requires tax review` until the qualified reviewer approves their legal assumptions and the deterministic computation passes its checks.
- Reviewer approval of legality is separate from owner approval of business economics. The approved output becomes a controlled change request; it still needs affected-policy tests and deployment approval before entering production.
- An approved prospective change does not rewrite historical books or filed returns. Any past correction uses the existing amendment workflow with preserved evidence.
- Store prompts relevant to the decision, source IDs, model/version, assumptions, calculation engine/version, reviewer edits and final decision, subject to the appropriate access and retention policy.

## 12. Proposed tax-review service cadence and incident response

This cadence is an operating recommendation, not a statement of statutory frequency.

| Frequency / trigger | Automated work | Human owner and expected outcome |
|---|---|---|
| Daily | Identify missing/duplicate documents, bank gaps, new tax classifications, certificate mismatches and approaching internal deadlines | Finance resolves observable facts; tax service receives decision tasks with context. |
| Weekly | Consolidate unresolved tax exceptions, ageing, changes in counterparties/products/contracts and flagged official-source changes | Tax-review owner clears decisions, records rationale and escalates missing information. Initial service target: triage within one working day and review within two working days where evidence is sufficient. |
| Before each monthly filing/payment approval | Reconcile bases, obligations, evidence and output package against a fixed snapshot | Tax reviewer approves the monthly calculation and exceptions; designated owner/approver authorizes cash and signer completes supported submission. Set internal cutoffs comfortably before applicable legal deadlines. |
| Monthly after receipt | Compare approved package, portal outcome and official payment/allocation evidence; investigate discrepancies | Tax reviewer closes compliance exceptions; controller assesses any accounting correction. |
| Quarterly | Reassess profile facts, turnover trajectory, price/tax matrices, certificate expiry, recurrent manual overrides and annual readiness | Tax-review owner confirms continuing applicability and needed rule updates; owner supplies changed business facts. |
| Before year-end and annual filing | Produce fiscal bridge, missing-credit/disclosure list, regime-transition alerts and current-form coverage check | Tax reviewer and controller plan the annual close and approve the completed return package. The first live annual cycle receives full specialist review. |
| Immediately on a material rule/schema/company change | Flag affected assessments, expiry/transition dates and output mappings | Reviewer assesses effect before activating changes; engineering releases validated rules and maps. Notification alone does not change production policy. |

If a calculation error or filing rejection is found, preserve the affected inputs and output version, stop affected unapproved submissions, determine the population of impacted periods and transactions, and route legal/correction decisions to the tax-review owner. Give the owner a short factual impact summary, available cash implications and required business decisions. The specialist directs correction or amendment; the engineer fixes and verifies the software cause. Continue safe document collection and unaffected operations. A portal outage or a missed internal target triggers escalation against the real deadline; relief must not be assumed.

Track outcomes rather than an undefined accuracy percentage: unresolved blocking exceptions, unexplained reconciliation differences, evidence coverage, missed deadlines, rejected submissions, post-filing amendments by cause, rule-release regressions, reviewer turnaround and repeat manual corrections. Use those results to determine whether recurring cases can receive less manual inspection; do not remove accountable review merely because several calculations succeeded.
