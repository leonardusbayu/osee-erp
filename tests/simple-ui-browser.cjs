/* Run only through the isolated simple-ui-browser.py launcher. */
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const { chromium } = require(process.env.OSEE_PLAYWRIGHT_PATH);

(async () => {
  const base = process.env.OSEE_QA_URL, output = process.env.OSEE_QA_DIR;
  assert.ok(base && output && process.env.OSEE_QA_PASSWORD, 'Use the synthetic launcher.');
  const fixture = JSON.parse(await fs.readFile(path.join(output, 'fixture.json'), 'utf8'));
  const browser = await chromium.launch({ channel: 'chrome', headless: true });
  const results = { pages: [], flows: [], errors: [] };
  try {
    const pages = {};
    const landing = { owner: '/director/', finance: '/', marketing: '/marketing/today/' };
    for (const role of Object.keys(landing)) {
      const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
      // The test never contacts a provider or external site.
      await context.route('**/*', route => new URL(route.request().url()).origin === new URL(base).origin
        ? route.continue() : route.abort());
      const page = await context.newPage();
      page.on('pageerror', error => results.errors.push(error.message));
      pages[role] = page;
      assert.equal((await page.goto(base + '/login/')).status(), 200);
      await page.getByLabel('Nama akun', { exact: true }).fill(fixture.roles[role]);
      await page.getByLabel('Kata sandi', { exact: true }).fill(process.env.OSEE_QA_PASSWORD);
      await page.getByRole('button', { name: /Masuk ke OSEE/ }).click();
      await page.waitForURL(base + landing[role]);
      const primary = page.locator('.main-nav > a');
      const count = await primary.count();
      assert.ok(count > 0 && count <= 6, `${role}: ${count} primary links`);
      const labels = await primary.allTextContents();
      for (const text of labels) {
        const label = text.trim().replace(/\s+/g, ' ');
        assert.ok(label.length > 0 && label.length <= 35, `${role}: long/empty label ${label}`);
        assert.doesNotMatch(label, /fingerprint|SLA|attribution|multi.dimensi/i);
      }
      results.flows.push(`${role} login opens ${landing[role]} with ${count} short primary menu labels`);
    }

    const moduleHomes = { owner: 'Direktur', finance: 'Finance', marketing: 'Marketing' };
    for (const [role, home] of Object.entries(moduleHomes)) {
      const page = pages[role];
      assert.equal((await page.goto(base + '/modules/')).status(), 200);
      await page.screenshot({ path: path.join(output, `modules-picker-${role}-desktop.png`), fullPage: true });
      assert.equal(await page.locator('.workspace-breadcrumb strong').textContent(), home,
        `${role}: workspace breadcrumb matches the role's primary module`);
      assert.equal(await page.locator('.module-switch strong').textContent(), home,
        `${role}: sidebar module name matches the role's primary module`);
      const primaryCard = page.locator('.module-primary');
      assert.equal(await primaryCard.locator('h2').textContent(), home, `${role}: role-specific primary module`);
      assert.equal(await primaryCard.getByRole('link', { name: new RegExp(`Mulai di ${home}`) }).count(), 1);
      const otherModules = page.locator('details.module-others');
      if (role === 'marketing') {
        assert.equal(await otherModules.count(), 0, 'Marketing has no unrelated module choices');
        assert.equal(await page.locator('.module-primary h2').allTextContents().then(items => items.join()), 'Marketing');
        assert.equal(await page.locator('h2').filter({ hasText: /^(Finance|Direktur)$/ }).count(), 0);
      } else {
        assert.equal(await otherModules.count(), 1);
        assert.equal(await otherModules.evaluate(node => node.open), false, `${role}: secondary modules start closed`);
        assert.equal(await otherModules.locator('.module-card').first().isVisible(), false,
          `${role}: secondary modules do not compete with the main task`);
        assert.deepEqual((await otherModules.locator('.module-card h2').allTextContents()).sort(),
          (role === 'owner'
            ? ['Finance', 'Marketing', 'Pesanan & Peserta', 'SDM & Penggajian']
            : ['Direktur', 'Marketing', 'Pesanan & Peserta', 'SDM & Penggajian']).sort());
        const summary = otherModules.locator('summary');
        await summary.focus();
        await summary.press('Enter');
        assert.equal(await otherModules.evaluate(node => node.open), true, `${role}: secondary modules open by keyboard`);
        assert.equal(await otherModules.locator('.module-card').first().isVisible(), true);
        await summary.press('Space');
        assert.equal(await otherModules.evaluate(node => node.open), false, `${role}: secondary modules close by keyboard`);
      }
      if (role === 'owner') {
        await page.setViewportSize({ width: 390, height: 844 });
        await page.waitForFunction(() => document.querySelector('#sidebar').getBoundingClientRect().right <= 1);
        assert.equal(await page.locator('body').evaluate(node => node.scrollWidth > innerWidth + 2), false,
          'Module picker fits the mobile viewport');
        await page.screenshot({ path: path.join(output, 'modules-owner-mobile.png'), fullPage: true });
        await page.setViewportSize({ width: 1440, height: 1000 });
      }
    }
    results.flows.push('Module picker puts the role-specific home first, keeps other authorized modules collapsed, and fits mobile');

    const finance = pages.finance;
    await finance.goto(base + '/bank/');
    const menu = finance.locator('.main-nav > details.nav-more');
    const summary = menu.locator('summary');
    assert.equal(await menu.evaluate(node => node.open), false);
    await summary.focus();
    await summary.press('Space');
    assert.equal(await menu.evaluate(node => node.open), true, 'Native summary opens with Space');
    await summary.press('Enter');
    assert.equal(await menu.evaluate(node => node.open), false, 'Native summary closes with Enter');
    results.flows.push('Menu lainnya opens and closes with the keyboard');

    for (const [role, route] of [['finance', '/settings/get-started/'], ['finance', '/people/'], ['owner', '/director/readiness/'],
      ['marketing', '/marketing/members/']]) {
      const page = pages[role];
      assert.equal((await page.goto(base + route)).status(), 200);
      const active = page.locator('.main-nav details.nav-more a[aria-current="page"]');
      assert.equal(await active.count(), 1, `${role}: one active secondary destination`);
      assert.equal(await active.evaluate(node => node.closest('details').open), true, `${role}: active destination is expanded`);
      if (role === 'finance' && route === '/people/') {
        assert.equal(await page.getByRole('heading', { name: 'Belum ada payroll', exact: true }).count(), 1,
          'An empty payroll starts without invented salary data');
        assert.equal(await active.evaluate(node => getComputedStyle(node).display), 'flex',
          'Secondary links remain readable as full menu rows');
        assert.equal(await page.locator('.people-guide li').count(), 4,
          'Payroll instructions are separate ordered steps');
        const safety = page.locator('details.people-safety');
        assert.equal(await safety.evaluate(node => node.open), false);
        await safety.locator('summary').click();
        assert.match(await safety.innerText(), /Aplikasi belum menghitung atau menebak nominal/);
        await safety.locator('summary').click();
        await page.screenshot({ path: path.join(output, 'people-payroll-empty-desktop.png'), fullPage: true });
        await page.setViewportSize({ width: 390, height: 844 });
        await page.waitForFunction(() => document.querySelector('#sidebar').getBoundingClientRect().right <= 1);
        assert.equal(await page.locator('#sidebar').evaluate(node => node.classList.contains('is-open')), false,
          'Finance sidebar stays closed by default on mobile');
        assert.equal(await page.getByRole('button', { name: 'Buka navigasi', exact: true }).getAttribute('aria-expanded'), 'false');
        const sidebarRight = await page.locator('#sidebar').evaluate(node => node.getBoundingClientRect().right);
        assert.ok(sidebarRight <= 1, `Closed mobile sidebar must stay outside the viewport (right=${sidebarRight})`);
        assert.equal(await page.locator('body').evaluate(node => node.scrollWidth > innerWidth + 2), false,
          'Payroll home fits a mobile viewport without horizontal scrolling');
        await page.screenshot({ path: path.join(output, 'people-payroll-empty-mobile.png') });
        await page.setViewportSize({ width: 1440, height: 1000 });
        await page.getByRole('link', { name: 'Tambah orang', exact: true }).click();
        await page.waitForURL(base + '/people/people/new/');
        assert.equal(await page.getByRole('heading', { name: 'Tambah karyawan atau tutor', exact: true }).count(), 1);
        assert.equal(await page.locator('#id_name').count(), 1);
        assert.equal(await page.locator('#id_kind').count(), 1);
        await page.getByRole('link', { name: 'Batal', exact: true }).click();
        await page.waitForURL(base + '/people/');
      }
    }
    results.flows.push('Current secondary destination is already expanded for every workspace');

    // Exercise the real shared script with an existing bank matching form.
    await finance.goto(base + '/bank/');
    const bankDetails = finance.locator('details.allocation-details').first();
    assert.equal(await bankDetails.evaluate(node => node.open), false);
    const invoice = bankDetails.locator('select[required]');
    assert.equal(await invoice.evaluate(node => node.reportValidity()), false);
    assert.equal(await bankDetails.evaluate(node => node.open), true, 'Invalid required input reveals its optional section');
    assert.equal(await invoice.isVisible(), true);
    assert.match(await bankDetails.locator('label').first().textContent(), /wajib|\*/);
    results.flows.push('Required bank input reveals its section and required label without submitting money');

    await finance.goto(base + '/sales/new/');
    const specialPrice = finance.locator('#special-price');
    assert.equal(await specialPrice.evaluate(node => node.open), false);
    assert.equal(await finance.locator('#id_service_date').isVisible(), true, 'Ordinary service date remains visible');
    assert.equal(await specialPrice.locator('#id_unit_price').isVisible(), false, 'Special pricing starts secondary');
    await specialPrice.locator('summary').focus();
    await specialPrice.locator('summary').press('Enter');
    assert.equal(await specialPrice.locator('#id_unit_price').isVisible(), true);
    assert.equal(await specialPrice.locator('#id_price_override').isVisible(), true, 'Approval stays beside special price');
    results.flows.push('Standard invoice is task-first; special pricing and approval open with the keyboard');

    await finance.goto(base + '/bank/?status=unmatched');
    const outgoing = finance.locator('tr.bank-transaction-row').filter({ hasText: 'QA-UI-OUT' });
    const outgoingAction = outgoing.locator('xpath=following-sibling::tr[1]');
    assert.equal(await outgoingAction.locator('select[required]').count(), 0, 'No unusable empty bill selector');
    assert.match(await outgoingAction.textContent(), /Belum ada tagihan/);
    assert.equal(await outgoingAction.locator('a[href="/bills/"]').count(), 1, 'Empty bill state gives a next step');
    const incoming = finance.locator('tr.bank-transaction-row').filter({ hasText: 'QA-UI-BANK' });
    const incomingAction = incoming.locator('xpath=following-sibling::tr[1]');
    await incomingAction.locator('details summary').click();
    await incomingAction.locator('select[name="invoice"]').selectOption(String(fixture.invoice));
    // The transfer is larger than this invoice. Rejection must preserve what staff entered.
    await incomingAction.locator('input[name="amount"]').fill('510000');
    await incomingAction.getByRole('button', { name: 'Simpan alokasi', exact: true }).click();
    const failedIncoming = finance.locator('tr.bank-transaction-row').filter({ hasText: 'QA-UI-BANK' });
    const failedIncomingAction = failedIncoming.locator('xpath=following-sibling::tr[1]');
    assert.equal(await failedIncomingAction.locator('details').evaluate(node => node.open), true);
    assert.equal(await failedIncomingAction.locator('select[name="invoice"]').inputValue(), String(fixture.invoice));
    assert.equal(await failedIncomingAction.locator('input[name="amount"]').inputValue(), '510000');
    assert.ok(await failedIncomingAction.locator('[role="alert"]').count(), 'Matching error is beside the form');
    assert.ok((await failedIncomingAction.locator('form').getAttribute('action')).includes('status=unmatched'));
    await finance.screenshot({ path: path.join(output, 'bank-match-error-desktop.png'), fullPage: true });
    await finance.setViewportSize({ width: 390, height: 844 });
    await finance.waitForFunction(() => document.querySelector('#sidebar').getBoundingClientRect().right <= 1);
    const bankScroll = finance.locator('.table-wrap').filter({ has: finance.locator('.bank-table') });
    assert.equal(await bankScroll.evaluate(node => node.scrollWidth > node.clientWidth + 2), false,
      'Bank matching on mobile must not require horizontal table scrolling');
    assert.equal(await failedIncomingAction.locator('td[data-label]').count(), 1);
    await failedIncomingAction.locator('input[name="amount"]').scrollIntoViewIfNeeded();
    const amountBox = await failedIncomingAction.locator('input[name="amount"]').boundingBox();
    assert.ok(amountBox.x >= 0 && amountBox.x + amountBox.width <= 392);
    await finance.screenshot({ path: path.join(output, 'bank-match-error-mobile.png'), fullPage: true });
    await finance.setViewportSize({ width: 1440, height: 1000 });
    results.flows.push('No eligible bill gives a next step; rejected overpayment retains amount, invoice, filter and expanded form');

    await finance.goto(base + '/bank/import/');
    await finance.locator('select[name="account"]').selectOption(String(fixture.account));
    await finance.locator('input[type="file"]').setInputFiles({ name: 'qa-statement.csv', mimeType: 'text/csv',
      buffer: Buffer.from(`date,reference,description,amount\n${fixture.today},QA-PREVIEW-ONLY,Synthetic receipt,250000.00\n`) });
    await finance.getByRole('button', { name: 'Lanjut ke pemetaan kolom', exact: true }).click();
    await finance.waitForURL(/\/bank\/statements\/\d+\//);
    assert.equal(await finance.locator('input[name="no_transactions"]').count(), 0, 'Populated source has no empty-period checkbox');
    assert.equal(await finance.locator('select[name="date"]').isVisible(), true);
    assert.equal(await finance.locator('select[name="reference_mode"]').isVisible(), true);
    const rowRange = finance.locator('input[name="first_row"]').locator('xpath=ancestor::details');
    assert.equal(await rowRange.evaluate(node => node.open), false);
    await rowRange.locator('summary').focus();
    await rowRange.locator('summary').press('Enter');
    assert.equal(await finance.locator('input[name="first_row"]').isVisible(), true);
    await rowRange.locator('summary').click();
    await finance.getByRole('button', { name: 'Tampilkan pratinjau', exact: true }).click();
    assert.equal(await finance.locator('input[name="preview_token"]').count(), 1, 'Mapping still requires explicit confirm after preview');
    assert.equal(await finance.getByRole('button', { name: '3. Konfirmasi impor', exact: true }).isVisible(), true);
    await finance.setViewportSize({ width: 390, height: 844 });
    await finance.waitForFunction(() => document.querySelector('#sidebar').getBoundingClientRect().right <= 1);
    assert.equal(await finance.locator('body').evaluate(node => node.scrollWidth > innerWidth + 2), false);
    await finance.screenshot({ path: path.join(output, 'bank-preview-mobile.png'), fullPage: true });
    await finance.setViewportSize({ width: 1440, height: 1000 });
    results.flows.push('Grouped statement mapping works by keyboard and on mobile; preview does not import money');

    await finance.goto(base + '/bank/import/');
    await finance.locator('select[name="account"]').selectOption(String(fixture.account));
    await finance.locator('input[type="file"]').setInputFiles({ name: 'qa-empty-statement.csv', mimeType: 'text/csv',
      buffer: Buffer.from('date,reference,description,amount\n') });
    await finance.getByRole('button', { name: 'Lanjut ke pemetaan kolom', exact: true }).click();
    await finance.waitForURL(/\/bank\/statements\/\d+\//);
    assert.equal(await finance.locator('select:visible').count(), 0, 'Empty source does not ask for nonexistent columns');
    await finance.getByRole('button', { name: 'Tampilkan pratinjau', exact: true }).click();
    assert.equal(await finance.locator('input[name="preview_token"]').count(), 0, 'Empty source still requires explicit attestation');
    await finance.locator('input[name="no_transactions"]').check();
    await finance.getByRole('button', { name: 'Tampilkan pratinjau', exact: true }).click();
    assert.equal(await finance.locator('input[name="preview_token"]').count(), 1);
    await finance.setViewportSize({ width: 390, height: 844 });
    await finance.waitForFunction(() => document.querySelector('#sidebar').getBoundingClientRect().right <= 1);
    await finance.screenshot({ path: path.join(output, 'bank-empty-preview-mobile.png'), fullPage: true });
    await finance.setViewportSize({ width: 1440, height: 1000 });
    results.flows.push('Empty bank source needs explicit attestation and preview before a separate import confirmation');

    const routes = {
      finance: ['/', '/sales/', '/sales/new/', '/bills/', '/bills/new/', '/bank/', '/bank/import/', '/reports/',
        '/tax/', '/tax/annual/', '/settings/get-started/', '/modules/'],
      owner: ['/director/', '/director/decisions/new/', '/director/budgets/new/', '/director/readiness/', '/settings/tax/', '/modules/'],
      marketing: ['/marketing/today/', '/modules/'],
    };
    const screenshots = new Map([
      ['marketing:/marketing/today/:390', 'marketing-daily-mobile.png'],
      ['finance:/bank/:1440', 'finance-bank-desktop.png'],
      ['owner:/director/:1440', 'director-desktop.png'],
      ['finance:/settings/get-started/:390', 'onboarding-mobile.png'],
    ]);
    for (const width of [1440, 390]) {
      for (const [role, destinations] of Object.entries(routes)) {
        const page = pages[role];
        await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
        for (const route of destinations) {
          const response = await page.goto(base + route);
          assert.equal(response.status(), 200, `${role} ${route}`);
          assert.ok(await page.getByRole('heading', { level: 1 }).count(), `${role} ${route}: heading`);
          assert.ok(await page.getByText(fixture.organization, { exact: true }).count(), `${role} ${route}: synthetic workspace`);
          assert.equal(await page.locator('body').evaluate(node => node.scrollWidth > innerWidth + 2), false,
            `${role} ${route} overflows at ${width}px`);
          const file = screenshots.get(`${role}:${route}:${width}`);
          if (file) await page.screenshot({ path: path.join(output, file), fullPage: true });
          results.pages.push({ role, route, width, status: response.status() });
        }
      }
    }
    const staff = pages.marketing;
    await staff.goto(base + '/marketing/today/');
    const urgent = staff.locator('.daily-attention');
    assert.equal(await urgent.isVisible(), true);
    const overdueLink = urgent.locator('a[href*="queue=overdue"]');
    assert.equal(await overdueLink.isVisible(), true, 'Urgent alternate work is visible without opening filters');
    await overdueLink.click();
    assert.equal(await staff.locator('.daily-tasks a[href="/marketing/leads/' + fixture.overdue + '/"]').first().isVisible(), true);
    results.flows.push('Marketing sees and opens overdue alternate work without opening filters');

    await staff.goto(base + `/marketing/leads/${fixture.appointment}/`);
    assert.match(await staff.locator('.next-appointment').textContent(), /11:45 WIB/);
    const leadSummary = staff.locator('.lead-summary-edit');
    assert.equal(await leadSummary.evaluate(node => node.open), false);
    await leadSummary.locator('summary').click();
    await leadSummary.locator('textarea[name="notes"]').fill('Catatan terbaru dari uji browser');
    await leadSummary.getByRole('button', { name: 'Simpan ringkasan', exact: true }).click();
    assert.equal(await staff.locator('.lead-summary-edit [role="alert"]').count(), 0, 'Summary actually saves without correction errors');
    assert.match(await staff.locator('.next-appointment').textContent(), /11:45 WIB/);
    assert.equal(await staff.locator('textarea[name="notes"]').inputValue(), 'Catatan terbaru dari uji browser');
    await staff.screenshot({ path: path.join(output, 'lead-appointment-mobile.png'), fullPage: true });
    results.flows.push('Editing summary notes preserves the visible exact appointment after reload');

    await staff.goto(base + `/marketing/leads/${fixture.appointment}/contacts/new/`);
    assert.equal(await staff.getByText('Wajib bila Hasil kontak dipilih Tidak lanjut.', { exact: true }).count(), 1);
    assert.equal(await staff.getByText(/^Wajib bila memilih Tetapkan jadwal baru\./).count(), 1);
    await staff.locator('select[name="outcome"]').selectOption('lost');
    await staff.locator('select[name="followup_action"]').selectOption('set');
    await staff.locator('textarea[name="summary"]').fill('Hasil kontak sintetis tanpa alasan dan jadwal');
    await staff.locator('textarea[name="evidence"]').fill('Bukti sintetis pemeriksaan formulir');
    await staff.locator('form button[type="submit"]').click();
    assert.equal(await staff.getByText('Pilih alasan prospek tidak lanjut.', { exact: true }).count(), 1);
    assert.equal(await staff.getByText('Isi tanggal dan jam janji berikutnya.', { exact: true }).count(), 1);
    results.flows.push('Contact form explains conditional fields and rejects missing reason/schedule without changing the appointment');

    await staff.goto(base + '/marketing/today/');
    const counts = staff.locator('details').filter({ has: staff.getByText('Lihat jumlah pekerjaan menurut jenis', { exact: true }) });
    assert.equal(await counts.evaluate(node => node.open), false, 'Aggregate counts stay secondary on mobile');
    assert.equal(await counts.locator('.m-accountability-links a').first().isVisible(), false);
    await counts.locator('summary').click();
    assert.equal(await counts.locator('.m-accountability-links a').first().isVisible(), true, 'Counts appear when requested');
    await counts.locator('summary').click();
    const taskScroll = staff.locator('.m-table-scroll').filter({ has: staff.locator('.daily-tasks') });
    const scroll = await taskScroll.evaluate(node => ({ left: node.scrollLeft, width: node.scrollWidth, visible: node.clientWidth }));
    assert.equal(scroll.left, 0);
    assert.ok(scroll.width <= scroll.visible + 2, 'Daily work does not require horizontal table scrolling');
    const firstTitle = staff.locator('.daily-tasks tbody tr').first().locator('td').first().getByRole('link', { name: fixture.lead_name, exact: true });
    await firstTitle.scrollIntoViewIfNeeded();
    assert.equal(await firstTitle.isVisible(), true);
    const titleBox = await firstTitle.boundingBox();
    assert.ok(titleBox.x >= 0 && titleBox.x + titleBox.width <= 392, 'Work title is inside the mobile viewport');
    await firstTitle.click();
    await staff.waitForURL(base + `/marketing/leads/${fixture.lead}/`);
    results.flows.push('Mobile staff opens a listed prospect without horizontal scrolling; aggregate counts appear only on request');
    await staff.goto(base + '/marketing/today/');
    const toggle = staff.getByRole('button', { name: 'Buka navigasi', exact: true });
    assert.equal(await toggle.getAttribute('aria-expanded'), 'false');
    await toggle.click();
    assert.equal(await toggle.getAttribute('aria-expanded'), 'true');
    await staff.keyboard.press('Escape');
    assert.equal(await toggle.getAttribute('aria-expanded'), 'false');
    results.flows.push('Mobile navigation opens and Escape closes it');
    assert.equal((await staff.goto(base + '/bank/')).status(), 403, 'Marketing stays outside Finance');
    assert.equal((await staff.goto(base + '/people/')).status(), 403, 'Marketing cannot access payroll or employee records');
    results.flows.push('Simplified menus preserve Marketing access boundaries');
    assert.deepEqual(results.errors, []);
    results.passed = true;
    await fs.writeFile(path.join(output, 'browser-results.json'), JSON.stringify(results, null, 2));
    console.log(JSON.stringify({ passed: true, pages: results.pages.length, flows: results.flows.length, evidence: output }));
  } catch (error) {
    results.passed = false;
    results.failure = error.stack || error.message;
    await fs.writeFile(path.join(output, 'browser-results.json'), JSON.stringify(results, null, 2));
    throw error;
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error.message); process.exitCode = 1; });
