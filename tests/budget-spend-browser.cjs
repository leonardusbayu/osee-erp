const fs = require('fs'), path = require('path'), assert = require('assert/strict');
const {chromium} = require(process.env.OSEE_PLAYWRIGHT_PATH);
const out = process.env.OSEE_QA_DIR, base = process.env.OSEE_QA_URL;
const fixture = JSON.parse(fs.readFileSync(path.join(out, 'fixture.json'), 'utf8'));
const result = {flows: [], pages: [], errors: []};

async function login(browser, credential, width) {
  const context = await browser.newContext({viewport: {width, height: 1000}});
  const page = await context.newPage();
  page.on('pageerror', error => result.errors.push(error.message));
  page.on('dialog', dialog => dialog.accept());
  await page.goto(base + '/login/');
  await page.locator('#id_username').fill(credential.username);
  await page.locator('#id_password').fill(credential.password);
  await Promise.all([page.waitForURL(url => !url.pathname.startsWith('/login/')), page.locator('button[type=submit]').click()]);
  return {context, page};
}
async function record(page, status = 200) {
  const state = await page.evaluate(() => ({overflow: document.documentElement.scrollWidth > innerWidth + 1,
    main: !!document.querySelector('main')}));
  result.pages.push({route: new URL(page.url()).pathname, status, width: page.viewportSize().width, ...state});
  assert.equal(state.overflow, false);
  if (status === 200) assert.equal(state.main, true);
}
async function inspect(page, route, expected = 200) {
  const response = await page.goto(base + route);
  assert.equal(response.status(), expected);
  await record(page, response.status());
}
async function fill(page, values) {
  for (const [name, value] of Object.entries(values)) {
    const field = page.locator(`.form-panel [name="${name}"]`);
    const tag = await field.evaluate(element => element.tagName);
    if (tag === 'SELECT') await field.selectOption(String(value));
    else await field.fill(String(value));
  }
}
async function checked(page, values) {
  await fill(page, {date: '2026-09-15', reason: 'Synthetic exact spending decision checked',
    evidence: 'Synthetic source document and amount correspondence checked', ...values});
  await page.locator('.form-panel [name=confirmed]').check();
}
async function submit(page, label) {
  await Promise.all([page.waitForNavigation({waitUntil: 'load'}), page.getByRole('button', {name: label, exact: true}).click()]);
  await record(page);
}
async function payload(page) {
  return page.locator('.form-panel form').evaluate(form => Object.fromEntries(new FormData(form)));
}
async function exactRetry(context, route, data) {
  const response = await context.request.post(base + route, {form: data});
  assert.equal(response.status(), 200);
  assert.match(new URL(response.url()).pathname, /\/director\/spends\/\d+\/$/);
}
async function adopt(page, f) {
  await inspect(page, `/director/budgets/${f.budget}/`);
  const route = await page.getByRole('link', {name: 'Kelola belanja ini', exact: true}).first().getAttribute('href');
  await inspect(page, route);
  assert.equal(await page.locator('.form-panel [name=date]').inputValue(), '');
  await checked(page, {date: '2026-09-10'});
  await submit(page, 'Mulai kelola belanja');
  const detail = await page.getByRole('link', {name: 'Lihat belanja dan langkah berikutnya →', exact: true}).getAttribute('href');
  await inspect(page, detail);
  return detail;
}
async function linkBill(context, page, f, detail, amounts, date = '2026-09-15') {
  const route = detail + 'bills/new/';
  await inspect(page, route);
  await checked(page, {date, bill: f.bill, ...amounts});
  await submit(page, 'Periksa tagihan terbaru');
  assert.match(await page.locator('main').innerText(), /Tagihan yang diperiksa/);
  const data = {...await payload(page), intent: 'save'};
  assert.match(data.expected_bill_fingerprint, /^[0-9a-f]{64}$/);
  await page.screenshot({path: path.join(out, `bill-preview-${f.kind}-${page.viewportSize().width}.png`), fullPage: true});
  await submit(page, 'Simpan hubungan yang diperiksa');
  await exactRetry(context, route, data);
}

(async () => {
  const browser = await chromium.launch({executablePath: process.env.OSEE_CHROME_PATH, headless: true});
  try {
    for (const width of [1440, 390]) {
      const groups = fixture.fixtures[String(width)];
      let f = groups.manual, {context, page} = await login(browser, f.credentials.finance, width);
      const manual = await adopt(page, f);
      await inspect(page, manual + 'convert/');
      await checked(page, {date: '2026-09-12', transfer: 'open_commitment:incurred', amount: '20000000'});
      const first = {...await payload(page), intent: 'save'};
      await submit(page, 'Simpan catatan');
      await exactRetry(context, manual + 'convert/', first);
      await page.getByText('Pembayaran dan utang terkait', {exact: true}).click();
      let text = await page.locator('main').innerText();
      for (const value of ['Rp20.000.000', 'Rp60.000.000', 'Rp80.000.000']) assert.ok(text.includes(value));
      assert.match(text, /Uang keluar bank yang dicocokkan Finance/);
      await page.screenshot({path: path.join(out, `partial-spend-${width}.png`), fullPage: true});
      await inspect(page, manual + 'convert/');
      await checked(page, {date: '2026-09-14', transfer: 'open_commitment:incurred', amount: '5000000'});
      const old = await payload(page);
      const other = await context.newPage();
      await inspect(other, manual + 'payments/new/');
      await checked(other, {date: '2026-09-13', amount: '1000000'});
      await submit(other, 'Simpan catatan');
      await other.close();
      await submit(page, 'Simpan catatan');
      assert.equal(await page.locator('[name=amount]').inputValue(), '5000000');
      assert.equal(await page.locator('[name=reference]').inputValue(), old.reference);
      assert.ok(await page.locator('.message-error').count());
      await page.screenshot({path: path.join(out, `stale-spend-${width}.png`), fullPage: true});
      await submit(page, 'Muat posisi terbaru');
      assert.equal(await page.locator('[name=amount]').inputValue(), '5000000');
      await submit(page, 'Simpan catatan');
      assert.equal(await page.getByRole('link', {name: 'Perbaiki catatan ini', exact: true}).count(), 0);
      await context.close();
      ({context, page} = await login(browser, f.credentials.owner, width));
      await inspect(page, manual);
      const secondCorrection = await page.getByRole('link', {name: 'Perbaiki catatan ini', exact: true}).last().getAttribute('href');
      await inspect(page, secondCorrection);
      await checked(page, {date: '2026-09-15', correction: 'replace', transfer: 'open_commitment:incurred', amount: '3000000'});
      await submit(page, 'Simpan perbaikan');
      await inspect(page, manual + 'release/');
      await checked(page, {date: '2026-09-16', bucket: 'open_commitment', amount: '2000000'});
      await submit(page, 'Simpan catatan');
      await page.screenshot({path: path.join(out, `corrected-spend-${width}.png`), fullPage: true});
      result.flows.push({width, flow: '80m commitment, reported30m, actual20m partial conversion; exact retry; stale retained fields; Owner immutable replacement and residual release'});
      await context.close();

      f = groups.prepaid;
      ({context, page} = await login(browser, f.credentials.finance, width));
      const prepaid = await adopt(page, f);
      await inspect(page, prepaid + 'payments/new/');
      await checked(page, {date: '2026-09-11', amount: '30000000'});
      await submit(page, 'Simpan catatan');
      await linkBill(context, page, f, prepaid, {[`coverage_entry_${f.entry}`]: '60000000'}, '2026-09-26');
      await inspect(page, `/finance/bills/${f.bill}/release/`);
      await fill(page, {date: '2026-09-26', amount: '20000000', evidence: 'Synthetic exact delivered campaign service portion'});
      await page.locator('[name=confirmed]').check();
      await submit(page, 'Konfirmasi & catat');
      await inspect(page, prepaid);
      await page.getByText('Pembayaran dan utang terkait', {exact: true}).click();
      text = await page.locator('main').innerText();
      for (const value of ['Rp20.000.000', 'Rp40.000.000', 'Rp80.000.000']) assert.ok(text.includes(value));
      await page.screenshot({path: path.join(out, `prepaid-position-${width}.png`), fullPage: true});
      for (const [payment, amount, date] of [[f.first_payment, '20000000', '2026-09-26'], [f.second_payment, '10000000', '2026-09-27']]) {
        await inspect(page, prepaid + 'payments/match/');
        const manualKey = await page.locator('[name=manual_source_key] option').nth(1).getAttribute('value');
        await checked(page, {date, manual_source_key: manualKey, finance_event_id: `bill_payment:${payment}`, amount});
        const data = {...await payload(page), intent: 'save'};
        await submit(page, 'Simpan catatan');
        await exactRetry(context, prepaid + 'payments/match/', data);
      }
      await inspect(page, prepaid);
      await page.getByText('Pembayaran dan utang terkait', {exact: true}).click();
      assert.match(await page.locator('main').innerText(), /Rp35\.000\.000/);
      await context.close();
      ({context, page} = await login(browser, f.credentials.owner, width));
      await inspect(page, prepaid);
      const voidMatch = await page.getByRole('link', {name: 'Tarik pencocokan yang keliru', exact: true}).last().getAttribute('href');
      await inspect(page, voidMatch);
      await checked(page, {date: '2026-09-28'});
      await submit(page, 'Simpan perbaikan');
      await page.getByText('Pembayaran dan utang terkait', {exact: true}).click();
      await page.screenshot({path: path.join(out, `payment-match-history-${width}.png`), fullPage: true});
      result.flows.push({width, flow: 'Prepaid60m replaces reservation once; public20m recognition;35m actual bank distinct from reported30m; payment matches retry once and Owner withdraws wrong overlap'});
      await context.close();

      f = groups.multi;
      ({context, page} = await login(browser, f.credentials.finance, width));
      const multi = await adopt(page, f);
      await inspect(page, multi + 'convert/');
      await checked(page, {date: '2026-09-12', transfer: 'open_commitment:incurred', amount: '20000000'});
      await submit(page, 'Simpan catatan');
      await inspect(page, multi + 'bills/new/');
      const movedName = await page.locator('[name^=coverage_movement_]').first().getAttribute('name');
      await linkBill(context, page, f, multi, {[`coverage_entry_${f.entry}`]: '60000000', [movedName]: '20000000'});
      assert.match(await page.locator('main').innerText(), /Rp80\.000\.000/);
      await context.close();
      ({context, page} = await login(browser, f.credentials.owner, width));
      await inspect(page, multi);
      const voidLink = await page.getByRole('link', {name: 'Tarik hubungan yang keliru', exact: true}).getAttribute('href');
      await inspect(page, voidLink);
      await checked(page, {date: '2026-09-16'});
      await submit(page, 'Simpan perbaikan');
      await page.screenshot({path: path.join(out, `multi-source-recovery-${width}.png`), fullPage: true});
      result.flows.push({width, flow: 'One80m Bill explicitly covers both20m incurred and60m remaining commitment; exact retry; Owner withdraws link without changing Finance journals'});
      await context.close();

      f = groups.stale;
      ({context, page} = await login(browser, f.credentials.finance, width));
      const stale = await adopt(page, f);
      await inspect(page, stale + 'bills/new/');
      await checked(page, {date: '2026-09-15', bill: f.bill, [`coverage_entry_${f.entry}`]: '60000000'});
      await submit(page, 'Periksa tagihan terbaru');
      const oldHash = await page.locator('[name=expected_bill_fingerprint]').inputValue();
      const approve = await context.newPage();
      approve.on('dialog', dialog => dialog.accept());
      await inspect(approve, `/bills/${f.bill}/`);
      await submit(approve, 'Setujui pembukuan');
      await approve.close();
      await submit(page, 'Simpan hubungan yang diperiksa');
      assert.ok(await page.locator('.message-error').count());
      assert.equal(await page.locator('[name=expected_bill_fingerprint]').inputValue(), oldHash);
      assert.equal(await page.locator(`[name=coverage_entry_${f.entry}]`).inputValue(), '60000000');
      await page.screenshot({path: path.join(out, `changed-bill-${width}.png`), fullPage: true});
      await submit(page, 'Periksa tagihan terbaru');
      assert.notEqual(await page.locator('[name=expected_bill_fingerprint]').inputValue(), oldHash);
      await submit(page, 'Simpan hubungan yang diperiksa');
      const csrf = await context.request.post(base + stale + 'payments/new/', {form: {amount: '1000'}});
      assert.equal(csrf.status(), 403);
      await inspect(page, `/director/budgets/${fixture.foreign.budget}/`, 404);
      result.flows.push({width, flow: 'Changed unlinked Bill is rejected with values and reviewed fingerprint retained; explicit recheck then save; CSRF and foreign organization blocked'});
      await context.close();

      f = groups.manual;
      ({context, page} = await login(browser, f.credentials.marketing, width));
      await inspect(page, manual, 403);
      await inspect(page, manual + 'convert/', 403);
      await context.close();
      ({context, page} = await login(browser, f.credentials.auditor, width));
      await inspect(page, manual);
      assert.equal(await page.getByRole('link', {name: 'Catat perubahan sebagian', exact: true}).count(), 0);
      await inspect(page, manual + 'payments/new/', 403);
      await context.close();
      result.flows.push({width, flow: 'Marketing denied; Auditor reads persisted history without write actions'});
    }
    assert.deepEqual(result.errors, []);
  } catch (error) {
    result.failure = error.stack;
    process.exitCode = 1;
  } finally {
    await browser.close();
    fs.writeFileSync(path.join(out, 'browser-results.json'), JSON.stringify(result, null, 2));
    console.log(JSON.stringify({flows: result.flows.length, pages: result.pages.length, errors: result.errors, failure: result.failure}));
  }
})();
