const fs = require('fs'), path = require('path'), assert = require('assert/strict');
const {chromium} = require(process.env.OSEE_PLAYWRIGHT_PATH);
const out = process.env.OSEE_QA_DIR, base = process.env.OSEE_QA_URL, stage = process.env.OSEE_QA_STAGE;
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
  const state = await page.evaluate(() => ({overflow: document.documentElement.scrollWidth > innerWidth + 1, main: !!document.querySelector('main')}));
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
async function submit(page) {
  await Promise.all([page.waitForNavigation({waitUntil: 'load'}), page.getByRole('button', {name: 'Konfirmasi & catat', exact: true}).click()]);
}
async function payload(page) {
  return page.locator('.form-panel form').evaluate(form => Object.fromEntries(new FormData(form)));
}

(async () => {
  const browser = await chromium.launch({executablePath: process.env.OSEE_CHROME_PATH, headless: true});
  try {
    for (const width of [1440, 390]) {
      const {normal: f, missing} = fixture.fixtures[String(width)];
      let {context, page} = await login(browser, f.credentials.finance, width);
      if (stage === 'prepare') {
        await inspect(page, '/finance/advances/new/');
        await fill(page, {party: f.party, transaction: f.bank, amount: '490000',
          evidence: 'Synthetic original bank advance before the invoice'});
        await page.locator('[name=confirmed]').check();
        await submit(page);
        assert.equal(new URL(page.url()).pathname, '/finance/movements/');
        const apply = await page.getByRole('link', {name: 'Gunakan untuk membayar invoice', exact: true}).getAttribute('href');
        await inspect(page, apply);
        await fill(page, {invoice: f.invoice, date: '2026-09-10', amount: '490000',
          evidence: 'Synthetic exact advance used for the original invoice'});
        await page.locator('[name=confirmed]').check();
        await page.screenshot({path: path.join(out, `advance-application-${width}.png`), fullPage: true});
        await submit(page);
        assert.equal(new URL(page.url()).pathname, '/finance/movements/');
        assert.match(await page.locator('main').innerText(), /Rp490\.000/);
        result.flows.push({width, flow: 'Public Finance records old bank advance and applies it to exact invoice'});
      } else {
        const route = `/finance/tax/${f.obligation}/credit-after-payment/`;
        await inspect(page, `/tax/obligations/${f.obligation}/`);
        assert.equal(await page.getByRole('link', {name: 'Catat bukti potong untuk pembayaran lama', exact: true}).getAttribute('href'), route);
        await inspect(page, route);
        assert.equal(await page.locator('[name=date]').inputValue(), '');
        const text = await page.locator('main').innerText();
        assert.match(text, /Uang muka digunakan/);
        assert.match(text, /01 Sep 2026/);
        assert.match(text, /10 Sep 2026/);
        assert.match(text, /Uang bank baru Rp0/);
        assert.doesNotMatch(text, /AdvanceApplication|Allocation/);
        await fill(page, {source_allocation: `advance:${f.application}`, date: '2026-09-10',
          evidence: 'Synthetic exact applied advance and verified certificate'});
        await page.locator('[name=confirmed]').check();
        const originalReference = await page.locator('[name=reference]').inputValue();
        await submit(page);
        assert.equal(new URL(page.url()).pathname, route);
        assert.equal(await page.locator('[name=date]').inputValue(), '2026-09-10');
        assert.equal(await page.locator('[name=source_allocation]').inputValue(), `advance:${f.application}`);
        assert.equal(await page.locator('[name=reference]').inputValue(), originalReference);
        assert.match(await page.locator('.message-error').first().innerText(), /dikunci|tertutup/);
        await fill(page, {date: '2026-10-01'});
        const exact = await payload(page);
        await page.screenshot({path: path.join(out, `late-credit-form-${width}.png`), fullPage: true});
        await submit(page);
        assert.equal(new URL(page.url()).pathname, '/finance/movements/');
        assert.match(await page.locator('main').innerText(), /Uang muka digunakan untuk invoice/);
        assert.match(await page.locator('main').innerText(), /Uang bank baru/);
        await page.screenshot({path: path.join(out, `late-credit-history-${width}.png`), fullPage: true});
        const retry = await context.request.post(base + route, {form: exact});
        assert.equal(retry.status(), 200);
        assert.equal(new URL(retry.url()).pathname, '/finance/movements/');
        const conflict = await context.request.post(base + route, {form: {...exact, evidence: 'Synthetic changed retry must remain rejected'}});
        assert.equal(conflict.status(), 200);
        assert.match(await conflict.text(), /Permintaan yang berbeda belum tersimpan/);
        assert.match(await conflict.text(), /Synthetic changed retry must remain rejected/);
        await inspect(page, route);
        assert.match(await page.locator('main').innerText(), /Kredit pajak sudah tercatat/);
        assert.equal(await page.locator('[name=source_allocation]').count(), 0);
        await inspect(page, `/tax/obligations/${f.obligation}/`);
        assert.match(await page.locator('main').innerText(), /Uang bank baru Rp0/);
        assert.equal(await page.getByRole('link', {name: 'Catat bukti potong untuk pembayaran lama', exact: true}).count(), 0);
        await page.screenshot({path: path.join(out, `advance-tax-settled-${width}.png`), fullPage: true});
        await inspect(page, `/finance/tax/${fixture.foreign.obligation}/credit-after-payment/`, 404);
        await context.close();
        ({context, page} = await login(browser, missing.credentials.finance, width));
        await inspect(page, `/finance/tax/${missing.obligation}/credit-after-payment/`);
        assert.match(await page.locator('main').innerText(), /Periksa bukti potong terlebih dahulu/);
        assert.equal(await page.locator('[name=source_allocation]').count(), 0);
        assert.equal(await page.getByRole('link', {name: 'Lihat pemeriksaan bukti potong', exact: true}).getAttribute('href'), `/tax/obligations/${missing.obligation}/`);
        await context.close();
        ({context, page} = await login(browser, f.credentials.marketing, width));
        await inspect(page, route, 403);
        await inspect(page, '/finance/movements/', 403);
        await context.close();
        ({context, page} = await login(browser, f.credentials.auditor, width));
        await inspect(page, route, 403);
        await inspect(page, '/finance/movements/');
        assert.equal(await page.getByRole('link', {name: 'Catat uang muka', exact: true}).count(), 0);
        result.flows.push({width, flow: 'Closed-old-month error preserves values; open-month credit and exact retry; changed retry rejects; scoped role/read-only history'});
      }
      await context.close();
    }
    assert.deepEqual(result.errors, []);
  } catch (error) {
    result.failure = error.stack;
    process.exitCode = 1;
  } finally {
    await browser.close();
    fs.writeFileSync(path.join(out, `browser-${stage}.json`), JSON.stringify(result, null, 2));
    console.log(JSON.stringify({stage, flows: result.flows.length, pages: result.pages.length, errors: result.errors, failure: result.failure}));
  }
})();
