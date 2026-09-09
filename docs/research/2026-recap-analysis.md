# Review of the supplied 2026 TOEFL ITP recap

Analysis date: 7 September 2026. Source: `C:\Users\user\Downloads\2026 - TOEFL ITP RECAP.pdf`, 10 pages. This note preserves the source and records observations, calculations and architecture implications. Only this note was authored as a final deliverable; no source figures were corrected.

Source SHA-256: `3633074E8F4F5243D7C276689EA6166DA43A84DC6F14C7EE8EC01B5441BE7A1C`.

## 1. Method and evidence limits

The PDF was rendered with Poppler and visually reviewed, including enlarged table regions for May 2, the January dates, the August BRIGHTEN column and the January-March lower monetary rows. Dense columns were assigned using pdfplumber table-cell/character coordinates, not concatenated plain-text order. PDF page numbers below are one-based; coordinates are PDF points measured from the top-left. Extracted table cells and arithmetic were checked against the rendered pages.

The document is a printed ITP operating/sales recap. It is not a general ledger, bank statement, customer invoice register, accepted tax return, tax payment receipt or complete supplier-cost register. It exposes printed values and visual formatting, not the original spreadsheet formulas, hidden sheets, edits, effective price contracts or meaning of row colors. Blank cells cannot automatically become verified zeroes. Participant counts are test-taker occurrences as presented; the PDF cannot establish unique people, paid orders or completed tests.

User-confirmed current context: mitra buy wholesale and independently resell; the current discussed Rp500,000-Rp530,000 selling range and approximately Rp450,000 supplier amount are ITP-only; mitra prepay per order before the test. The printed historical/header amounts below should be reconciled with that current setup, not forced into it. Neither this PDF nor the indicative cost establishes PKP status, VAT recoverability, PPh treatment, taxable turnover or current final-tax eligibility.

The shared Google report was not read. This analysis uses the uploaded PDF, not inaccessible source-workbook contents.

## 2. Page inventory and displayed monthly totals

| PDF page | Month represented | Displayed participant total | Explicit format split | Displayed monthly monetary total |
|---|---|---:|---|---:|
| 9 | January 2026 context, with three conflicting 2025 dates | 615 | No separate format summary shown | Rp346,530,000 |
| 8 | February 2026 | 1,343 | No separate format summary shown | Rp728,560,000 |
| 7 | March 2026 | 580 | No separate format summary shown | Rp318,750,000 |
| 6 | April 2026 | 921 | No separate format summary shown | Rp506,920,000 |
| 5 | May 2026 | 961 | Printed `ONLINR` 950; `OFFLINE` 11 | Rp530,300,000 |
| 4 | June 2026 | 817 | `ONLINE` 765; `OFFLINE` 52 | Rp444,670,000 |
| 3 | July 2026 | 928 | `ONLINE` 918; `PAPER` 10 | Rp500,970,000 |
| 2 | August 2026 | 342 | `ONLINE` 332; `PAPER` 10 | Rp180,830,000 |
| 1 | September 2026, partially populated | 13 | `ONLINE` 13; `PAPER` 0 | Rp6,950,000 |
| 10 | Annual-style partner summary, populated only January-June | 5,237 | No format split | No comparable annual monetary total shown |

The nine displayed monthly participant totals add to **6,520**. Their displayed monthly monetary totals add to **Rp3,564,480,000**. These are sums of the export's printed values before resolving the exceptions below, not verified accounting revenue, bank collections or taxable turnover.

January-June displayed totals add to 5,237 participants and Rp2,875,730,000. July-September add another 1,283 participants and Rp688,750,000. September shows only the September 2 row populated, with 3 OSEE and 10 TITC; later date rows are largely blank. Do not interpret the month as complete or blank future dates as final zero activity. No October-December detail pages are included.

Each of the nine monthly monetary totals exactly equals the sum of the **printed partner monetary subtotals** on that page. This does not resolve differences between a detail quantity, a printed column count, a header price and the corresponding monetary subtotal. Arithmetic agreement at one level is not completeness at every level.

The sum of visible dated detail counts agrees with the printed monthly total for January-July and September. August has the specific exception in section 4.1. This check is against the displayed PDF, not against bookings, payment records or actual test delivery.

## 3. Annual-summary coverage is incomplete

Page 10 contains named channel/partner rows with January-June values, followed by blank July-December partner cells and zeroes in those monthly footer positions. Its printed total of 5,237 exactly matches January-June, even though pages 1-3 contain July, August and September data.

Examples: page 10 OSEE shows January-June 250, 403, 175, 287, 308, 206, totaling 1,629. TITC shows 246, 583, 246, 424, 447, 420, totaling 2,366. Page 3 separately reports July OSEE 234 and TITC 571, which do not appear in those annual-summary totals.

This establishes incomplete summary coverage within the supplied export. It does not establish a broken formula, unreported tax, or actual omitted transactions in another system. The original workbook may have a different update/export state, which is not visible here.

Architecture consequence: every rollup must declare `covered_periods`, source cutoff, source-run completeness and unresolved exceptions. Missing-month data must display as `not imported/not confirmed`, not a zero that resembles a complete year. Annual export must reconcile to included monthly snapshots and visibly identify the July-September 1,283-count difference until corrected or explained.

## 4. Specific detail and price reconciliation exceptions

### 4.1 August BRIGHTEN count does not match its dated detail

Page 2, BRIGHTEN ENGLISH column, header Rp530,000:

- `13 Agu 26`: 2 participants.
- `26 Agu 26`: 1 participant.
- Printed bottom column count: 2.
- Printed monetary subtotal: Rp1,060,000, consistent with 2 x Rp530,000.

The two visible detail cells add to **3**, not 2. Total visible August dated quantities therefore add to **343**, while the printed month total is 342. If both detail cells are valid billable occurrences at the displayed price, the extension would be Rp1,590,000, or Rp530,000 above the printed subtotal. This is a conditional arithmetic check, not an instruction to alter revenue.

Provenance: page 2 BRIGHTEN column x=425.32-442.37; detail text top approximately y=101.25 for August 13 and y=140.24 for August 26; bottom count y=147.32; subtotal y=161.50. The header, cells and bottom count were visually checked.

Required resolution: inspect the source booking/order and any cancellation, duplicate, date movement or price adjustment for those rows. Preserve both source and approved resolution; do not assume a formula-range error from the PDF.

### 4.2 Printed header-price extensions differ from monetary subtotals

| Page / month | Exact printed channel | Printed count | Header unit price | Count x header price | Printed subtotal | Printed minus extension |
|---|---|---:|---:|---:|---:|---:|
| 5 / May | KAMPUNG INGGRIS LC PARE | 11 | Rp500,000 | Rp5,500,000 | Rp5,830,000 | +Rp330,000 |
| 6 / April | KAMPUNG INGGRIS LC PARE | 10 | Rp500,000 | Rp5,000,000 | Rp5,300,000 | +Rp300,000 |
| 8 / February | A ONE | 10 | Rp520,000 | Rp5,200,000 | Rp5,300,000 | +Rp100,000 |
| 8 / February | KAMPUNG INGGRIS LC PARE | 20 | Rp500,000 | Rp10,000,000 | Rp10,600,000 | +Rp600,000 |
| 8 / February | LEDALERO | 10 | Rp570,000 | Rp5,700,000 | Rp5,300,000 | -Rp400,000 |

These are definite differences between printed fields. They could reflect stale headers, negotiated prices, product/format differences, additional charges, adjustments or an error; the PDF cannot choose the explanation. Do not automatically replace the printed subtotal with the header extension or combine these differences into a corrected tax base.

Provenance: page 5 LC PARE x=527.60-544.65, count row y=180.33 and subtotal y=196.24; page 6 LC PARE x=530.60-546.10, count y=192.16 and subtotal y=200.79. Page 8 count y=188.87 and subtotal y=197.80; A ONE x=465.13-480.23, LC PARE x=528.52-545.11, LEDALERO x=545.11-561.71. Header positions and bottom values were visually checked.

### 4.3 February CO ID has quantities without a printed sales subtotal

Page 8 `OSEE CO ID` has a printed total of **16** and visible dated counts, but its header contains no price and its monetary subtotal cell is blank. The February Rp728,560,000 footer adds the other displayed amounts; no CO ID amount appears in that sum.

Other months show CO ID information, but a later Rp530,000 header does not authorize applying that rate retroactively. A sensitivity calculation at Rp530,000 would be 16 x Rp530,000 = Rp8,480,000; this is **not** an established missing receivable or revenue amount and must not be imported as such. Retrieve accepted orders, pricing and any deliberate nonbillable/other-channel treatment.

Provenance: page 8 OSEE CO ID x=109.86-130.07, bottom count y=188.87 and blank monetary position y=197.80. January page 9 has 8 OSEE CO ID and a Rp4,240,000 subtotal, but no header price; that gives a January implied extension rate, not February contractual evidence.

### 4.4 January has three conflicting year labels

Page 9 is placed in the January 2026 recap context and its surrounding dates end `-26`, but three rows visibly read:

| Printed date | Nonblank channel counts | Total occurrences in row |
|---|---|---:|
| 13-Jan-25 | OSEE 6, TITC 10, NEO SPECTRA 1 | 17 |
| 14-Jan-25 | OSEE 11, TITC 4, OSEE CO ID 1, JAGO BAHASA 1 | 17 |
| 26-Jan-25 | OSEE 10, TITC 10, NEO SPECTRA 1, ENGLITE 1, UKAW 2 | 24 |

These three rows cover **58** printed occurrences. They look inconsistent with the surrounding year, but the actual service dates require confirmation. Keep both `original_date_text` and a proposed normalized date; flag the year mismatch before posting or annual attribution. Do not silently change all dates to 2026 based only on the file name.

Provenance: page 9 date-row text top approximately y=118.42, 123.82 and 177.79. The first two were checked in an enlarged rendering and all three in the full rendered table and coordinate extraction.

## 5. January-March lower contribution rows are not a verified profit statement

The lower monetary rows sum to the displayed footer amounts:

| Page / month | Sum of populated contribution cells | Displayed lower footer | Coverage caveat |
|---|---:|---:|---|
| 9 / January | Rp71,346,000 | Rp71,346,000 | Contribution cells blank for CO ID 8 and LC PARE 26 although their sales subtotals are printed |
| 8 / February | Rp141,900,000 | Rp141,900,000 | Contribution cells blank for CO ID 16, LC PARE 20 and LEDALERO 10 |
| 7 / March | Rp62,850,000 | `net bruto itp` Rp62,850,000 | All positive printed channel counts have contribution entries; no complete supplier/overhead/tax evidence is provided |

Where both a unit contribution and total contribution are printed, count x unit contribution agrees with the displayed contribution total. This arithmetic result does not make the contribution model complete or establish the accounting meaning of `net bruto`.

There are also reasons not to infer a single supplier cost from these figures. In January, OSEE's Rp650,000 header less Rp206,000 contribution implies Rp444,000, while GO ENGLISH's Rp510,000 header less Rp76,000 contribution implies Rp434,000. February's GO ENGLISH Rp510,000 header less Rp80,000 implies Rp430,000, whereas OSEE Rp650,000 less Rp210,000 implies Rp440,000. These are arithmetic implications of printed fields, not verified purchase prices. Dates, formats, charges, actual vendor invoices and cost allocation must explain them.

Do not label these rows audited profit, tax profit, after-tax profit or verified gross margin. Import them only as legacy comparison measures. Reconstruct accounting contribution from recognized revenue and supported allocated costs, showing estimates/accruals, actual bill variances and tax treatment separately.

## 6. May 2 row: exact channel mapping for the supplier-invoice cross-check

The page 5 row `2 Mei 2026` has exactly four populated channel cells:

| Channel | Count | Printed header price | Header-price extension |
|---|---:|---:|---:|
| OSEE | 20 | Rp650,000 | Rp13,000,000 |
| TITC | 26 | Rp500,000 | Rp13,000,000 |
| NEO SPECTRA | 3 | Rp500,000 | Rp1,500,000 |
| ENGLITE | 1 | Rp530,000 | Rp530,000 |
| Total | **50** | | **Rp28,030,000** |

The `1` belongs to **ENGLITE**, not UPY. Plain concatenated text loses that distinction. Row top is approximately y=72.45; header-cell x ranges are OSEE 67.35-84.42, TITC 84.42-101.44, NEO SPECTRA 169.63-186.68, ENGLITE 442.37-459.42. Those headers and cells were inspected in enlarged renderings.

The companion supplier-invoice review reports invoice `1300/IIEF/R139001/V/2026`, dated May 4 for a May 2 test, quantity 50, base Rp20,000,000 plus displayed VAT Rp2,200,000, total Rp22,200,000. That invoice was inspected by the coordinating reviewer, not independently re-extracted in this note. Its **quantity 50** agrees with the recap's May 2 count; the date and count make it a candidate matching obligation, not conclusive order/payment/delivery linkage.

Rp28,030,000 minus Rp22,200,000 is a raw **Rp5,830,000** difference. Do not call it profit without checking accepted selling prices, sales tax treatment, input-tax recoverability, delivery obligations, other costs and whether the supplier invoice covers exactly those orders. Do not replace the invoice's date-specific Rp444,000 gross per seat with the approximate current Rp450,000 quote, nor apply that May invoice cost to all 2026 recap rows.

## 7. Mapping the existing export into the ERP safely

The existing recap can become a familiar report generated from structured records. It should not become the transaction database itself.

| Observed export field | Proposed mapped field | Import treatment |
|---|---|---|
| PDF/file and page | `source_artifact_id`, hash, page number, coordinate/row locator | Immutable provenance; preserve the file |
| Apparent month and printed date | `source_period`, `original_date_text`, proposed `service_date`, validation status | Mixed-year dates require review; do not use the filename as sole date authority |
| Channel header | `source_channel_label`, reviewed canonical reseller/customer/channel ID | Resolve aliases explicitly; do not treat all columns as owned branches or all as external resellers |
| Header amount | `legacy_header_price`, currency, unknown tax-inclusion status | Comparison evidence, not an automatically active price version |
| Dated channel count | Aggregate order/session occurrence quantity | Can seed an operations migration staging table; cannot create named participants, invoices or cash receipts |
| Online/paper/offline subtotal | Legacy format-summary control | Map to exact product/format only after roster/contract confirmation; do not derive format solely from colors |
| Printed monthly channel count | Legacy quantity-control total | Reconcile to imported dated detail; block unexplained mismatches such as August BRIGHTEN |
| Channel monetary subtotal | Legacy reported-sales comparison amount | Reconcile to accepted invoices/order lines; do not treat as proved AR, collected cash or recognized revenue |
| Lower contribution row | Legacy management comparison measure | Preserve as legacy data; do not post as expense or profit |
| Month/annual footer | Coverage-aware reconciliation control | Compare consistent snapshots and missing periods; never double-count detail and totals |
| Colored rows/cells | Raw visual annotation if needed | Meaning unconfirmed; never map green to paid or blue to a tax status without documented evidence |

Names and column positions change between pages. Examples include `CO ID`, `CO.ID` and `OSEE CO ID`; `BYEC` and `BROFESSIONAL BY YEC`; `ENGLISH ACCELERATION` and abbreviated `ENG ACCELERATION`; `ENGLISH ONE` and `ENGLISHONE`; and `LI-LINGUA`/`LI LINGUA`. These are candidate aliases, not automatic proof of the same legal counterparty. UKAW, LI-LINGUA, RTEC and ECTC appear in different positions across months. Match by reviewed identity rather than fixed spreadsheet column number.

The export also shows header prices beyond the current general Rp500,000-Rp530,000 reseller range, including UPY Rp550,000, DISCOVERY ENGLISH Rp560,000, and LEDALERO Rp570,000, plus OSEE Rp650,000. GO ENGLISH's March header is Rp520,000 while other pages show Rp510,000; ECTC shifts from Rp520,000 to Rp530,000 in later headers. Treat these as observed historical/export values needing agreement/date/format reconciliation. Do not assume the user misstated current prices or overwrite special/historical agreements.

Suggested report replacement: date rows and familiar reseller columns generated from accepted order/session data, with optional direct OSEE channels. Add explicit `ordered`, `funded`, `provider-confirmed`, `delivered`, `cancelled`, and `unresolved` filters instead of unexplained colors. Generate a separate commercial-sales, cash-collection and recognized-revenue view so users can compare the familiar recap without confusing those concepts.

## 8. Acceptance checks driven by this actual sample

1. Import August detail with BRIGHTEN 2 + 1 and legacy total 2: show an unresolved one-count difference; do not silently discard the extra occurrence or auto-post a Rp530,000 adjustment.
2. Import January's three `-25` date rows: preserve printed dates, show the 58-occurrence period conflict and require a reviewed resolution before accounting attribution.
3. Load February CO ID count 16 with blank amount/price: quantity can be staged; no fabricated price, invoice, payment or income appears.
4. Load LC PARE and February A ONE/LEDALERO price differences: show printed amount, header extension and accepted-order evidence side by side; do not choose the largest or latest number automatically.
5. Generate annual recap after January-September imports: explicitly reconcile 6,520 displayed source counts with the 5,237 legacy annual summary; missing periods are visible, not zero-complete.
6. Match May 2's 20/26/3/1 channel quantities to the candidate provider invoice: quantity matches 50, but accounting approval still requires document/order and delivery linkage. Neither matching quantity nor row color marks payment settled.
7. Reimport the same PDF/extraction: source-artifact and row identities prevent duplicate staging or duplicate posted documents. A corrected source version links to the old one.
8. Generate margin from supported costs: missing contributions in the legacy rows remain explanatory differences, not zero costs or hidden profit adjustments.
9. Test changing column order and aliases between monthly reports: quantities remain assigned to reviewed counterparties, particularly May ENGLITE and the reordered year-summary rows.

## 9. Recommended resolution order

First establish a source-workbook/booking export with immutable row or order IDs and a finance-owned coverage cutoff. Resolve the August detail count, the three January date years, February CO ID's commercial treatment, and the five header-price differences against original orders. Then link actual provider invoices, accepted selling-price snapshots, delivery evidence and BNI settlement allocations. Only approved accounting/tax facts should flow into the monthly close, turnover workpaper or SPT schedules.

The review demonstrates why OSEE needs automated reconciliation and effective-dated price/cost records. It does not support a finding of wrongdoing, a tax shortfall, a full-year turnover figure or a completed financial audit.

## 10. Import verification addendum

The later per-cell import verification matched all 874 populated quantity cells to the PDF table coordinates. It additionally identified three vertically merged cells spanning two dated rows: January JAGO BAHASA quantity 2 across 7/8 January; May OSEE quantity 26 and TITC quantity 65 across 25/26 May. Earlier text-baseline extraction assigned these cells to individual dates. The source layout does not support selecting one date with certainty.

The import counts each merged cell once and preserves both candidate dates, its original cell location, and the previous baseline observation. Its definite parsed/proposed dates remain null, with an explicit date-ambiguity flag. These three cells cover 93 occurrences and do not change the reported or detailed monthly totals. Three additional review exceptions bring the import's exception count to 14. The private import bundle and verification report retain full provenance without modifying the original PDFs.
