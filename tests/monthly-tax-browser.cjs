const fs = require('fs'), path = require('path'), assert = require('assert/strict');
const {chromium} = require(process.env.OSEE_PLAYWRIGHT_PATH);
const out = process.env.OSEE_QA_DIR, base = process.env.OSEE_QA_URL;
const fixture = JSON.parse(fs.readFileSync(path.join(out, 'fixture.json'), 'utf8'));
const result = {flows: [], pages: [], errors: []};

async function login(browser, credential, width) {
  const context = await browser.newContext({viewport: {width, height: 1000}, acceptDownloads: true});
  const page = await context.newPage();
  page.on('pageerror', error => result.errors.push(error.message));
  await page.goto(base + '/login/');
  await page.locator('#id_username').fill(credential.username);
  await page.locator('#id_password').fill(credential.password);
  await Promise.all([page.waitForURL(url => !url.pathname.startsWith('/login/')), page.locator('button[type=submit]').click()]);
  return {context, page};
}

async function inspect(page, route, status = 200) {
  const response = await page.goto(base + route);
  const state = await page.evaluate(() => ({overflow: document.documentElement.scrollWidth > innerWidth + 1, main: !!document.querySelector('main')}));
  result.pages.push({route, status: response.status(), width: page.viewportSize().width, ...state});
  assert.equal(response.status(), status);
  assert.equal(state.overflow, false);
  if (status === 200) assert.equal(state.main, true);
}

async function submit(page, button) {
  await Promise.all([page.waitForNavigation({waitUntil: 'load'}), button.click()]);
}

async function prepare(page, month) {
  await inspect(page, '/tax/monthly/sources/?period=' + month);
  await page.locator('[name=period]').fill(month);
  await submit(page, page.getByRole('button', {name: 'Siapkan data bulan ini', exact: true}));
  assert.match(page.url(), /\/tax\/monthly\/sources\/\d+\/$/);
  return new URL(page.url()).pathname;
}

async function reportFields(page, evidence) {
  await page.locator('[name=inventory_note]').fill('Synthetic all products, mitra, direct customers, channels and branches reviewed');
  await page.locator('[name=completeness_confirmed]').check();
  await page.locator('[name=evidence]').selectOption(String(evidence));
  await page.locator('[name=professional_name]').fill('Synthetic browser professional');
  await page.locator('[name=qualification_reference]').fill('Synthetic engagement for this monthly source version');
  await page.locator('[name=reviewed_on]').fill('2026-10-09');
  await page.locator('[name=statement]').fill('Synthetic report confirms each source, actual supported delta and monthly completeness');
  await page.locator('[name=report_confirmed]').check();
  await page.locator('[name=rule_reference]').fill('Synthetic reviewed applicable fiscal period rules');
  if (await page.locator('[name=self_payment_only_confirmed]').count()) {
    await page.locator('[name=self_payment_only_confirmed]').check();
    await page.locator('[name=object_code]').fill('411128-420');
  }
  if (await page.locator('[name=prior_history_confirmed]').count()) await page.locator('[name=prior_history_confirmed]').check();
}

async function sourceChoices(page, rows) {
  await page.locator('[name=positive_reason]').fill('Synthetic documented same treatment for positive income');
  await page.locator('[name=proposed_confirmed]').check();
  for (let index = 0; index < rows.length; index++) {
    const choice = page.locator(`[name=source_${index}_decision]`);
    await choice.evaluate(element => { element.closest('details').open = true; });
    await choice.selectOption(rows[index].proposed_inclusion ? 'include' : 'exclude');
    await page.locator(`[name=source_${index}_reason]`).fill(rows[index].proposed_inclusion ? '' : 'Synthetic context already represented in revenue; not assessed again');
  }
}

async function sourceCsv(page, route, fileName) {
  await inspect(page, route);
  const download = await Promise.all([page.waitForEvent('download'), page.getByRole('link', {name: 'Unduh sumber CSV'}).click()]);
  const target = path.join(out, fileName);
  await download[0].saveAs(target);
  const text = fs.readFileSync(target, 'utf8');
  assert.match(text, /BUKAN SPT/);
  assert.match(text, /source_hash/);
  return text;
}

(async () => {
  const browser = await chromium.launch({executablePath: process.env.OSEE_CHROME_PATH, headless: true});
  try {
    for (const width of [1440, 390]) {
      const f = fixture.fixtures[String(width)];
      let {context, page} = await login(browser, f.final.credentials.finance, width);
      const sourceRoute = await prepare(page, '2026-09');
      assert.match(await page.locator('main').innerText(), /Belum ditentukan/);
      await inspect(page, sourceRoute + 'review/', 403);
      await context.close();
      ({context, page} = await login(browser, f.final.credentials.owner, width));
      await inspect(page, sourceRoute + 'review/');
      await sourceChoices(page, f.final.rows);
      await reportFields(page, f.final.evidence);
      const add = page.getByRole('button', {name: 'Tambah penyesuaian', exact: true});
      await page.evaluate(() => {
        for (let count = 0; count < 25; count++) {
          document.querySelector('[data-add-adjustment]').click();
          const rows = document.querySelectorAll('[data-adjustment]');
          rows[rows.length - 1].querySelector('[data-remove-adjustment]').click();
        }
      });
      assert.equal(await page.locator('[name="adjustments-TOTAL_FORMS"]').inputValue(), '0');
      await add.focus();
      await page.keyboard.press('Enter');
      await page.locator('[name=adjustments-0-kind]').selectOption('correction');
      await page.locator('[name=adjustments-0-amount]').fill('123');
      await page.locator('[data-adjustment]').first().getByRole('button', {name: 'Hapus penyesuaian ini'}).click();
      assert.equal(await page.locator('[data-adjustment]').count(), 0);
      await add.click();
      await page.locator('[name=adjustments-0-kind]').selectOption('gross_basis');
      await page.locator('[name=adjustments-0-amount]').fill('40000');
      await page.locator('[name=adjustments-0-reason]').fill('Synthetic supported contract gross delta, not catalogue upsell');
      await page.locator('[name=adjustments-0-evidence]').selectOption(String(f.final.evidence));
      await page.locator(`[name="adjustments-0-source_row_ids"][value="${f.final.candidate}"]`).check();
      // A server validation error must preserve the packed per-row selections and actual delta.
      await page.locator('[name=statement]').fill('');
      await page.locator('[name=statement]').evaluate(element => element.removeAttribute('required'));
      await submit(page, page.getByRole('button', {name: 'Simpan keputusan sesuai laporan'}));
      assert.match(page.url(), /\/review\/$/);
      assert.equal(await page.locator('[name=adjustments-0-amount]').inputValue(), '40000');
      assert.equal(await page.locator('[name=professional_name]').inputValue(), 'Synthetic browser professional');
      assert.equal(await page.locator('[data-adjustment]').count(), 1);
      for (let index = 0; index < f.final.rows.length; index++) assert.equal(await page.locator(`[name=source_${index}_decision]`).inputValue(), f.final.rows[index].proposed_inclusion ? 'include' : 'exclude');
      result.flows.push({width, flow: 'compact choices, report and add/remove adjustment survive error'});
      await page.locator('[name=statement]').fill('Synthetic report confirms the exact source package, evidenced gross delta and self-payment only');
      await submit(page, page.getByRole('button', {name: 'Simpan keputusan sesuai laporan'}));
      assert.equal(new URL(page.url()).pathname, sourceRoute);
      await inspect(page, sourceRoute);
      await page.screenshot({path: path.join(out, `approved-${width}.png`), fullPage: true});
      await submit(page, page.getByRole('button', {name: 'Buat draf pajak', exact: true}));
      const draftRoute = new URL(page.url()).pathname;
      assert.match(draftRoute, /\/tax\/obligations\/\d+\/$/);
      await inspect(page, sourceRoute);
      await submit(page, page.getByRole('button', {name: 'Buat draf pajak', exact: true}));
      assert.equal(new URL(page.url()).pathname, draftRoute);
      result.flows.push({width, flow: 'Finance prepare -> Owner report -> exact reviewed final draft, repeat same draft'});
      await inspect(page, `/tax/monthly/sources/${fixture.foreign_package}/`, 404);
      await context.close();
      ({context, page} = await login(browser, f.final.credentials.marketing, width));
      await inspect(page, sourceRoute, 403);
      await inspect(page, sourceRoute + 'review/', 403);
      result.flows.push({width, flow: 'wrong company, Finance approval and Marketing access denied'});
      await context.close();
      ({context, page} = await login(browser, f.final.credentials.finance, width));
      await inspect(page, '/sales/new/');
      await page.locator('[name=party]').selectOption(String(f.final.party));
      await page.locator('[name=product]').selectOption(String(f.final.product));
      await page.locator('[name=quantity]').fill('1');
      for (const key of ['date', 'due_date', 'service_date']) await page.locator(`[name=${key}]`).fill('2026-09-20');
      await submit(page, page.locator('main form button[type=submit]').last());
      await submit(page, page.locator('form[action$="/issue/"] button[type=submit]'));
      const oldCsv = await sourceCsv(page, sourceRoute, `history-${width}.csv`);
      assert.match(oldCsv, /Data berubah/);
      await submit(page, page.getByRole('button', {name: 'Siapkan versi terbaru'}));
      assert.notEqual(new URL(page.url()).pathname, sourceRoute);
      assert.match(await page.locator('main').innerText(), /Versi 2/);
      await page.getByText('Riwayat versi bulan ini', {exact: true}).click();
      assert.equal(await page.locator(`a[href="${sourceRoute}"]`).count(), 1);
      result.flows.push({width, flow: 'backdated issued invoice -> stale source -> version2 with old CSV/history retained'});
      const nilRoute = await prepare(page, '2026-08');
      assert.match(await page.locator('main').innerText(), /Belum ditentukan/);
      await context.close();
      ({context, page} = await login(browser, f.final.credentials.owner, width));
      await inspect(page, nilRoute + 'review/');
      await reportFields(page, f.final.evidence);
      await page.locator('[name=nil_confirmed]').check();
      await submit(page, page.getByRole('button', {name: 'Simpan keputusan sesuai laporan'}));
      assert.equal(new URL(page.url()).pathname, nilRoute);
      assert.match(await page.locator('main').innerText(), /Nilai dari keputusan yang disetujui/);
      result.flows.push({width, flow: 'empty extraction remains unknown until explicitly reviewed genuine nil'});
      await context.close();
      ({context, page} = await login(browser, f.normal.credentials.finance, width));
      const normalRoute = await prepare(page, '2026-09');
      await context.close();
      ({context, page} = await login(browser, f.normal.credentials.owner, width));
      await inspect(page, normalRoute + 'review/');
      await reportFields(page, f.normal.evidence);
      await page.locator('[name=nil_confirmed]').check();
      await submit(page, page.getByRole('button', {name: 'Simpan keputusan sesuai laporan'}));
      assert.equal(new URL(page.url()).pathname, normalRoute);
      assert.match(await page.locator('main').innerText(), /Tidak berlaku/);
      assert.equal(await page.getByRole('button', {name: 'Buat draf pajak', exact: true}).count(), 0);
      await inspect(page, normalRoute);
      await page.screenshot({path: path.join(out, `normal-nil-${width}.png`), fullPage: true});
      result.flows.push({width, flow: 'normal-regime reviewed package has no invented monthly final tax'});
      await context.close();
      ({context, page} = await login(browser, f.correction.credentials.finance, width));
      const originalReview = await prepare(page, '2026-09');
      await inspect(page, originalReview + 'correction/', 403);
      await context.close();
      ({context, page} = await login(browser, f.correction.credentials.owner, width));
      await inspect(page, originalReview + 'review/');
      await sourceChoices(page, f.correction.rows);
      await reportFields(page, f.correction.evidence);
      await submit(page, page.getByRole('button', {name: 'Simpan keputusan sesuai laporan'}));
      await submit(page, page.getByRole('button', {name: 'Buat draf pajak', exact: true}));
      const oldDraft = new URL(page.url()).pathname;
      await inspect(page, originalReview);
      assert.equal(await page.getByRole('link', {name: 'Perbaiki hasil pemeriksaan', exact: true}).count(), 0);
      await page.getByRole('link', {name: 'Tarik draf yang keliru', exact: true}).click();
      await page.locator('[name=reason]').fill('Synthetic browser corrected gross contract classification');
      await page.locator('[name=confirmed]').check();
      await submit(page, page.getByRole('button', {name: 'Tarik draf dan simpan alasan'}));
      assert.equal(new URL(page.url()).pathname, oldDraft);
      assert.match(await page.locator('main').innerText(), /Draf ini sudah ditarik/);
      await inspect(page, originalReview);
      await page.getByRole('link', {name: 'Perbaiki hasil pemeriksaan', exact: true}).click();
      const correctionRoute = new URL(page.url()).pathname;
      await inspect(page, correctionRoute);
      await page.locator('[name=reason]').fill('Synthetic browser corrected gross contract classification');
      await page.locator('[name=confirmed]').evaluate(element => element.removeAttribute('required'));
      await submit(page, page.getByRole('button', {name: 'Siapkan pemeriksaan baru'}));
      assert.equal(new URL(page.url()).pathname, correctionRoute);
      assert.equal(await page.locator('[name=reason]').inputValue(), 'Synthetic browser corrected gross contract classification');
      await page.locator('[name=confirmed]').check();
      await submit(page, page.getByRole('button', {name: 'Siapkan pemeriksaan baru'}));
      const correctedReview = new URL(page.url()).pathname.replace(/review\/$/, '');
      assert.notEqual(correctedReview, originalReview);
      assert.match(await page.locator('main').innerText(), /Versi 2/);
      await sourceChoices(page, f.correction.rows);
      await reportFields(page, f.correction.evidence);
      await page.getByRole('button', {name: 'Tambah penyesuaian', exact: true}).click();
      await page.locator('[name=adjustments-0-kind]').selectOption('gross_basis');
      await page.locator('[name=adjustments-0-amount]').fill('40000');
      await page.locator('[name=adjustments-0-reason]').fill('Synthetic corrected professional report: supported contract gross delta');
      await page.locator('[name=adjustments-0-evidence]').selectOption(String(f.correction.evidence));
      await page.locator(`[name="adjustments-0-source_row_ids"][value="${f.correction.candidate}"]`).check();
      await submit(page, page.getByRole('button', {name: 'Simpan keputusan sesuai laporan'}));
      assert.equal(new URL(page.url()).pathname, correctedReview);
      await inspect(page, correctedReview);
      assert.match(await page.locator('main').innerText(), /Pendapatan yang dimasukkan: Rp500\.000/);
      assert.match(await page.locator('main').innerText(), /Perbedaan nilai kontrak \/ bruto: Rp40\.000/);
      assert.match(await page.locator('main').innerText(), /Jumlah dasar sebelum pembulatan: Rp540\.000/);
      await page.getByText('Lihat perlakuan setiap sumber', {exact: true}).click();
      assert.match(await page.locator('main').innerText(), /Tidak dihitung pada bulan ini/);
      await page.screenshot({path: path.join(out, `corrected-review-${width}.png`), fullPage: true});
      await submit(page, page.getByRole('button', {name: 'Buat draf pajak', exact: true}));
      const replacementDraft = new URL(page.url()).pathname;
      assert.notEqual(replacementDraft, oldDraft);
      await inspect(page, correctedReview);
      await submit(page, page.getByRole('button', {name: 'Buat draf pajak', exact: true}));
      assert.equal(new URL(page.url()).pathname, replacementDraft);
      const oldReviewCsv = await sourceCsv(page, originalReview, `corrected-history-${width}.csv`);
      assert.match(oldReviewCsv, /Versi sudah digantikan/);
      assert.match(oldReviewCsv, /taxable_turnover,500000/);
      assert.equal(await page.getByRole('button', {name: 'Buat draf pajak', exact: true}).count(), 0);
      result.flows.push({width, flow: 'explicit withdrawal -> reasoned unchanged-source revision -> corrected 500k+40k bridge -> one active replacement draft'});
      await context.close();
    }
  } finally {
    fs.writeFileSync(path.join(out, 'browser-results.json'), JSON.stringify(result, null, 2));
    await browser.close();
  }
  assert.equal(result.errors.length, 0);
  console.log(JSON.stringify({flows: result.flows, pages: result.pages.length, scriptErrors: result.errors}));
})().catch(error => {console.error(error.stack); process.exit(1);});
