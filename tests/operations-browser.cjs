const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const { chromium } = require(process.env.OSEE_PLAYWRIGHT_PATH);
(async () => {
  const base = process.env.OSEE_QA_URL, out = process.env.OSEE_QA_DIR;
  assert.ok(base && out, 'Use isolated launcher');
  const f = JSON.parse(await fs.readFile(path.join(out, 'fixture.json'), 'utf8'));
  const browser = await chromium.launch({channel: 'chrome', headless: true});
  const results = {pages: [], flows: [], errors: []};
  try {
    const ctx = await browser.newContext({storageState: path.join(out, 'owner-state.json')});
    const page = await ctx.newPage();
    page.on('pageerror', error => results.errors.push(error.message));
    const routes = ['/operations/', '/operations/orders/new/', `/operations/orders/${f.order}/`,
      '/operations/sessions/', '/operations/intake/', `/sales/${f.invoice}/units/`, `/sales/${f.invoice}/costs/`];
    for (const width of [1440, 390]) {
      await page.setViewportSize({width, height: 900});
      for (const route of routes) {
        const response = await page.goto(base + route);
        assert.equal(response.status(), 200, route);
        assert.ok(await page.getByRole('heading', {level: 1}).count());
        assert.equal(await page.locator('body').evaluate(n => n.scrollWidth > innerWidth + 2), false, `${route} overflow ${width}`);
        results.pages.push({route, width});
      }
    }
    await page.setViewportSize({width: 1440, height: 1000});
    await page.goto(base + `/operations/lines/${f.line}/register/`);
    for (const [key, value] of Object.entries({name: 'Peserta browser QA', source: 'FORM-QA', external_id: 'QA-PARTICIPANT'}))
      await page.locator(`[name="${key}"]`).fill(value);
    await page.getByRole('button', {name: 'Simpan', exact: true}).click();
    await page.waitForURL(/\/operations\/registrations\/\d+\/$/);
    const registration = page.url();
    await page.getByRole('link', {name: 'Jadwalkan / pindah sesi'}).click();
    await page.locator('[name="session"]').selectOption(String(f.session));
    await page.locator('[name="reason"]').fill('Jadwal dikonfirmasi dalam uji');
    await page.getByRole('button', {name: 'Simpan', exact: true}).click();
    await page.getByRole('link', {name: 'Catat kehadiran'}).click();
    await page.locator('[name="attended"]').selectOption('yes');
    await page.locator('[name="evidence"]').fill('Daftar hadir sintetis');
    await page.getByRole('button', {name: 'Simpan', exact: true}).click();
    await page.getByRole('link', {name: 'Konfirmasikan layanan selesai'}).click();
    await page.locator('[name="date"]').fill(f.day);
    await page.locator('[name="evidence"]').fill('Bukti tes sintetis selesai');
    await page.getByRole('button', {name: 'Simpan', exact: true}).click();
    await page.waitForURL(registration);
    assert.ok(await page.getByText('Layanan selesai ·', {exact: false}).count());
    results.flows.push('Register, schedule, attendance, one-unit completion on prepaid group');
    await page.screenshot({path: path.join(out, 'participant-completed.png'), fullPage: true});
    await page.goto(base + '/operations/intake/');
    await page.locator('[name="source"]').fill('FORM-CSV-QA');
    await page.locator('[name="file"]').setInputFiles({name: 'synthetic.csv', mimeType: 'text/csv', buffer: Buffer.from('id,name\nQA-CSV,Peserta CSV QA\n')});
    await page.getByRole('button', {name: 'Unggah untuk diperiksa'}).click();
    await page.waitForURL(/\/intake\/\d+\/map\//);
    await page.locator('[name="external_id"]').selectOption('0');
    await page.locator('[name="name"]').selectOption('1');
    await page.getByRole('button', {name: 'Simpan', exact: true}).click();
    await page.locator('[name="row_ids"]').check();
    await page.locator('[name="order"]').selectOption(String(f.new_order));
    await page.locator('[name="product"]').selectOption(String(f.product));
    await page.locator('[name="reason"]').fill('Identitas dan pembeli diperiksa');
    await page.locator('[name="confirm_identity"]').check();
    await page.locator('[name="operation"][value="accept"]').click();
    await page.goto(base + `/operations/orders/${f.new_order}/`);
    await page.getByRole('link', {name: 'Periksa & terbitkan invoice'}).click();
    await page.locator('[name="date"]').fill(f.day);
    await page.locator('[name="due_date"]').fill(f.day);
    await page.getByRole('button', {name: 'Tampilkan ulang harga'}).click();
    assert.ok(await page.getByText('Harga per unit', {exact: false}).count());
    await page.locator('[name="confirm"]').check();
    await page.getByRole('button', {name: 'Konfirmasi & terbitkan'}).click();
    await page.waitForURL(/\/sales\/\d+\/$/);
    results.flows.push('CSV source mapping, selected-row confirmation, order invoice price preview');
    const restricted = await browser.newContext({storageState: path.join(out, 'marketing-state.json')});
    const staff = await restricted.newPage();
    assert.equal((await staff.goto(registration)).status(), 403);
    results.flows.push('Marketing denied private participant details');
    assert.deepEqual(results.errors, []);
  } finally {
    await fs.writeFile(path.join(out, 'results.json'), JSON.stringify(results, null, 2));
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
