/* Only runs against the synthetic server launched by marketing-performance-browser.py. */
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const { chromium } = require(process.env.OSEE_PLAYWRIGHT_PATH);

(async () => {
  const base = process.env.OSEE_QA_URL, output = process.env.OSEE_QA_DIR;
  assert.ok(base && output, 'Run through the isolated Python QA launcher.');
  const fixture = JSON.parse(await fs.readFile(path.join(output, 'fixture.json'), 'utf8'));
  const browser = await chromium.launch({ channel: 'chrome', headless: true });
  const result = { pages: [], flows: [], errors: [] };
  try {
    const owner = await browser.newContext({ storageState: path.join(output, 'owner-state.json'), viewport: { width: 1440, height: 1050 } });
    const marketer = await browser.newContext({ storageState: path.join(output, 'marketing-state.json'), viewport: { width: 1440, height: 1050 } });
    const replacementContext = await browser.newContext({ storageState: path.join(output, 'replacement-state.json'), viewport: { width: 390, height: 844 } });
    const page = await owner.newPage(), staff = await marketer.newPage(), replacement = await replacementContext.newPage();
    for (const tab of [page, staff, replacement]) tab.on('pageerror', error => result.errors.push(error.message));
    const routes = ['/marketing/', '/marketing/kpis/', '/marketing/kpis/new/', `/marketing/kpis/${fixture.target}/`,
      '/marketing/activities/', '/marketing/activities/new/', `/marketing/activities/${fixture.activity}/`,
      `/marketing/activities/${fixture.activity}/handoff/`, '/marketing/audit/',
      '/marketing/management/', `/marketing/kpis/${fixture.target}/sources/new/`, '/modules/', '/settings/get-started/'];
    for (const width of [1440, 390]) {
      await page.setViewportSize({ width, height: width === 390 ? 844 : 1050 });
      for (const route of routes) {
        const response = await page.goto(base + route);
        assert.equal(response.status(), 200, route);
        assert.ok(await page.getByRole('heading', { level: 1 }).count());
        assert.ok(await page.getByText('OSEE · Data uji KPI', { exact: false }).count());
        assert.equal(await page.locator('body').evaluate(node => node.scrollWidth > innerWidth + 2), false, `${route} overflow ${width}`);
        result.pages.push({ route, width, status: response.status() });
        if (route === '/marketing/kpis/' || route === '/marketing/audit/' || route === `/marketing/kpis/${fixture.target}/`)
          await page.screenshot({ path: path.join(output, route.replaceAll('/', '_') + width + '.png'), fullPage: true });
      }
    }
    await page.setViewportSize({ width: 1440, height: 1050 });
    await staff.goto(base + '/marketing/kpis/new/');
    for (const [name, value] of Object.entries({ title: 'Target dari browser', target: '4', period_start: fixture.start,
      period_end: fixture.end, rationale: 'Uji browser untuk target dan audit' })) await staff.locator(`[name="${name}"]`).fill(value);
    await staff.locator('[name="metric_key"]').selectOption('leads');
    await staff.locator('[name="channel"]').selectOption('sales');
    // Team scope differs from the pre-existing individual target.
    await staff.getByRole('button', { name: 'Simpan', exact: true }).click();
    await staff.waitForURL(/\/marketing\/kpis\/\d+\//);
    const targetUrl = staff.url();
    assert.equal(await staff.getByRole('button', { name: 'Setujui & aktifkan' }).count(), 0);
    await page.goto(targetUrl);
    await page.locator('[name="reason"]').fill('Target diperiksa direktur QA');
    await page.getByRole('button', { name: 'Setujui & aktifkan' }).click();
    assert.equal(await page.getByRole('button', { name: 'Simpan evaluasi' }).count(), 0);
    await page.getByRole('link', { name: 'Daftarkan seluruh sumber untuk diperiksa' }).click();
    await page.locator('[name="reference"]').fill('BROWSER-SOURCE-PERIOD');
    await page.locator('[name="inventory_note"]').fill('Seluruh sumber sintetis diperiksa; dua prospek di sumber.');
    await page.locator('[name="confirm_all_sources"]').check();
    await page.locator('[name="sources-0-kind"]').selectOption('leads');
    for (const [key, value] of Object.entries({ name: 'Pendaftaran sintetis lengkap', reference: 'FORM-ARCHIVE-1',
      expected_count: '2', imported_count: '2', evidence: 'Dua baris sumber cocok dengan data ERP.' }))
      await page.locator(`[name="sources-0-${key}"]`).fill(value);
    await page.getByRole('button', { name: 'Simpan daftar untuk diperiksa' }).click();
    await page.waitForURL(/\/marketing\/source-periods\/\d+\//);
    await page.locator('[name="reason"]').fill('Jumlah dan daftar seluruh sumber sudah cocok.');
    await page.getByRole('button', { name: 'Sahkan pemeriksaan sumber' }).click();
    await page.goto(targetUrl);
    await page.getByRole('button', { name: 'Simpan evaluasi' }).waitFor();
    await page.locator('[name="conclusion"]').fill('Hasil 2 dari 4 prospek; perlu tindak lanjut.');
    await page.getByRole('button', { name: 'Simpan evaluasi' }).click();
    await page.getByText('Sesuai dasar saat ini', { exact: true }).waitFor();
    result.flows.push('Marketing proposes KPI; director activates, reviews complete source inventory, and freezes evaluation');

    await page.goto(base + '/marketing/activities/new/');
    await page.locator('[name="title"]').fill('Tugas dari browser');
    await page.locator('[name="owner"]').selectOption(String(fixture.member));
    await page.locator('[name="kind"]').selectOption('follow_up');
    await page.locator('[name="channel"]').selectOption('sales');
    await page.locator('[name="due_date"]').fill(fixture.today);
    await page.locator('[name="description"]').fill('Hubungi prospek dan arsipkan hasilnya.');
    await page.getByRole('button', { name: 'Simpan', exact: true }).click();
    await page.waitForURL(/\/marketing\/activities\/\d+\//);
    const activityUrl = page.url();
    await staff.goto(activityUrl);
    await staff.getByRole('button', { name: 'Mulai mengerjakan' }).click();
    await staff.locator('[name="occurred_on"]').fill(fixture.today);
    await staff.locator('[name="evidence"]').fill('Arsip awal QA #1');
    await staff.locator('[name="outcome"]').fill('Jadwal pembicaraan dicatat.');
    await staff.getByRole('button', { name: 'Ajukan hasil & bukti' }).click();
    await staff.getByText('Menunggu pemeriksaan', { exact: true }).waitFor();
    assert.equal(await staff.getByRole('button', { name: 'Terima hasil' }).count(), 0);
    await page.goto(activityUrl);
    await page.locator('[name="review_notes"]').fill('Lengkapi referensi arsip hasil.');
    await page.getByRole('button', { name: 'Minta perbaikan' }).click();
    await page.getByText('Perlu perbaikan', { exact: true }).waitFor();
    await staff.goto(activityUrl);
    await staff.locator('[name="evidence"]').fill('Arsip revisi QA #2');
    await staff.locator('[name="outcome"]').fill('Referensi sudah dilengkapi.');
    await staff.getByRole('button', { name: 'Ajukan hasil & bukti' }).click();
    await page.goto(activityUrl);
    await page.locator('[name="review_notes"]').fill('Referensi diperiksa dan hasil diterima.');
    await page.getByRole('button', { name: 'Terima hasil' }).click();
    await page.getByText('Hasil pekerjaan diterima', { exact: true }).waitFor();
    await page.screenshot({ path: path.join(output, 'accepted-work.png'), fullPage: true });
    result.flows.push('Owner assignment, marketing evidence, director return, revision and independent acceptance');

    await page.getByRole('link', { name: 'Lihat seluruh riwayat pekerjaan' }).click();
    await page.locator('.m-audit-event').first().waitFor();
    assert.ok(await page.getByText('marketing.activity.accepted', { exact: false }).count());
    const downloadWait = page.waitForEvent('download');
    await page.getByRole('button', { name: 'Unduh audit CSV' }).click();
    const download = await downloadWait;
    const downloaded = path.join(output, 'audit.csv');
    await download.saveAs(downloaded);
    const csv = await fs.readFile(downloaded, 'utf8');
    for (const fragment of ['Arsip awal QA #1', 'Arsip revisi QA #2', 'qa_owner', 'qa_marketing']) assert.ok(csv.includes(fragment));
    result.flows.push('Scoped audit history and CSV retain original evidence, revisions and actors');
    await page.goto(base + `/marketing/activities/${fixture.activity}/`);
    await page.locator('[name="occurred_on"]').fill(fixture.today);
    await page.locator('[name="evidence"]').fill('Pengajuan owner untuk uji penarikan');
    await page.locator('[name="outcome"]').fill('Perlu diserahkan ke pelaksana.');
    await page.locator('form').filter({ has: page.locator('[name="evidence"]') }).locator('[name="responsibility_reason"]').fill('Direktur membantu pelaksanaan sementara sebelum menyerahkan ke PIC.');
    await page.getByRole('button', { name: 'Ajukan hasil & bukti' }).click();
    await page.getByText('Perlu pemeriksa yang berbeda', { exact: true }).waitFor();
    const withdrawal = page.getByText('Tarik pengajuan untuk diperbaiki', { exact: true });
    // The withdrawal form is deliberately secondary to the normal review queue.
    if (await withdrawal.evaluate(node => node.tagName) === 'SUMMARY') await withdrawal.click();
    else {
      const details = page.locator('details').filter({ hasText: 'Tarik pengajuan untuk diperbaiki' });
      if (await details.count()) await details.locator('summary').click();
    }
    await page.locator('[name="review_notes"]:visible').fill('Pelaksana akan menyiapkan bukti lengkap.');
    await page.getByRole('button', { name: 'Tarik pengajuan untuk diperbaiki' }).click();
    await page.getByRole('button', { name: 'Ajukan hasil & bukti' }).waitFor();
    assert.equal(await page.locator('[name="evidence"]').inputValue(), 'Pengajuan owner untuk uji penarikan');
    result.flows.push('Sole owner withdraws own submission with reason; evidence retained for staff revision');
    await page.getByRole('link', { name: 'Pindahkan ke staf lain', exact: true }).click();
    await page.getByRole('heading', { name: 'Pindahkan pekerjaan', exact: true }).waitFor();
    await page.setViewportSize({ width: 390, height: 844 });
    await page.reload();
    await page.locator('[name="member"]').selectOption(String(fixture.replacement_member));
    await page.locator('[name="reason"]').fill('Bima melanjutkan tugas karena Ayu mendapat tanggung jawab lain.');
    assert.equal(await page.locator('body').evaluate(node => node.scrollWidth > innerWidth + 2), false);
    await page.screenshot({ path: path.join(output, 'work-handoff-mobile.png'), fullPage: true });
    await page.getByRole('button', { name: 'Pindahkan pekerjaan', exact: true }).click();
    await page.waitForURL(new RegExp(`/marketing/activities/${fixture.activity}/$`));
    await replacement.goto(base + '/marketing/today/?queue=work');
    await replacement.getByRole('link', { name: 'Pekerjaan contoh terisolasi', exact: true }).click();
    assert.equal(await replacement.locator('[name="evidence"]').inputValue(), 'Pengajuan owner untuk uji penarikan');
    await replacement.getByRole('button', { name: 'Ajukan hasil & bukti' }).waitFor();
    assert.equal(await replacement.getByRole('link', { name: 'Pindahkan ke staf lain', exact: true }).count(), 0);
    await replacement.locator('details').filter({ hasText: 'PIC saat tugas dibuat dan riwayat perpindahan' }).locator('summary').click();
    await replacement.getByText('Bima melanjutkan tugas karena Ayu mendapat tanggung jawab lain.', { exact: true }).waitFor();
    await replacement.screenshot({ path: path.join(output, 'handed-work-mobile.png'), fullPage: true });
    await staff.goto(base + `/marketing/activities/${fixture.activity}/`);
    assert.equal(await staff.getByRole('button', { name: 'Ajukan hasil & bukti' }).count(), 0);
    await staff.goto(base + '/marketing/today/?queue=work');
    assert.equal(await staff.getByRole('link', { name: 'Pekerjaan contoh terisolasi', exact: true }).count(), 0);
    result.flows.push('Director hands unfinished work to mapped staff; mobile queue, old evidence, origin and handoff history remain; previous PIC loses execution');
    const denied = await staff.goto(base + '/');
    assert.equal(denied.status(), 403, 'Marketing remains outside Finance');
    result.flows.push('Marketing cannot access Finance');
    assert.deepEqual(result.errors, []);
    result.passed = true;
    await fs.writeFile(path.join(output, 'browser-results.json'), JSON.stringify(result, null, 2));
    console.log(JSON.stringify({ passed: true, pages: result.pages.length, flows: result.flows.length, output }));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
