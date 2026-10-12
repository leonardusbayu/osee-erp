const fs = require('fs'), path = require('path'), assert = require('assert/strict');
const {chromium} = require(process.env.OSEE_PLAYWRIGHT_PATH);
const out = process.env.OSEE_QA_DIR, base = process.env.OSEE_QA_URL;
const fixture = JSON.parse(fs.readFileSync(path.join(out, 'fixture.json'), 'utf8'));
const results = {flows: [], pages: [], errors: []};

async function login(browser, credential, width) {
  const context = await browser.newContext({viewport: {width, height: 1000}});
  const page = await context.newPage();
  page.on('pageerror', error => results.errors.push(error.message));
  await page.goto(base + '/login/');
  await page.locator('#id_username').fill(credential.username);
  await page.locator('#id_password').fill(credential.password);
  await Promise.all([page.waitForURL(url => !url.pathname.startsWith('/login/')),
    page.locator('button[type=submit]').click()]);
  return {context, page};
}
async function record(page, status = 200) {
  const state = await page.evaluate(() => ({overflow: document.documentElement.scrollWidth > innerWidth + 1,
    main: !!document.querySelector('main')}));
  results.pages.push({route: new URL(page.url()).pathname, status, width: page.viewportSize().width, ...state});
  assert.equal(state.overflow, false);
  if (status === 200) assert.equal(state.main, true);
}
async function inspect(page, route, expected = 200) {
  const response = await page.goto(base + route);
  assert.equal(response.status(), expected);
  await record(page, expected);
}
async function fill(page, values) {
  for (const [name, value] of Object.entries(values)) {
    const field = page.locator(`.form-panel [name="${name}"]`);
    if (await field.evaluate(element => element.tagName) === 'SELECT') await field.selectOption(String(value));
    else await field.fill(String(value));
  }
}
async function submit(page, name) {
  await Promise.all([page.waitForNavigation({waitUntil: 'load'}),
    page.getByRole('button', {name, exact: true}).click()]);
  await record(page);
}
async function payload(page) {
  return page.locator('.form-panel form').evaluate(form => Object.fromEntries(new FormData(form)));
}
async function createAd(page, campaign, values = {}) {
  await inspect(page, '/marketing/ads/new/');
  await fill(page, {campaign, date: '2026-01-10', ad_name: 'Semua iklan', audience: 'Semua audiens',
    region: 'Semua wilayah', spend: '100000', impressions: '1000', clicks: '50', source: 'Synthetic actual platform report',
    reference: 'SYNTHETIC-ORIGINAL-DOCUMENT', ...values});
  await submit(page, 'Simpan');
  const reference = values.reference || 'SYNTHETIC-ORIGINAL-DOCUMENT';
  const href = await page.locator('tr').filter({hasText: reference})
    .getByRole('link', {name: 'Lihat laporan →', exact: true}).getAttribute('href');
  await inspect(page, href);
  return href;
}
async function coverage(page, campaign, spend, reference, succeed = true) {
  await inspect(page, '/marketing/coverage/new/');
  await fill(page, {campaign, period_start: '2026-01-01', period_end: '2026-01-31', declared_spend: spend,
    source: 'All synthetic source accounts checked', reference,
    evidence: 'Synthetic complete source report checked with documented inactive days'});
  await page.locator('[name=confirm_complete]').check();
  await submit(page, 'Simpan');
  if (succeed) assert.equal(new URL(page.url()).pathname, '/marketing/coverage/');
  else assert.ok(await page.locator('.message-error, .field-error').count());
}
async function checked(page, values = {}) {
  await fill(page, {reason: 'Synthetic source document proves campaign date or source detail was wrong',
    evidence: 'Synthetic archived platform report and exact source row checked', ...values});
  await page.locator('.form-panel [name=confirmed]').check();
}
async function exactRetry(context, route, data, detail) {
  const response = await context.request.post(base + route, {form: data});
  assert.equal(response.status(), 200);
  assert.equal(new URL(response.url()).pathname, detail);
}
async function readers(browser, f, width, detail) {
  const {context, page} = await login(browser, f.credentials.auditor, width);
  try {
    await inspect(page, detail);
    assert.equal(await page.getByRole('link', {name: 'Perbaiki kampanye/tanggal/rincian', exact: true}).count(), 0);
    assert.equal(await page.getByRole('link', {name: 'Tarik laporan yang keliru →', exact: true}).count(), 0);
    await page.screenshot({path: path.join(out, `auditor-history-${width}.png`), fullPage: true});
    await inspect(page, detail + 'withdraw/', 403);
    await inspect(page, detail + 'replace/', 403);
    assert.equal((await context.request.post(base + detail + 'withdraw/', {form: {}})).status(), 403);
    await inspect(page, `/marketing/ads/${fixture.foreign.observation}/`, 404);
  } finally { await context.close(); }
}

(async () => {
  const browser = await chromium.launch({executablePath: process.env.OSEE_CHROME_PATH, headless: true});
  try {
    for (const width of [1440, 390]) {
      let f = fixture.fixtures[String(width)].replace;
      let {context, page} = await login(browser, f.credentials.marketing, width);
      let original;
      try {
        await createAd(page, f.corrected_campaign, {date: '2026-01-11', ad_name: 'Actual ad conflict',
          audience: 'Actual audience', region: 'Jakarta', spend: '50000', reference: 'SYNTHETIC-CONFLICT-DOCUMENT'});
        original = await createAd(page, f.original_campaign);
        await coverage(page, f.original_campaign, '100000', 'SYNTHETIC-ORIGINAL-COVERAGE');
        await coverage(page, f.corrected_campaign, '50000', 'SYNTHETIC-CORRECT-COVERAGE');
        const route = original + 'replace/';
        await inspect(page, route);
        assert.equal(await page.locator('[name=date]').inputValue(), '2026-01-10');
        assert.equal(await page.locator('[name=reference]').inputValue(), 'SYNTHETIC-ORIGINAL-DOCUMENT');
        assert.equal(await page.locator('[name=reference]').isDisabled(), true);
        await checked(page, {campaign: f.corrected_campaign, date: '2026-01-11', ad_name: 'Actual ad conflict',
          audience: 'Actual audience', region: 'Jakarta', spend: '110000'});
        await submit(page, 'Simpan pengganti');
        assert.ok(await page.locator('.message-error, .field-error').count());
        assert.equal(await page.locator('[name=spend]').inputValue(), '110000');
        assert.equal(await page.locator('[name=date]').inputValue(), '2026-01-11');
        await page.screenshot({path: path.join(out, `overlap-retained-${width}.png`), fullPage: true});
        await fill(page, {ad_name: 'Actual ad A'});
        const old = await payload(page);
        const other = await context.newPage();
        try {
          await inspect(other, original + 'correct/');
          await fill(other, {spend: '110000', reason: 'Actual source amount independently corrected'});
          await submit(other, 'Simpan');
        } finally { await other.close(); }
        await submit(page, 'Simpan pengganti');
        assert.ok((await page.locator('main').innerText()).includes('Sumber berubah setelah formulir ini dibuka'));
        assert.equal(await page.locator('[name=expected_fingerprint]').inputValue(), old.expected_fingerprint);
        assert.equal(await page.locator('[name=spend]').inputValue(), '110000');
        await page.screenshot({path: path.join(out, `stale-replacement-${width}.png`), fullPage: true});
        await submit(page, 'Periksa versi terbaru');
        assert.equal(await page.locator('[name=date]').inputValue(), '2026-01-11');
        assert.equal(await page.locator('[name=spend]').inputValue(), '110000');
        assert.equal(await page.locator('[name=expected_version]').inputValue(), '2');
        assert.equal(await page.locator('[name=confirmed]').isChecked(), false);
        await page.locator('[name=confirmed]').check();
        const saved = {...await payload(page), action: 'save'};
        await page.screenshot({path: path.join(out, `replacement-reviewed-${width}.png`), fullPage: true});
        await submit(page, 'Simpan pengganti');
        const detail = new URL(page.url()).pathname;
        assert.notEqual(detail, original);
        assert.ok((await page.locator('main').innerText()).includes('Synthetic correct campaign'));
        assert.ok((await page.locator('main').innerText()).includes('SYNTHETIC-ORIGINAL-DOCUMENT'));
        await page.screenshot({path: path.join(out, `replacement-saved-${width}.png`), fullPage: true});
        await exactRetry(context, route, saved, detail);
        const changed = await context.request.post(base + route, {form: {...saved, reason: 'Different retry must remain rejected'}});
        assert.equal(changed.status(), 200);
        assert.equal(new URL(changed.url()).pathname, route);
        assert.ok((await changed.text()).includes('isi berbeda'));
        await inspect(page, route);
        assert.equal(await page.getByRole('button', {name: 'Simpan pengganti', exact: true}).count(), 0);
        await inspect(page, original);
        assert.ok((await page.locator('main').innerText()).includes('Koreksi angka'));
        await inspect(page, '/marketing/coverage/');
        assert.ok((await page.locator('main').innerText()).includes('Data berubah · tinjau ulang'));
        await readers(browser, f, width, original);
        results.flows.push({name: 'equal-amount identity replacement with overlap/stale recovery and exact retry', width});
      } finally { await context.close(); }

      f = fixture.fixtures[String(width)].withdraw;
      ({context, page} = await login(browser, f.credentials.marketing, width));
      try {
        original = await createAd(page, f.original_campaign, {spend: f.kind === 'zero' ? '0' : '',
          impressions: f.kind === 'zero' ? '0' : '1000', clicks: f.kind === 'zero' ? '0' : ''});
        await coverage(page, f.original_campaign, '0', 'SYNTHETIC-WITHDRAW-COVERAGE', f.kind === 'zero');
        const route = original + 'withdraw/';
        await inspect(page, route);
        const missing = await context.request.post(base + route, {form: {}});
        assert.equal(missing.status(), 403);
        await checked(page);
        const saved = {...await payload(page), action: 'save'};
        await page.screenshot({path: path.join(out, `withdraw-source-${width}.png`), fullPage: true});
        await submit(page, 'Tarik dari perhitungan');
        assert.equal(new URL(page.url()).pathname, original);
        assert.ok((await page.locator('main').innerText()).includes('tidak menyatakan biaya sebenarnya nol'));
        await exactRetry(context, route, saved, original);
        await inspect(page, route);
        assert.equal(await page.getByRole('button', {name: 'Tarik dari perhitungan', exact: true}).count(), 0);
        await inspect(page, '/marketing/campaigns/');
        assert.equal(await page.getByRole('link', {name: 'Lihat laporan →', exact: true}).count(), 0);
        assert.ok((await page.locator('main').innerText()).includes('belum membuktikan biaya nol'));
        await inspect(page, '/marketing/campaigns/?history=1');
        await inspect(page, original);
        await page.screenshot({path: path.join(out, `withdraw-history-${width}.png`), fullPage: true});
        await readers(browser, f, width, original);
        results.flows.push({name: 'sole-source withdrawal with unknown/zero distinction and retained history', width, source_kind: f.kind});
      } finally { await context.close(); }
    }
    assert.equal(results.errors.length, 0);
  } catch (error) {
    results.errors.push(error.stack || String(error));
    process.exitCode = 1;
  } finally {
    await browser.close();
    fs.writeFileSync(path.join(out, 'browser-results.json'), JSON.stringify(results, null, 2));
  }
})();
