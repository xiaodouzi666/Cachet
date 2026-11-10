const fs = require("node:fs/promises");
const {constants} = require("node:fs");
const path = require("node:path");
const {spawn} = require("node:child_process");
const {performance} = require("node:perf_hooks");
const {createRequire} = require("node:module");
const root = path.resolve(__dirname, "..");
const fromFrontend = createRequire(path.join(root,"frontend","package.json"));
const {expect} = fromFrontend("@playwright/test");

function check(value, message) { if (!value) throw Error(message); }
function origin(value) {
  const u = new URL(value);
  check(!u.username && !u.password && u.pathname === "/" && !u.search && !u.hash, "A plain configured origin is required");
  check(u.protocol === "https:" || (u.protocol === "http:" && ["localhost","127.0.0.1","[::1]"].includes(u.hostname)), "Require HTTPS or loopback");
  return u.origin;
}
async function privateJson(file) {
  const f = await fs.open(file, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    const st = await f.stat();
    check(st.isFile() && (st.mode & 0o777) === 0o600 && (!process.getuid || st.uid === process.getuid()), "The recording journal must be an owned 0600 file");
    return JSON.parse(await f.readFile("utf8"));
  } finally { await f.close(); }
}
async function binding({baseURL, journalPath, rehearsal, rehearsalActions}, env=process.env) {
  check(typeof rehearsal === "boolean","Set rehearsal explicitly for the recording");
  const prodWeb=origin(env.PUBLIC_WEB_URL), prodAPI=origin(env.PUBLIC_API_URL);
  const changingPreview = rehearsal && rehearsalActions === true;
  const designated = changingPreview ? env.CACHET_SOAK_REHEARSAL_JOURNAL : env.CACHET_SOAK_JOURNAL;
  check(designated && journalPath && await fs.realpath(designated) === await fs.realpath(journalPath),"Select the designated private recording journal");
  const web = changingPreview ? origin(env.CACHET_REHEARSAL_WEB_URL) : prodWeb;
  const api = changingPreview ? origin(env.CACHET_REHEARSAL_API_URL) : prodAPI;
  check(origin(baseURL) === web,"Browser origin does not match the designated recording web origin");
  if (!rehearsal) check(web.startsWith("https://") && api.startsWith("https://"),"Formal recording requires production HTTPS");
  const j = await privateJson(journalPath);
  check(j.scenario === "operator-live-soak-v3" && j.origin === api && j.web_origin === web &&
        j.workspace_id && j.mandate_id && j.workspace_cookie && j.latest?.workspace_id === j.workspace_id &&
        j.latest?.mandate_id === j.mandate_id && j.latest?.paypal_order_id && j.latest?.authorization_id,
        "The journal does not identify this recording workspace and mandate");
  if (changingPreview) {
    check(web !== prodWeb && api !== prodAPI && env.CACHET_SOAK_JOURNAL,"Use a separate rehearsal deployment and journal");
    check(path.resolve(env.CACHET_SOAK_JOURNAL) !== path.resolve(journalPath),"The production journal cannot fund a rehearsal");
    let prod;
    try{prod=await privateJson(env.CACHET_SOAK_JOURNAL);}catch(e){if(e.code!=="ENOENT")throw e;}
    if(prod)check(prod.workspace_id !== j.workspace_id && prod.mandate_id !== j.mandate_id && prod.latest?.authorization_id !== j.latest.authorization_id,
      "The rehearsal must have its own workspace, mandate and authorization");
  }
  return {web,api,ws:j.workspace_id,mid:j.mandate_id,cookie:j.workspace_cookie,order:j.latest.paypal_order_id,
    auth:j.latest.authorization_id,runnerStopped:Boolean(j.latest.authorization_get && j.latest.measurement_status),faultRefundIds:j.latest.fault_refund_ids || [],journalPath:path.resolve(journalPath),canAct:!rehearsal || changingPreview};
}
async function get(page, route) {
  const resp=await page.request.get("/api/"+route);
  check(resp.ok(),"Workspace readback failed for "+route.split("?")[0]+" ("+resp.status()+")");
  return resp.json();
}
function provider(data) {
  return new Promise((resolve,reject)=>{
    const proc=spawn(process.env.CACHET_PYTHON || path.join(root,".venv","bin","python"),[path.join(__dirname,"soak_readback.py")],{cwd:root,stdio:["pipe","pipe","ignore"]});
    let raw="";proc.stdout.on("data",x=>{raw+=x.toString();});
    proc.on("error",()=>reject(Error("Could not start the sandbox readback helper")));
    proc.on("close",code=>{ if(code) return reject(Error("PayPal did not confirm the recording receipts"));try{resolve(JSON.parse(raw));}catch{reject(Error("Invalid sandbox readback"));}});
    proc.stdin.end(JSON.stringify(data));
  });
}
async function liveWorkspace(page,b) {
  const [s,m,h]=await Promise.all([get(page,"judge/state"),get(page,"mandates/"+b.mid),get(page,"health")]);
  check(s.workspace_id === b.ws && s.mandate?.id === b.mid && s.operator_soak_mandate_id === b.mid && m.id === b.mid &&
    m.paypal_order_id === b.order && m.authorization_id === b.auth && m.cap_micros === 5_000_000,"Unexpected workspace or recording authorization");
  b.privateTexts=[s.sandbox_buyer_email,s.sandbox_buyer_password].filter(Boolean);
  return {mandate:m, mode:h.mode};
}
async function cashEvidence(page,b) {
  const [raw,refunds]=await Promise.all([get(page,"captures"),get(page,"refunds")]);
  const caps=raw.filter(c=>c.mandate_id===b.mid), ids=new Set(caps.map(c=>c.id));
  const matched=refunds.filter(f=>ids.has(f.capture_id) && b.faultRefundIds.includes(f.paypal_refund_id));
  const pp=await provider({order:b.order,authorization:b.auth,captures:caps.map(c=>c.paypal_capture_id),refunds:matched.map(f=>f.paypal_refund_id)});
  for(const cap of caps){
    const raw=pp.captures.find(c=>c.id===cap.paypal_capture_id);
    check(raw && cap.amount_cents===raw.amount_cents && cap.fee_cents===raw.fee_cents && cap.net_cents===raw.net_cents,"Capture fees or net differ from PayPal");
  }
  for(const f of matched)check(pp.refunds.some(x=>x.id===f.paypal_refund_id && x.amount_cents===f.amount_cents),"Refund amount differs from PayPal");
  return {caps, pp};
}
async function writeOnce(file, content){
  const tmp=file+"."+require("node:crypto").randomUUID()+".tmp";
  const handle=await fs.open(tmp,"wx",0o600);
  try{
    await handle.writeFile(JSON.stringify(content));await handle.sync();await handle.close();
    try{await fs.link(tmp,file);}
    catch(e){if(e.code==="EEXIST")return false;throw e;}
    const dir=await fs.open(path.dirname(file),"r");try{await dir.sync();}finally{await dir.close();}
    return true;
  }finally{await handle.close().catch(()=>{});await fs.rm(tmp,{force:true});}
}
async function intent(b,scene,before){
  const file=b.journalPath+"."+scene+"-action.json";
  const content={scene,origin:b.web,api:b.api,workspace_id:b.ws,mandate_id:b.mid,started_at:new Date().toISOString(),...before};
  if(await writeOnce(file,content))return {fresh:true,prior:content};
  return {fresh:false,prior:await existingIntent(b,scene)};
}
function actionEvent(cams,label,key){
  const t=performance.now(), utc=new Date().toISOString();
  for(const cam of cams)cam.events.push({label,ms:Math.round(t-cam.start),utc,actionKey:key});
}
function checkDisplayedMoney(text, micros){
  const match = text.match(/(?:\$|USD\s*)?(-?[\d,]+(?:\.\d+)?)/);
  check(match,"No measured monetary value in the selected field");
  const value = match[1].replaceAll(",",""), digits = value.includes(".") ? value.split(".")[1].length : 0;
  check(Number((micros / 1_000_000).toFixed(digits)) === Number(value), "Displayed amount differs from the confirmed receipt");
}
async function go(cam,route){
  const res=await cam.page.goto(route+"?rec=1",{waitUntil:"domcontentloaded"});
  check(res?.ok(),"The scene page did not load");
  await cam.page.evaluate(()=>document.fonts.ready);
}
async function mark(cam,label) {
  const visible = await cam.page.locator("body").innerText();
  check(!(cam.binding.privateTexts || []).some(text=>visible.includes(text)),"A private buyer credential is visible on the recording page");
  const ms=Math.round(performance.now()-cam.start);
  const name=cam.scene+"-soak-"+label+".png";
  await cam.page.screenshot({path:path.join(cam.dir,name),mask:[cam.page.locator("input[type=password], [data-sensitive], [data-private]")]});
  cam.events.push({label,ms,screenshot:name});
}
async function cameras(browser,b,dirs,scene){
  const cams=[];
  try{
    for(const dir of dirs){
      await fs.mkdir(dir,{recursive:true});
      const scratch=await fs.mkdtemp(path.join(dir,".soak-"));
      const context=await browser.newContext({baseURL:b.web,viewport:{width:1920,height:1080},deviceScaleFactor:1,
        locale:"en-US",recordVideo:{dir:scratch,size:{width:1920,height:1080}}});
      const cam={dir,scratch,context,scene,binding:b,events:[],page:null,start:0};
      cams.push(cam);
      await context.addCookies([{name:"cachet_workspace",value:b.cookie,url:b.web,secure:b.web.startsWith("https:"),httpOnly:true,sameSite:"Lax"}]);
      await context.addInitScript(()=>{
        const hide=()=>{if(!document.head)return;const s=document.createElement("style");s.textContent="input[type=password],[data-sensitive],[data-private]{visibility:hidden!important}";document.head.append(s);};
        if(document.head)hide();else document.addEventListener("DOMContentLoaded",hide,{once:true});
      });
      cam.start=performance.now();
      cam.page=await context.newPage();
    }
    return cams;
  }catch(e){await Promise.allSettled(cams.map(c=>c.context.close()));throw e;}
}
async function finish(cam,success){
  const video=cam.page.video();
  try{
    await cam.context.close();
    if(success)await video.saveAs(path.join(cam.dir,cam.scene+"-soak.webm"));
  }finally{
    if(video)await video.delete().catch(()=>{});
    await fs.rm(cam.scratch,{recursive:true,force:true});
  }
}
async function tab(page,name){
  const nav=page.getByRole("navigation",{name:"Settlement statements"});
  await nav.getByRole("button",{name,exact:true}).click();
}
async function capturesShot(cams,b){
  const {caps,pp}=await cashEvidence(cams[0].page,b);
  const threshold=caps.filter(c=>c.final===false && c.amount_cents>=200);
  check(threshold.length===2,"Two actual threshold captures are required for S5");
  await Promise.all(cams.map(async(cam,index)=>{
    await go(cam,"/settlement");await tab(cam.page,"Captures");
    const ordered=index===0?threshold:threshold.slice().reverse();
    for(const c of ordered){
      const row=cam.page.getByRole("row").filter({has:cam.page.locator('[col-id="captures.paypal_capture_id"]').filter({hasText:c.paypal_capture_id})}).first();
      await expect(row).toBeVisible();
      await row.scrollIntoViewIfNeeded();
      for(const field of ["amount_cents","fee_cents","net_cents"]){
        const cell=row.locator('[col-id="captures.'+field+'"]');
        await expect(cell).toBeVisible();checkDisplayedMoney(await cell.innerText(),c[field]*10000);
      }
      await mark(cam,"capture-"+c.paypal_capture_id);
    }
  }));
  return {workspaceId:b.ws,mandateId:b.mid,thresholdCaptures:pp.captures.filter(x=>threshold.some(c=>c.paypal_capture_id===x.id))};
}
async function waitFor(page, callback, timeout=150000){
  let value;
  await expect.poll(async()=>{ value=await callback();return Boolean(value); },{timeout,intervals:[350,1000,2000]}).toBe(true);
  return value;
}
async function existingIntent(b,scene){
  try {
    const saved=await privateJson(b.journalPath+"."+scene+"-action.json");
    check(saved.scene===scene && saved.workspace_id===b.ws && saved.mandate_id===b.mid && saved.origin===b.web && saved.api===b.api,
      "Checkpoint belongs to another recording");
    return saved;
  }catch(e){if(e.code==="ENOENT")return null;throw e;}
}
async function closingShot(cams,b){
  if(b.canAct){
    check(b.runnerStopped,"Finish the measurement window and retain the ACTIVE mandate before filming its close");
    const {pp}=await cashEvidence(cams[0].page,b);
    check(pp.refunds.length>0,"A verified refund from an actually captured fault is required before closing");
    const f=pp.refunds[0];
    await Promise.all(cams.map(async cam=>{
      await go(cam,"/settlement");await tab(cam.page,"Refunds");
      const row=cam.page.getByRole("row").filter({has:cam.page.locator('[col-id="refunds.paypal_refund_id"]').filter({hasText:f.id})}).first();
      await expect(row).toBeVisible();await row.scrollIntoViewIfNeeded();
      checkDisplayedMoney(await row.locator('[col-id="refunds.amount_cents"]').innerText(),f.amount_cents*10000);
      await mark(cam,"recorded-fault-refund");
    }));
  }
  await Promise.all(cams.map(c=>go(c,"/agent")));
  const before=await liveWorkspace(cams[0].page,b), previous=await existingIntent(b,"S6");
  await Promise.all(cams.map(c=>mark(c,"mandate-before")));
  if(!b.canAct && before.mandate.status==="ACTIVE")
    return {workspaceId:b.ws,mandateId:b.mid,action:"read-only rehearsal",observedStatus:"ACTIVE",pendingMicros:before.mandate.accrued_uncaptured_micros};
  if(b.canAct && !previous){
    check(before.mandate.status==="ACTIVE","An uncaptured close cannot be recorded after the mandate has already closed");
    const button=cams[0].page.getByRole("button",{name:"Close mandate",exact:true});
    await expect(button).toBeEnabled();
    const claim=await intent(b,"S6",{authorization_id:b.auth,pending_micros:before.mandate.accrued_uncaptured_micros});
    if(claim.fresh){
      const route="/api/mandates/"+b.mid+"/close";
      const [reply]=await Promise.all([
        cams[0].page.waitForResponse(r=>new URL(r.url()).pathname===route && r.request().method()==="POST",{timeout:120000}),
        cams[0].page.waitForRequest(r=>new URL(r.url()).pathname===route && r.method()==="POST").then(()=>actionEvent(cams,"original-close-submitted",b.auth)),
        button.click(),
      ]);
      check(reply.ok(),"The original mandate close was not confirmed; resume its checkpoint");
    }
  }
  const m=await waitFor(cams[0].page,async()=>{const x=(await liveWorkspace(cams[0].page,b)).mandate;return ["CLOSED","VOIDED","EXPIRED"].includes(x.status)&&x;});
  const {pp}=await cashEvidence(cams[0].page,b);
  check(["CAPTURED","VOIDED","EXPIRED"].includes(pp.authorization_status) &&
    m.captured_micros===pp.captures.reduce((sum,c)=>sum+c.amount_cents*10000,0) && m.released_micros===m.cap_micros-m.captured_micros,"The original authorization release is unconfirmed");
  check(pp.refunds.length>0,"S6 needs a PayPal-confirmed refund from a captured soak fault");
  await Promise.all(cams.map(async c=>{
    await go(c,"/agent"); await expect(c.page.getByTestId("money-forgiven")).toBeVisible();
    await expect(c.page.getByTestId("money-released")).toBeVisible();
    checkDisplayedMoney(await c.page.getByTestId("money-forgiven").innerText(),m.forgiven_micros);
    checkDisplayedMoney(await c.page.getByTestId("money-released").innerText(),m.released_micros);
    await c.page.getByTestId("money-released").scrollIntoViewIfNeeded();
    await mark(c,"mandate-closed");
  }));
  return {workspaceId:b.ws,mandateId:b.mid,action:!b.canAct?"observed closed mandate":previous?"verified existing close":"verified close",provider:pp,
    capturedMicros:m.captured_micros,forgivenMicros:m.forgiven_micros,releasedMicros:m.released_micros};
}
function safeId(id){check(typeof id==="string"&&/^[A-Za-z0-9_-]+$/.test(id),"Invalid provider identifier");return id;}
async function explainRoyalties(cam){
  const page=cam.page;
  await tab(page,"Royalties");
  for(const name of ["Linear","Shapley"]){
    const choice=page.getByRole("button",{name,exact:true});
    await choice.click();await expect(choice).toHaveAttribute("aria-pressed","true");
    await mark(cam,name.toLowerCase()+"-attribution");
  }
  await page.getByRole("button",{name:/Ops Copilot/}).click();
  const question="How much did Starlette earn this week, and why more per token than FastAPI?";
  await page.getByRole("textbox",{name:"Message input"}).fill(question);
  const [answer]=await Promise.all([page.waitForResponse(r=>new URL(r.url()).pathname==="/api/copilot" && r.request().method()==="POST",{timeout:120000}),
    page.getByRole("button",{name:"Send message"}).click()]);
  const body=await answer.text();check(answer.ok()&&body.includes("event: done")&&!body.includes("event: error"),"Copilot did not finish the workspace question");
  const deltas=body.split(/\r?\n\r?\n/).filter(x=>x.startsWith("event: delta")).map(x=>JSON.parse(x.split(/\r?\ndata: /)[1]).text);
  const text=deltas.join("").trim();check(text,"No grounded answer in this workspace");
  await expect(page.getByText(text,{exact:false}).last()).toBeVisible();
  await mark(cam,"copilot-royalty-answer");
}
async function savedBatch(b, batchId){
  const file=b.journalPath+".S7-receipt.json";
  if(batchId)await writeOnce(file,{batchId,workspace:b.ws,mandate:b.mid});
  let value;
  try{value=await privateJson(file);}catch(e){if(e.code==="ENOENT")return null;throw e;}
  check(value.workspace===b.ws && value.mandate===b.mid && (!batchId || value.batchId===batchId),
    "The original batch receipt is bound to another action");
  return value.batchId;
}
async function payoutShot(cams,b){
  const page=cams[0].page;
  const current=(await liveWorkspace(page,b)).mandate;
  await Promise.all(cams.map(c=>go(c,"/settlement")));
  const original=await existingIntent(b,"S7");
  if(!b.canAct && !original){
    await Promise.all(cams.map(c=>mark(c,"settlement-preflight")));
    await Promise.all(cams.map(explainRoyalties));
    return {workspaceId:b.ws,mandateId:b.mid,action:"read-only rehearsal",observedStatus:current.status};
  }
  check(current.status==="CLOSED","Record the completed soak mandate close before its payout");
  const contexts=await get(page,"contexts");
  const payees={};
  for(const slug of ["starlette","fastapi"]){
    const c=contexts.find(x=>x.slug===slug && x.can_manage);
    check(c?.publisher_id,"Both registered owners must exist in the recording workspace");
    payees[c.publisher_id]=slug;
  }
  check(Object.keys(payees).length===2,"Two distinct publishers are required");
  let previous=original;
  if(!previous){
    const before=(await get(page,"payouts")).map(x=>x.id);
    const button=page.getByRole("button",{name:"Settle now",exact:true});
    await expect(button).toBeEnabled();await button.click();
    const confirm=page.getByRole("button",{name:"Confirm sandbox payout",exact:true});
    await expect(confirm).toBeEnabled();await mark(cams[0],"confirm-original-payout");
    const claim=await intent(b,"S7",{existing_batches:before});
    previous=claim.prior;
    if(claim.fresh){
      const [reply]=await Promise.all([
        page.waitForResponse(r=>new URL(r.url()).pathname==="/api/judge/settle"&&r.request().method()==="POST",{timeout:120000}),
        page.waitForRequest(r=>new URL(r.url()).pathname==="/api/judge/settle"&&r.method()==="POST").then(()=>actionEvent(cams,"original-payout-submitted",b.mid)),
        confirm.click(),
      ]);
      check(reply.ok(),"The original payout submission has no confirmation; resume its checkpoint");
      const created=await reply.json();check(created.id && !before.includes(created.id),"No new payout batch was submitted");
      await savedBatch(b,created.id);
    }
  }
  const knownBatch=await savedBatch(b);
  const batch=await waitFor(page,async()=>{
    const candidates=(await get(page,"payouts")).filter(x=>knownBatch?x.id===knownBatch:!previous.existing_batches.includes(x.id));
    check(candidates.length<=1,"Multiple later batches require inspection of the original recording");
    const x=candidates[0];
    if(x && x.status==="SUCCESS" && x.items?.length===2 && x.items.every(i=>i.status==="SUCCESS"&&payees[i.publisher_id]&&i.paypal_item_id))return x;
    return null;
  },180000);
  await savedBatch(b,batch.id);
  const {caps}=await cashEvidence(page,b);
  const verified=await provider({order:b.order,authorization:b.auth,captures:caps.map(c=>c.paypal_capture_id),
    batch:{id:batch.paypal_batch_id,sender_batch_id:batch.sender_batch_id,items:batch.items.map(i=>({id:i.paypal_item_id,slug:payees[i.publisher_id],sender_item_id:i.id}))}});
  for(const item of batch.items){
    const p=verified.batch.items.find(i=>i.id===item.paypal_item_id);
    check(p&&p.amount_cents===item.amount_cents && p.fee_cents===item.fee_cents && item.net_cents===item.amount_cents+item.fee_cents,"Payout receipt differs from PayPal");
  }
  const report=await get(page,"reconcile");check(report.ok===true&&report.checked>0&&!report.differences.length,"The recording workspace has not reconciled");
  await Promise.all(cams.map(async cam=>{
    await go(cam,"/settlement");
    const card=cam.page.locator('[data-testid="settlement-batch"][data-batch-id="'+safeId(batch.paypal_batch_id)+'"]');
    await expect(card).toBeVisible({timeout:30000});
    for(const item of batch.items){
      const row=card.locator('[data-testid="settlement-item"][data-item-id="'+safeId(item.paypal_item_id)+'"]');
      await expect(row).toBeVisible();await expect(row.locator('[data-field="status"]')).toContainText("SUCCESS");
      for(const [field,n] of [["amount_cents",item.amount_cents],["fee_cents",item.fee_cents],["net_cents",item.net_cents]]){
        const cell=row.locator('[data-field="'+field+'"]');await expect(cell).toBeVisible();
        check(Math.round(Number((await cell.innerText()).replace(/[^\d.-]/g,""))*100)===n,"Displayed payout amount differs from PayPal");
      }
    }
    await card.scrollIntoViewIfNeeded();await mark(cam,"successful-original-payout");
    await explainRoyalties(cam);
  }));
  return {workspaceId:b.ws,mandateId:b.mid,action:original?"verified existing payout":"verified payout",batch:verified.batch,reconciliation:report};
}
async function recordSoakSegment(options){
  const {scene,browser,outputDirs,rehearsal}=options;
  check(["S5","S6","S7"].includes(scene)&&Array.isArray(outputDirs)&&outputDirs.length===2&&
    path.resolve(outputDirs[0])!==path.resolve(outputDirs[1]),"Provide two independent output directories for S5, S6 or S7");
  fromFrontend("@next/env").loadEnvConfig(root);
  const b=await binding({...options,journalPath:options.journalPath||(options.rehearsalActions?process.env.CACHET_SOAK_REHEARSAL_JOURNAL:process.env.CACHET_SOAK_JOURNAL)});
  const cams=await cameras(browser,b,outputDirs,scene);
  let success=false;
  try{
    for(const c of cams){
      const live=await liveWorkspace(c.page,b);
      if(!rehearsal)check(live.mode==="live","Formal filming requires the production live GPU");
    }
    const evidence=await ({S5:capturesShot,S6:closingShot,S7:payoutShot}[scene])(cams,b);
    success=true;
    return cams.map((cam,i)=>({events:cam.events,evidence:{...evidence,camera:i+1,rehearsal}}));
  }finally{
    const saved=await Promise.allSettled(cams.map(c=>finish(c,success)));
    const failed=saved.find(s=>s.status==="rejected");
    if(success && failed)throw failed.reason;
  }
}
module.exports={recordSoakSegment,binding,privateJson,intent,existingIntent,savedBatch,checkDisplayedMoney};

if(require.main===module){
  (async()=>{
    const [scene,dirA,dirB,mode]=process.argv.slice(2);
    check(dirA&&dirB&&["--formal","--rehearsal","--isolated-rehearsal-actions"].includes(mode),
      "Usage: node video/soak_scenes.cjs S5|S6|S7 CAMERA_A CAMERA_B --formal|--rehearsal|--isolated-rehearsal-actions");
    fromFrontend("@next/env").loadEnvConfig(root);
    const rehearsalActions=mode==="--isolated-rehearsal-actions";
    const browser=await fromFrontend("playwright").chromium.launch({channel:process.env.PLAYWRIGHT_CHROMIUM_CHANNEL||undefined});
    try{
      const outputDirs=[dirA,dirB];
      const shots=await recordSoakSegment({scene,browser,baseURL:rehearsalActions?process.env.CACHET_REHEARSAL_WEB_URL:process.env.PUBLIC_WEB_URL,
        journalPath:rehearsalActions?process.env.CACHET_SOAK_REHEARSAL_JOURNAL:process.env.CACHET_SOAK_JOURNAL,
        outputDirs,rehearsal:mode!=="--formal",rehearsalActions});
      for(const [i,shot] of shots.entries())await fs.writeFile(path.join(outputDirs[i],scene+"-soak-timeline.json"),JSON.stringify(shot,null,2)+"\n");
    }finally{await browser.close();}
  })().catch(error=>{console.error(error.message.split("Call log:")[0]);process.exitCode=1;});
}
