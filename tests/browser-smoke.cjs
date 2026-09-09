/* Real browser verification against the local, isolated DEMO workspace only. */
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const { chromium } = require(process.env.OSEE_PLAYWRIGHT_PATH || 'playwright');

(async () => {
  const base = process.env.OSEE_TEST_URL || 'http://127.0.0.1:8000';
  assert.ok(/^http:\/\/127\.0\.0\.1:\d+$/.test(base), 'Browser smoke is restricted to localhost.');
  const output = path.resolve('test-results');
  await fs.mkdir(output, { recursive: true });
  const browser = await chromium.launch({ channel: 'chrome', headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1050 }, locale: 'id-ID' });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  try {
    await page.goto(base + '/login/');
    await page.getByRole('button', { name: 'Buka workspace demo', exact: true }).click();
    await page.waitForURL(base + '/');
    await page.getByRole('heading', { name: /Ringkasan keuangan/ }).waitFor();
    assert.ok(await page.getByText('Data contoh', { exact: false }).count() > 0, 'Demo must be labelled.');
    await page.screenshot({ path: path.join(output, 'dashboard-desktop.png'), fullPage: true });
    const pages = ['/sales/', '/sales/new/', '/bills/', '/bills/new/', '/bank/', '/bank/import/', '/partners/', '/prices/', '/reports/', '/settings/', '/modules/', '/tax/', '/tax/annual/', '/tax/chat/'];
    for (const route of pages) {
      const response = await page.goto(base + route);
      assert.equal(response.status(), 200, route + ' must render');
      assert.ok(await page.locator('h1').count() > 0, route + ' needs a visible page heading');
      assert.equal(await page.locator('body').evaluate(node => node.scrollWidth > window.innerWidth + 2), false, route + ' desktop overflow');
    }
    await page.goto(base + '/sales/');
    await page.locator('table a.table-link').first().click();
    const invoicePath = new URL(page.url()).pathname;
    const pdf = await context.request.get(base + invoicePath + 'document/');
    assert.equal(pdf.status(), 200, 'Invoice PDF must download');
    assert.equal((await pdf.body()).subarray(0, 5).toString(), '%PDF-');
    await page.locator('input[type="file"]').setInputFiles({ name: 'demo-review-proof.pdf', mimeType: 'application/pdf', buffer: await pdf.body() });
    await page.getByRole('button', { name: 'Simpan dokumen', exact: true }).click();
    await page.getByRole('link', { name: 'demo-review-proof.pdf', exact: true }).waitFor();
    const evidenceHref = await page.getByRole('link', { name: 'demo-review-proof.pdf', exact: true }).getAttribute('href');
    const evidence = await context.request.get(base + evidenceHref);
    assert.equal(evidence.status(), 200, 'Private source evidence must download');
    assert.equal((await evidence.body()).subarray(0, 5).toString(), '%PDF-');
    await page.screenshot({ path: path.join(output, 'invoice-desktop.png'), fullPage: true });
    await page.goto(base + '/bills/');
    await page.locator('table a.table-link').first().click();
    await page.getByRole('heading', { name: 'Dokumen pendukung', exact: true }).waitFor();
    const report = await context.request.get(base + '/reports/document/');
    assert.equal(report.status(), 200, 'Monthly report PDF must download');
    assert.equal((await report.body()).subarray(0, 5).toString(), '%PDF-');
    await page.goto(base + '/tax/chat/');
    const question = page.locator('textarea, input[name="message"]').first();
    await question.fill('Apa beda PP 23 dan PPh 23?');
    const submit = page.locator('form').filter({ has: question }).locator('button[type="submit"]');
    await submit.click();
    await page.getByText(/Panduan tersimpan/).first().waitFor({ timeout: 10000 });
    await page.screenshot({ path: path.join(output, 'tax-chat.png'), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(base + '/');
    assert.equal(await page.locator('body').evaluate(node => node.scrollWidth > window.innerWidth + 2), false, 'Mobile dashboard overflow');
    await page.screenshot({ path: path.join(output, 'dashboard-mobile.png'), fullPage: true });
    await page.goto(base + invoicePath);
    assert.equal(await page.locator('body').evaluate(node => node.scrollWidth > window.innerWidth + 2), false, 'Mobile invoice overflow');
    await page.screenshot({ path: path.join(output, 'invoice-mobile.png'), fullPage: true });
    assert.deepEqual(errors, [], 'No uncaught browser errors');
    console.log(JSON.stringify({ passed: true, routes: pages.length + 3, chat: 'stored guidance response', documents: 'invoice PDF, monthly PDF, private upload/download', viewports: ['1440x1050', '390x844'], screenshots: output }, null, 2));
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
