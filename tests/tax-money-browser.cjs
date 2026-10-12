const fs=require('fs'),path=require('path'),assert=require('assert/strict');
const {chromium}=require(process.env.OSEE_PLAYWRIGHT_PATH);
const out=process.env.OSEE_QA_DIR,base=process.env.OSEE_QA_URL;
const f=JSON.parse(fs.readFileSync(path.join(out,'fixture.json'),'utf8'));
const result={flows:[],pages:[],errors:[]};
async function login(browser,credential){
 const context=await browser.newContext({viewport:{width:1440,height:1000}}),page=await context.newPage();
 page.on('pageerror',e=>result.errors.push(e.message));
 await page.goto(base+'/login/');await page.locator('#id_username').fill(credential.username);await page.locator('#id_password').fill(credential.password);
 await Promise.all([page.waitForURL(u=>!u.pathname.startsWith('/login/')),page.locator('button[type=submit]').click()]);
 return {context,page};
}
async function inspect(page,route){
 const response=await page.goto(base+route);
 const state=await page.evaluate(()=>({overflow:document.documentElement.scrollWidth>innerWidth+1,main:!!document.querySelector('main')}));
 result.pages.push({route,status:response.status(),width:page.viewportSize().width,...state});
 assert.equal(response.status(),200);assert.equal(state.overflow,false);assert.equal(state.main,true);
}
async function operation(page,route,values){
 await inspect(page,route);
 for(const [key,value] of Object.entries(values)){
  const field=page.locator(`[name=${key}]`);
  if(await field.evaluate(e=>e.tagName)==='SELECT')await field.selectOption(String(value));else await field.fill(String(value));
 }
 await page.locator('[name=confirmed]').check();
 await Promise.all([page.waitForURL(base+'/finance/movements/'),page.locator('main form button[type=submit]').click()]);
 result.flows.push(route);
}
async function official(page){
 await inspect(page,`/tax/obligations/${f.final_obligation}/verify/`);
 await page.locator('[name=kind]').selectOption('payment');await page.locator('[name=evidence]').selectOption(String(f.official_evidence));
 for(const [key,value] of Object.entries({reference:'BROWSER-SYNTHETIC-NTPN',taxpayer_reference:'1234567890123456',period:'2026-09',amount:'5000',note:'Synthetic official identity period and amount matched'}))await page.locator(`[name=${key}]`).fill(value);
 await page.locator('[name=matches_confirmed]').check();
 await page.locator('main form button[type=submit]').click();await page.waitForLoadState('load');
}
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.OSEE_CHROME_PATH,headless:true});
 try{
  let {context,page}=await login(browser,f.normal_credentials.finance);
  await operation(page,`/finance/tax/${f.credit}/customer-receipt/`,{transaction:f.net_bank,amount:'490000',evidence:'Synthetic transfer and verified customer certificate matched'});
  for(const width of [1440,390]){await page.setViewportSize({width,height:1000});await inspect(page,`/sales/${f.invoice}/`);await inspect(page,`/tax/obligations/${f.credit}/`);await page.screenshot({path:path.join(out,`net-receipt-${width}.png`),fullPage:true});}
  await page.setViewportSize({width:1440,height:1000});
  await inspect(page,`/tax/obligations/${f.late_credit}/`);
  assert.match(await page.locator('main').innerText(),/Catat bukti potong untuk pembayaran lama/);
  await operation(page,`/finance/tax/${f.late_credit}/credit-after-payment/`,{source_allocation:f.late_allocation,date:'2026-10-01',evidence:'Synthetic late certificate received after original bank reconciliation'});
  for(const width of [1440,390]){await page.setViewportSize({width,height:1000});await inspect(page,`/sales/${f.late_invoice}/`);await inspect(page,`/tax/obligations/${f.late_credit}/`);assert.match(await page.locator('main').innerText(),/tidak ditambahkan lagi/);await inspect(page,'/finance/movements/');assert.match(await page.locator('main').innerText(),/Uang bank sebelumnya/);await page.screenshot({path:path.join(out,`late-credit-${width}.png`),fullPage:true});}
  await context.close();
  ({context,page}=await login(browser,f.final_credentials.owner));
  await operation(page,`/finance/tax/${f.final_obligation}/recognize/`,{date:f.day,evidence:'Synthetic reviewed final tax expense recognition'});
  const paymentLink=page.locator('a[href*="/finance/tax/recognized/"]').first();const paymentRoute=await paymentLink.getAttribute('href');
  await operation(page,paymentRoute,{transaction:f.first_bank,amount:'2000',evidence:'Synthetic first partial tax transfer matched'});
  await inspect(page,`/tax/obligations/${f.final_obligation}/`);assert.match(await page.locator('main').innerText(),/Pembayaran belum diverifikasi/);
  await official(page);assert.match(await page.locator('main').innerText(),/Cocokkan setoran bank/);result.flows.push('official proof rejected while bank payment partial');
  await operation(page,paymentRoute,{transaction:f.second_bank,amount:'3000',evidence:'Synthetic remaining tax transfer matched'});
  await official(page);await page.waitForURL(base+`/tax/obligations/${f.final_obligation}/`);assert.match(await page.locator('main').innerText(),/Bukti pembayaran diverifikasi/);result.flows.push('official proof matched after full bank reconciliation');
  for(const width of [1440,390]){await page.setViewportSize({width,height:1000});await inspect(page,`/tax/obligations/${f.final_obligation}/`);await inspect(page,'/finance/movements/');await page.screenshot({path:path.join(out,`final-tax-${width}.png`),fullPage:true});}
  await context.close();
  ({context,page}=await login(browser,f.final_credentials.marketing));
  assert.equal((await page.goto(base+`/finance/tax/${f.final_obligation}/recognize/`)).status(),403);result.flows.push('marketing financial write forbidden');await context.close();
 }finally{fs.writeFileSync(path.join(out,'browser-results.json'),JSON.stringify(result,null,2));await browser.close();}
 assert.equal(result.errors.length,0);console.log(JSON.stringify({flows:result.flows,pages:result.pages.length,scriptErrors:result.errors},null,2));
})().catch(e=>{console.error(e.stack);process.exit(1);});
