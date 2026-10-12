const fs = require('fs'), path = require('path'), assert = require('assert/strict');
const {chromium} = require(process.env.OSEE_PLAYWRIGHT_PATH);
const out = process.env.OSEE_QA_DIR, base = process.env.OSEE_QA_URL;
const fixture = JSON.parse(fs.readFileSync(path.join(out, 'fixture.json'), 'utf8'));
const result = {flows: [], pages: [], errors: []};

async function login(browser, credential, width) {
  const context = await browser.newContext({viewport: {width, height: 1000}});
  const page = await context.newPage();
  page.on('pageerror', error => result.errors.push(error.message));
  await page.goto(base + '/login/');
  await page.locator('#id_username').fill(credential.username);
  await page.locator('#id_password').fill(credential.password);
  await Promise.all([page.waitForURL(url => !url.pathname.startsWith('/login/')), page.locator('button[type=submit]').click()]);
  return {context, page};
}

async function inspect(page, route, expected = 200) {
  const response = await page.goto(base + route);
  const state = await page.evaluate(() => ({overflow: document.documentElement.scrollWidth > innerWidth + 1,
    main: !!document.querySelector('main')}));
  result.pages.push({route, status: response.status(), width: page.viewportSize().width, ...state});
  assert.equal(response.status(), expected);
  assert.equal(state.overflow, false);
  if (expected === 200) assert.equal(state.main, true);
}

async function fill(page, values) {
  for (const [name, value] of Object.entries(values)) {
    const field = page.locator(`[name="${name}"]`);
    if (await field.evaluate(element => element.tagName) === 'SELECT') await field.selectOption(String(value));
    else await field.fill(String(value));
  }
}

async function submit(page, name) {
  await Promise.all([page.waitForNavigation({waitUntil: 'load'}), page.getByRole('button', {name, exact: true}).click()]);
}

async function identityFields(page, f, {target, party, name, type = 'npwp16', status = 'registered', number = '', residency = 'spdn', direct = false}) {
  await fill(page, {target});
  if (party) await fill(page, {recipient_party: party});
  await fill(page, {legal_name: name, recipient_type: 'body', tax_residency: residency, population_status: 'na',
    identity_type: type, number, registration_status: status, effective_from: '2025-05-22',
    reference: 'SYNTHETIC-BROWSER-IDENTITY', reason: 'Synthetic original recipient identity evidence documented for professional review'});
  assert.equal(await page.locator('[name=nik_validated]').isVisible(), false);
  if (direct) await page.locator('[name=file]').setInputFiles(f.pdf_path);
  else await fill(page, {evidence: f.evidence});
}

async function reportFields(page, f) {
  await fill(page, {evidence: f.evidence, professional_name: 'Synthetic browser professional',
    qualification_reference: 'Synthetic received professional engagement', reviewed_on: '2026-10-09',
    statement: 'Synthetic received report matches the exact displayed recipient and tax calculation'});
  await page.locator('[name=report_confirmed]').check();
}

async function approveIdentity(page, f, route, treatment) {
  await inspect(page, route + 'review/');
  await reportFields(page, f);
  await fill(page, {treatment});
  await submit(page, 'Simpan keputusan sesuai laporan');
  assert.equal(new URL(page.url()).pathname, route);
}

async function taxFields(page, f, identity, {direction = 'payable', reference = 'SYNTHETIC-MANUAL-PAYABLE', baseAmount = '500000'} = {}) {
  await fill(page, {rule_code: 'pph23_service_2', direction});
  await fill(page, {period: '2026-09', base: baseAmount, source_reference: reference,
    rule_reference: 'Synthetic professionally reviewed PPh23 object', object_code: '24-104-99', evidence: f.evidence,
    note: 'Synthetic classification, recipient and tax point matched to documents', recipient_identity: identity,
    tax_point_date: '2026-09-10', tax_point_basis: 'due', withholder_reference: direction === 'payable' ? '1234567890123456' : '3234567890123456',
    withholder_name: direction === 'payable' ? f.company_name : f.customer_name,
    object_reason: 'Synthetic professional object and final exemption review'});
  if (direction === 'payable') await fill(page, {recipient_party: f.supplier});
  else await fill(page, {source_invoice: f.invoice});
  await page.locator('[name=final_exempt_excluded]').check();
}

(async () => {
  const browser = await chromium.launch({executablePath: process.env.OSEE_CHROME_PATH, headless: true});
  try {
    for (const width of [1440, 390]) {
      const {normal: f, final} = fixture.fixtures[String(width)];
      let {context, page} = await login(browser, f.credentials.finance, width);
      await inspect(page, '/tax/recipients/new/');
      await identityFields(page, f, {target: 'organization', name: f.company_name, number: '1234567890123456', direct: true});
      const requestKey = await page.locator('[name=request_key]').inputValue();
      await fill(page, {effective_from: '2025-05-21'});
      await submit(page, 'Simpan untuk pemeriksaan');
      assert.match(await page.locator('main').innerText(), /22 Mei 2025/);
      assert.equal(await page.locator('[name=number]').inputValue(), '1234567890123456');
      assert.equal(await page.locator('[name=request_key]').inputValue(), requestKey);
      await fill(page, {effective_from: '2025-05-22'});
      await page.locator('[name=file]').setInputFiles(f.pdf_path);
      await submit(page, 'Simpan untuk pemeriksaan');
      const ownRoute = new URL(page.url()).pathname, own = ownRoute.split('/').filter(Boolean).at(-1);
      assert.match(ownRoute, /\/tax\/recipients\/\d+\/$/);
      await inspect(page, ownRoute + 'review/', 403);
      await context.close();
      ({context, page} = await login(browser, f.credentials.owner, width));
      await inspect(page, ownRoute + 'review/');
      await reportFields(page, f);
      await fill(page, {treatment: 'standard_npwp'});
      const version = await page.locator('[name=expected_version]').inputValue();
      await page.locator('[name=expected_version]').evaluate(element => { element.value = '99'; });
      await submit(page, 'Simpan keputusan sesuai laporan');
      assert.match(await page.locator('main').innerText(), /Versi identitas berubah/);
      assert.equal(await page.locator('[name=treatment]').inputValue(), 'standard_npwp');
      assert.equal(await page.locator('[name=professional_name]').inputValue(), 'Synthetic browser professional');
      await page.locator('[name=expected_version]').evaluate((element, value) => { element.value = value; }, version);
      await submit(page, 'Simpan keputusan sesuai laporan');
      assert.equal(new URL(page.url()).pathname, ownRoute);
      await context.close();
      result.flows.push({width, flow: 'Finance direct PDF/date-error recovery -> Owner exact-version report'});

      ({context, page} = await login(browser, f.credentials.finance, width));
      await inspect(page, '/tax/recipients/new/');
      await identityFields(page, f, {target: 'supplier', party: f.supplier, name: f.supplier_name, type: 'none', status: 'no_npwp'});
      await submit(page, 'Simpan untuk pemeriksaan');
      const supplierRoute = new URL(page.url()).pathname, supplier = supplierRoute.split('/').filter(Boolean).at(-1);
      await context.close();
      ({context, page} = await login(browser, f.credentials.owner, width));
      await approveIdentity(page, f, supplierRoute, 'increased_no_npwp');
      await context.close();
      ({context, page} = await login(browser, f.credentials.finance, width));
      await inspect(page, '/tax/obligations/new/');
      assert.doesNotMatch(await page.locator('main').innerText(), /UNREVIEWED-FREE-PARTY-ID/);
      await taxFields(page, f, supplier);
      assert.equal(await page.locator('[name=source_invoice]').isVisible(), false);
      await submit(page, 'Simpan catatan');
      const payableRoute = new URL(page.url()).pathname;
      assert.match(payableRoute, /\/tax\/obligations\/\d+\/$/);
      assert.match(await page.locator('main').innerText(), /Rp\s*20\.000/);
      await inspect(page, '/tax/recipients/');
      assert.doesNotMatch(await page.locator('main').innerText(), /1234567890123456/);
      await page.screenshot({path: path.join(out, `recipient-list-${width}.png`), fullPage: true});
      await context.close();
      ({context, page} = await login(browser, f.credentials.owner, width));
      await inspect(page, payableRoute + 'review/');
      await reportFields(page, f);
      await submit(page, 'Simpan catatan');
      await inspect(page, `/tax/bills/${f.positive_bill}/review/`);
      await reportFields(page, f);
      await fill(page, {rule_code: 'pph23_service_2', base: '500000', period: '2026-09', object_code: '24-104-99',
        reason: 'Synthetic professional object and exclusion report for this supplier bill', recipient_identity: supplier,
        tax_point_date: '2026-09-10', tax_point_basis: 'due', withholder_name: f.company_name, withholder_reference: '1234567890123456'});
      await page.locator('[name=final_exempt_excluded]').check();
      await submit(page, 'Simpan catatan');
      assert.match(new URL(page.url()).pathname, /\/tax\/obligations\/\d+\/$/);
      await inspect(page, `/tax/bills/${f.nil_bill}/review/`);
      await fill(page, {rule_code: 'no_withholding'});
      assert.equal(await page.locator('[data-pph23-group]').isVisible(), false);
      assert.equal(await page.locator('[name=tax_point_date]').isDisabled(), true);
      await reportFields(page, f);
      await fill(page, {base: '250000', period: '2026-09', reason: 'Synthetic professional exclusion explains no withholding on this bill'});
      await submit(page, 'Simpan catatan');
      assert.equal(new URL(page.url()).pathname, '/tax/');
      await context.close();
      result.flows.push({width, flow: 'Documented no-NPWP factor 2 -> manual/bill 20k; no-withholding needs no PPh23 identity'});

      ({context, page} = await login(browser, f.credentials.finance, width));
      await inspect(page, '/tax/obligations/new/');
      await taxFields(page, f, own, {direction: 'receivable', reference: 'SYNTHETIC-INVOICE-CREDIT'});
      assert.equal(await page.locator('[name=recipient_party]').isVisible(), false);
      await submit(page, 'Simpan catatan');
      const customerRoute = new URL(page.url()).pathname;
      assert.match(customerRoute, /\/tax\/obligations\/\d+\/$/);
      await context.close();
      ({context, page} = await login(browser, f.credentials.owner, width));
      await inspect(page, customerRoute + 'review/');
      await reportFields(page, f);
      await submit(page, 'Simpan catatan');
      assert.match(await page.locator('main').innerText(), /Owner\/Direktur perlu mencocokkan/);
      assert.equal(await page.getByRole('link', {name: 'Cocokkan uang masuk setelah potongan', exact: true}).count(), 0);
      await inspect(page, customerRoute + 'verify/');
      await fill(page, {kind: 'credit', evidence: f.evidence, reference: `SYNTHETIC-CERTIFICATE-${width}`, taxpayer_reference: '1234567890123456',
        period: '2026-09', amount: '10000', note: 'Synthetic received official certificate matched with both parties',
        recipient_reference: '1234567890123456', recipient_name: f.company_name, withholder_reference: '4234567890123456',
        withholder_name: f.customer_name, object_code: '24-104-99', base: '500000', tax_point_date: '2026-09-10', certificate_status: 'normal'});
      await page.locator('[name=matches_confirmed]').check();
      await submit(page, 'Simpan catatan');
      assert.equal(new URL(page.url()).pathname, customerRoute + 'verify/');
      assert.equal(await page.locator('[name=withholder_reference]').inputValue(), '4234567890123456');
      assert.equal(await page.locator('[name=recipient_name]').inputValue(), f.company_name);
      assert.match(await page.locator('main').innerText(), /harus cocok tepat/);
      await fill(page, {withholder_reference: '3234567890123456'});
      await submit(page, 'Simpan catatan');
      assert.equal(new URL(page.url()).pathname, customerRoute);
      await inspect(page, customerRoute);
      assert.equal(await page.getByRole('link', {name: 'Cocokkan uang masuk setelah potongan', exact: true}).count(), 1);
      await page.screenshot({path: path.join(out, `recipient-credit-${width}.png`), fullPage: true});
      await context.close();
      result.flows.push({width, flow: 'Invoice receivable -> report -> mismatched certificate preserved -> exact two-party credit proof'});

      ({context, page} = await login(browser, f.credentials.finance, width));
      await inspect(page, customerRoute);
      const receiptRoute = await page.getByRole('link', {name: 'Cocokkan uang masuk setelah potongan', exact: true}).getAttribute('href');
      await inspect(page, receiptRoute);
      await fill(page, {transaction: f.net_bank, amount: '490000', evidence: 'Synthetic net bank movement and exact identity-bound certificate matched'});
      await page.locator('[name=confirmed]').check();
      const retry = await page.locator('main form').evaluate(form => Object.fromEntries(new FormData(form)));
      await submit(page, 'Konfirmasi & catat');
      assert.equal(new URL(page.url()).pathname, '/finance/movements/');
      const replay = await page.request.post(base + receiptRoute, {form: retry});
      assert.equal(replay.status(), 200);
      assert.equal(new URL(replay.url()).pathname, '/finance/movements/');
      await inspect(page, customerRoute);
      assert.match(await page.locator('main').innerText(), /Uang bank:\s*Rp\s*490\.000/);
      assert.match(await page.locator('main').innerText(), /Invoice terbayar:\s*Rp\s*500\.000/);
      await page.screenshot({path: path.join(out, `recipient-settled-${width}.png`), fullPage: true});
      await context.close();
      result.flows.push({width, flow: 'Finance uses reloaded credit proof: bank490k + tax10k settles invoice500k; exact POST retry'});

      ({context, page} = await login(browser, f.credentials.finance, width));
      await inspect(page, '/tax/recipients/new/');
      await identityFields(page, f, {target: 'supplier', party: f.pending, name: f.pending_name, type: 'unknown', status: 'unknown', residency: 'unknown'});
      await submit(page, 'Simpan untuk pemeriksaan');
      const pendingRoute = new URL(page.url()).pathname;
      await context.close();
      ({context, page} = await login(browser, f.credentials.owner, width));
      await approveIdentity(page, f, pendingRoute, 'unresolved');
      assert.match(await page.locator('main').innerText(), /Belum dapat digunakan untuk PPh 23/);
      const recovery = await page.getByRole('link', {name: 'Siapkan pemeriksaan baru', exact: true}).getAttribute('href');
      await context.close();
      ({context, page} = await login(browser, f.credentials.finance, width));
      await inspect(page, recovery);
      assert.equal(await page.locator('[name=effective_from]').inputValue(), '2025-05-22');
      await fill(page, {tax_residency: 'spdn', identity_type: 'npwp16', number: '5234567890123456', registration_status: 'registered',
        reference: 'SYNTHETIC-CORRECTED-IDENTITY', reason: 'Synthetic new evidence resolves previously unknown facts from the same actual date', evidence: ''});
      await page.locator('[name=file]').setInputFiles(f.pdf_path);
      await submit(page, 'Simpan untuk pemeriksaan');
      const correctedRoute = new URL(page.url()).pathname;
      assert.match(correctedRoute, /\/tax\/recipients\/\d+\/$/);
      assert.notEqual(correctedRoute, pendingRoute);
      await context.close();
      ({context, page} = await login(browser, f.credentials.owner, width));
      await approveIdentity(page, f, correctedRoute, 'standard_npwp');
      await inspect(page, pendingRoute);
      assert.match(await page.locator('main').innerText(), /Versi sebelumnya/);
      await inspect(page, correctedRoute);
      await page.screenshot({path: path.join(out, `recipient-recovered-${width}.png`), fullPage: true});
      await context.close();
      result.flows.push({width, flow: 'Unresolved professional decision -> documented resolved successor on same actual date, history retained'});

      ({context, page} = await login(browser, f.credentials.auditor, width));
      await inspect(page, ownRoute);
      assert.doesNotMatch(await page.locator('main').innerText(), /1234567890123456/);
      await inspect(page, ownRoute + 'review/', 403);
      await context.close();
      ({context, page} = await login(browser, f.credentials.marketing, width));
      for (const route of ['/tax/recipients/', ownRoute, customerRoute, `/tax/evidence/${f.evidence}/download/`]) await inspect(page, route, 403);
      await context.close();
      result.flows.push({width, flow: 'Audit role masked history only; Marketing cannot access recipient, obligation or private evidence'});

      ({context, page} = await login(browser, final.credentials.finance, width));
      await inspect(page, '/tax/obligations/new/');
      await fill(page, {rule_code: 'final_turnover_005', direction: 'own_tax'});
      assert.equal(await page.locator('[data-pph23-group]').isVisible(), false);
      assert.equal(await page.locator('[name=recipient_identity]').isDisabled(), true);
      await fill(page, {period: '2026-09', base: '1000000', source_reference: 'SYNTHETIC-FINAL-BROWSER',
        rule_reference: 'Synthetic final profile and source review', object_code: '411128-420', evidence: final.evidence,
        note: 'Synthetic final amount requires no recipient PPh23 registry'});
      await submit(page, 'Simpan catatan');
      assert.match(new URL(page.url()).pathname, /\/tax\/obligations\/\d+\/$/);
      await context.close();
      result.flows.push({width, flow: 'Final turnover form remains usable without PPh23-only inputs'});
    }
    assert.deepEqual(result.errors, []);
  } catch (error) {
    result.failure = error.stack;
    throw error;
  } finally {
    fs.writeFileSync(path.join(out, 'browser-results.json'), JSON.stringify(result, null, 2));
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
