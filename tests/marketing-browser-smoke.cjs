/* Runs only against the isolated synthetic QA workspace created by tmp/marketing_qa_fixture.py. */
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const { chromium } = require(process.env.OSEE_PLAYWRIGHT_PATH || 'playwright');

(async () => {
  const base = 'http://127.0.0.1:8002';
  const output = path.resolve('test-results/marketing');
  await fs.mkdir(output, { recursive: true });
  const browser = await chromium.launch({ channel: 'chrome', headless: true });
  const context = await browser.newContext({ storageState: '.local/marketing-qa/browser-state.json', viewport: { width: 1440, height: 1050 }, locale: 'id-ID' });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  const routes = ['', 'analysis/', 'analysis/?group_by=member', 'analysis/?group_by=ad', 'leads/', 'leads/new/', 'leads/1/', 'campaigns/', 'campaigns/new/', 'ads/new/', 'ads/import/', 'ads/1/correct/', 'members/', 'members/new/', 'advisor/', 'actions/1/', 'ai-policy/'];
  try {
    for (const width of [1440, 390]) {
      await page.setViewportSize({ width, height: width === 390 ? 844 : 1050 });
      for (const route of routes) {
        const response = await page.goto(`${base}/marketing/${route}`);
        assert.equal(response.status(), 200, `${width} ${route} should render`);
        assert.ok(await page.getByRole('heading', { level: 1 }).count(), route + ' visible heading');
        assert.ok(await page.getByText('OSEE · Data uji Marketing', { exact: false }).count(), 'QA must never use company workspace');
        const overflowing = await page.locator('body').evaluate(node => node.scrollWidth > innerWidth + 2);
        assert.equal(overflowing, false, `${width} ${route} viewport overflow`);
        if (['', 'analysis/', 'advisor/', 'leads/1/'].includes(route)) {
          await page.screenshot({ path: path.join(output, `${route.replaceAll('/', '-').replaceAll('?', '-') || 'overview'}-${width}.png`), fullPage: true });
        }
      }
    }
    await page.setViewportSize({ width: 1440, height: 1050 });
    await page.goto(base + '/marketing/analysis/');
    await page.locator('#id_group_by').selectOption('member');
    await page.getByRole('button', { name: 'Tampilkan analisis' }).click();
    await page.waitForURL(/group_by=member/);
    assert.ok(await page.getByText('Ayu · contoh', { exact: false }).count());

    await page.goto(base + '/marketing/advisor/');
    await page.locator('#id_question').fill('Apa prioritas tim agar cash-in meningkat?');
    await page.getByRole('button', { name: 'Minta saran', exact: true }).click();
    await page.getByText('Saran dari aturan aplikasi', { exact: true }).waitFor();
    await page.getByRole('link', { name: 'Jadikan tindakan →' }).first().click();
    const actionTitle = await page.locator('#id_title').inputValue();
    assert.ok(actionTitle.length > 5, 'Recommendation evidence should prefill actionable form');
    const owner = page.locator('#id_owner');
    if (await owner.count()) await owner.selectOption({ label: 'Ayu · contoh' });
    const dueDate = page.locator('#id_due_date');
    await dueDate.fill('2026-09-15');
    await page.locator('main form button[type="submit"]').last().click();
    await page.waitForURL(/\/marketing\/actions\/\d+\//);
    assert.ok(await page.getByRole('heading', { name: actionTitle, exact: false }).count());
    await page.locator('#id_status').selectOption('done');
    await page.locator('#id_outcome').fill('Uji browser: bukti diperiksa dan tindak lanjut dicatat. Ini bukan hasil perusahaan.');
    await page.locator('main form button[type="submit"]').last().click();
    assert.ok(await page.getByText('Uji browser: bukti diperiksa', { exact: false }).count());

    await page.goto(base + '/marketing/ads/1/correct/');
    assert.ok(await page.locator('#id_campaign').isDisabled());
    await page.locator('#id_spend').fill('91000');
    await page.locator('#id_reason').fill('Uji koreksi laporan pada database QA terisolasi.');
    await page.locator('main form button[type="submit"]').last().click();
    await page.waitForURL(base + '/marketing/campaigns/');
    await page.goto(base + '/marketing/ads/1/correct/');
    assert.equal(Number(await page.locator('#id_spend').inputValue()), 91000);

    const csv = await context.request.get(base + '/marketing/ads/template/');
    assert.equal(csv.status(), 200);
    assert.ok((await csv.text()).includes('tanggal,iklan,audiens,wilayah,biaya,tayangan,klik,sumber,referensi'));
    assert.deepEqual(errors, []);
    const result = { passed: true, routesPerViewport: routes.length, viewports: [1440, 390], workflows: ['dimension filter', 'local advice', 'verified recommendation to task', 'task outcome', 'audited ad correction', 'CSV template'], screenshots: output };
    await fs.writeFile(path.join(output, 'browser-results.json'), JSON.stringify(result, null, 2));
    console.log(JSON.stringify(result, null, 2));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
