"use strict";
const fs = require("node:fs/promises");
const path = require("node:path");
const { randomUUID, createHash } = require("node:crypto");
const { spawn, execFile, execFileSync } = require("node:child_process");
const { promisify } = require("node:util");
const { performance } = require("node:perf_hooks");
const { recordSoakSegment } = require("./soak_scenes.cjs");
const exec = promisify(execFile);
const root = path.resolve(__dirname, "..");
const providerStillStyle = "body *{visibility:hidden!important} [data-cachet-film-visible],[data-cachet-film-visible] *,iframe[data-cachet-film-frame]{visibility:visible!important}";
const regularOrder = ["S2","S3","S4","S5","S1","S6","S8"];
const all = [...regularOrder,"S7"];
const changing = new Set(["S3","S4","S5","S6"]);
const wait = ms => new Promise(resolve => setTimeout(resolve,ms));
const assert = (ok,message) => { if (!ok) throw Error(message); };
async function atomic(file,data) {
  const tmp=file+"."+randomUUID()+".tmp";
  const h=await fs.open(tmp,"wx",0o600);
  try { await h.writeFile(JSON.stringify(data,null,2)+"\n"); await h.sync(); } finally { await h.close(); }
  await fs.rename(tmp,file);
}
async function read(file,fallback) {
  try { return JSON.parse(await fs.readFile(file,"utf8")); }
  catch(e) { if(e.code==="ENOENT" && fallback!==undefined) return fallback; throw e; }
}
async function fileLock(file,work,nonblocking=false) {
  // flock holds a kernel lock until the command's stdin closes, including parent-process loss.
  await fs.mkdir(path.dirname(file),{recursive:true});
  const child=spawn("flock", [...(nonblocking?["-n"]:[]),"-x",file,"sh","-c","echo acquired; cat >/dev/null"],{stdio:["pipe","pipe","inherit"]});
  const done = new Promise((res,rej)=>{child.once("error",rej);child.once("exit",res);});
  child.stdin.on("error",() => {});
  try {
    await Promise.race([new Promise(resolve=>child.stdout.once("data",resolve)),
      done.then(()=>{throw Error("The recording lock could not be acquired: "+path.basename(file));})]);
    return await work();
  } finally { child.stdin.end(); await done; }
}
async function restoreSubmittedSoak(scene, cameras, sourceDir, pair) {
  const label = scene === "S6" ? "original-close-submitted" : "original-payout-submitted";
  if (pair.every(view => view.events.some(event => event.label === label))) return {sourceDir, pair};
  assert(pair.every(view => !view.events.some(event => event.label === label) &&
    view.evidence.action.startsWith("verified existing ")), "Both cameras must refer to one original submission.");
  const joinedDir = scene + "-joined-" + randomUUID();
  const observed = [];
  for (const [index, camera] of cameras.entries()) {
    const view = pair[index];
    const id = scene === "S6" ? view.evidence.provider.authorization_id : view.evidence.mandateId;
    const digest = createHash("sha256").update(id).digest("hex");
    const {stdout} = await exec(process.env.CACHET_PYTHON || "python3",
      [path.join(__dirname, "join_soak.py"), camera.dir, scene, sourceDir, joinedDir, digest]);
    const joined = JSON.parse(stdout);
    view.events = joined.events;
    view.evidence.observationParts = joined.observationParts;
    const events = view.events.filter(event => event.label === label && event.actionKey === id);
    assert(events.length === 1 && events[0].utc, "The recovered footage lacks its original browser request.");
    observed.push(events[0].utc);
  }
  assert(observed[0] === observed[1], "Recovered camera streams observed different original requests.");
  return {sourceDir: joinedDir, pair};
}

function gpuLockPath() {
  if(process.env.CACHET_GPU_LOCK)return process.env.CACHET_GPU_LOCK;
  const common=path.resolve(root,execFileSync("git",["rev-parse","--git-common-dir"],{cwd:root,encoding:"utf8"}).trim());
  return path.join(path.dirname(common),".cache/night-gpu.lock");
}
function options(args) {
  const value = flag => {
    const n=args.indexOf(flag);
    if(n<0) return undefined;
    assert(args[n+1] && !args[n+1].startsWith("--"),"Missing "+flag+" value");
    return args[n+1];
  };
  const opts=new Set(["--soak-only","--regular-take","--session","--rehearsal","--isolated-rehearsal-actions","--archive-gallery","--compose-only","--cleanup","--allow-partial"]);
  assert(args.filter(x=>x.startsWith("--")).every(x=>opts.has(x)),"Unknown recorder option.");
  const sceneArg=args.find((s,i)=>!s.startsWith("--")&&!["--session","--regular-take"].includes(args[i-1]));
  const only=value("--regular-take");
  assert(only===undefined || ["1","2"].includes(only),"Choose --regular-take 1 or 2.");
  const names=sceneArg?sceneArg.split(","):all;
  assert(names.length && names.every(n=>all.includes(n)) && new Set(names).size===names.length,"Choose distinct S1–S8 IDs.");
  const soakOnly=args.includes("--soak-only");
  assert(!soakOnly || (!only && names.every(n=>["S5","S6","S7"].includes(n))),"--soak-only requires S5, S6 or S7 and both cameras.");
  return {names,dir:value("--session"),regularTake:only?Number(only):null,soakOnly,rehearsal:args.includes("--rehearsal"),actions:args.includes("--isolated-rehearsal-actions"),
          gallery:args.includes("--archive-gallery"),compose:args.includes("--compose-only"),cleanup:args.includes("--cleanup"),
          allowPartial:args.includes("--allow-partial")};
}
async function cookieState(context,base) {
  const snapshot=await context.storageState();
  const domain=new URL(base).hostname;
  return {cookies:snapshot.cookies.filter(c=>c.domain===domain&&["cachet_workspace","cachet_user_workspace"].includes(c.name)),origins:[]};
}
function proof(req) {
  return new Promise((resolve,reject)=>{
    const p=spawn(process.env.CACHET_PYTHON || "python3",[path.join(__dirname,"soak_readback.py")],
      {cwd:root,stdio:["pipe","pipe","ignore"]});
    let out=""; p.stdout.on("data",c=>out+=c);
    p.once("error",reject); p.once("close",code=>{
      try { assert(code===0,"Original wallet authorization could not be verified with PayPal Sandbox GET.");resolve(JSON.parse(out)); }
      catch(e){reject(e);}
    });
    p.stdin.end(JSON.stringify(req));
  });
}
async function cleanup(camera,browser,api) {
  const m=camera.data;
  if(!m.workspaceId || m.cleanup?.verified || !m.segments["S4-regular"]) return;
  const ctx=await browser.newContext({baseURL:m.origin,storageState:await read(path.join(camera.dir,"storage.json"))});
  try {
    const page=await ctx.newPage();
    const state=await api.readState(page);
    assert(state.workspace_id===m.workspaceId,"Cleanup requires the exact saved judge workspace.");
    const hold=state.mandate;
    if(!hold){ m.cleanup={no_order:true}; await camera.save(); return; }
    assert(hold.payment_method==="wallet" && hold.cap_micros===5000000 && (!m.mandateId||m.mandateId===hold.id)
      && (!m.orderId||m.orderId===hold.paypal_order_id)&&(!m.authorizationId||m.authorizationId===hold.authorization_id),
      "The wallet mandate differs from this camera's original order.");
    m.mandateId=hold.id; m.orderId=hold.paypal_order_id; m.authorizationId=hold.authorization_id;
    await camera.save();
    await api.expect.poll(async()=>!["running","queued"].includes((await api.readState(page)).run?.status),{timeout:15*60*1000}).toBe(true);
    const status=await page.request.post("/api/mandates/"+encodeURIComponent(hold.id)+"/close",{data:{}});
    assert(status.ok(),"Regular wallet cleanup HTTP "+status.status()+"; resume this session with --cleanup.");
    const closed=await status.json();
    assert(["VOIDED","CLOSED","EXPIRED","FAILED"].includes(closed.status) && closed.id===hold.id,"Regular wallet release was not terminal.");
    let receipt;
    if(hold.authorization_id){
      const caps=(await api.readJson(page,"captures")).filter(c=>c.mandate_id===hold.id);
      const capIds=new Set(caps.map(c=>c.id));
      const refunds=(await api.readJson(page,"refunds")).filter(r=>capIds.has(r.capture_id));
      receipt=await proof({order:hold.paypal_order_id,authorization:hold.authorization_id,captures:caps.map(c=>c.paypal_capture_id),
                           refunds:refunds.map(r=>r.paypal_refund_id)});
      assert(["CAPTURED","VOIDED","EXPIRED"].includes(receipt.authorization_status),"The original PayPal hold is still open.");
    } else assert(closed.status==="FAILED","Unexpected disposition of the unapproved wallet order.");
    m.cleanup={verified:true,closed,receipt:receipt||null};
    await camera.save();
  } finally { await ctx.close(); }
}
async function waitForSponsor(camera,browser,api) {
  if(!camera.data.workspaceId || !camera.data.segments["S3-regular"]) return;
  const saved=await read(path.join(camera.dir,"storage.json"),null);
  if(!saved) return;
  const ctx=await browser.newContext({baseURL:camera.data.origin,storageState:saved});
  try {
    const p=await ctx.newPage();
    assert((await api.readState(p)).workspace_id===camera.data.workspaceId,"Cannot inspect another workspace's guide preparation.");
    await api.expect.poll(async()=>{
      const guide=await api.readJson(p,"judge/walkthrough");
      return ["not_started","ready","exhausted","failed"].includes(guide.preparation.status);
    },{timeout:20*60*1000,intervals:[2000,5000,10000],message:"Waiting for this camera's GPU sponsor to finish before releasing its lease"}).toBe(true);
  } finally { await ctx.close(); }
}
async function recordNormal(camera,scene,browser,api,opts) {
  const m=camera.data, key=scene+"-regular", old=m.segments[key];
  if(old?.status==="captured") return;
  if(old && changing.has(scene)) throw Error(scene+" was interrupted; the existing wallet workspace is retained. Inspect its partial raw footage before recording another take.");
  if(scene==="S4") assert(m.segments["S3-regular"]?.status==="captured","S4 requires this same take's S3.");
  if(scene==="S5") assert(m.segments["S4-regular"]?.status==="captured","S5 requires this same take's wallet approval.");
  if(scene==="S6") assert(m.segments["S1-regular"]?.status==="captured" && m.segments["S5-regular"]?.status==="captured","Record the completed ten-turn S5 and S1 before S6.");
  const scratch=await fs.mkdtemp(path.join(camera.dir,".regular-"));
  const stateFile=path.join(camera.dir,"storage.json");
  const storage=await read(stateFile,null);
  const context=await browser.newContext({baseURL:m.origin,viewport:{width:1920,height:1080},deviceScaleFactor:1,locale:"en-US",
                                          storageState:storage||undefined,recordVideo:{dir:scratch,size:{width:1920,height:1080}}});
  await context.addInitScript(() => {
    const redact = () => {
      if (!document.head || !document.documentElement || document.documentElement.namespaceURI!=="http://www.w3.org/1999/xhtml" || document.getElementById("recording-privacy")) return;
      const style=document.createElement("style");
      style.id="recording-privacy";
      style.textContent="input[type=email],input[type=password],input[autocomplete*=email],input[autocomplete*=password],[data-sensitive],[data-private]{-webkit-text-security:disc!important;color:transparent!important;text-shadow:none!important}";
      document.head.appendChild(style);
    };
    redact(); document.addEventListener("DOMContentLoaded",redact,{once:true});
  });
  const start=performance.now();
  let page;
  const entry={status:"recording",events:[],waits:[]};
  m.segments[key]=entry;
  await camera.save();
  try {
    page=await context.newPage();
    const waitForEvidence=async(reason,operation)=>{
      const begin=Math.round(performance.now()-start);
      try{return await operation();}
      finally{
        const end=Math.round(performance.now()-start);
        if(end-begin>=5000){entry.waits.push({start_ms:begin,end_ms:end,reason});await camera.save();}
      }
    };
    const go=async route=>{
      const res=await waitForEvidence("document-navigation",()=>page.goto(route+"?rec=1",{waitUntil:"domcontentloaded"}));
      assert(res?.ok(),"Recording page did not load: "+route);
      await api.expect(page.locator("html")).toHaveClass(/recording/);
    };
    const mark=async(label,target=page,mask=[],surface="application")=>{
      assert(/^[a-z0-9-]+$/.test(label),"Invalid milestone label");
      const ms=Math.round(performance.now()-start);
      const screenshot=scene+"-"+label+".png";
      const obscured=[];
      for(const locator of [...mask,target.locator("input[type=email],input[type=password],[data-sensitive],[data-private]")]){
        if(await locator.count()) obscured.push(locator);
      }
      await target.screenshot({path:path.join(camera.dir,screenshot),...(obscured.length?{mask:obscured}:{}),
          ...(surface==="verified-provider-control"?{style:providerStillStyle}:{})});
      entry.events.push({label,ms,screenshot,surface,utc:new Date().toISOString()});
      await camera.save();
      // A short reading beat for the displayed slides and an encoder tail for live
      // actions. Wait on displayed animation frames so the video includes the new DOM.
      await target.evaluate(count=>new Promise(resolve=>{
        const tick=()=>--count ? requestAnimationFrame(tick) : resolve();
        requestAnimationFrame(tick);
      }), ["S1","S2","S8"].includes(scene)?60:8);
    };
    const checkpoint=async facts=>{
      Object.assign(m,facts);
      await atomic(stateFile,await cookieState(context,m.origin));
      await camera.save();
    };
    entry.evidence=await api.scenes[scene]({page,go,mark,checkpoint,waitForEvidence,baseURL:m.origin,archiveGallery:opts.gallery,
      s3:m.segments["S3-regular"]?.evidence,s4:m.segments["S4-regular"]?.evidence,s5:m.segments["S5-regular"]?.evidence});
    await checkpoint({});
  } catch(error) {
    entry.status="incomplete";
    entry.failure={category:["TimeoutError","AssertionError","CheckoutError"].includes(error.name)?error.name:"recording-error",
                   after:entry.events.at(-1)?.label||"scene-start"};
    throw error;
  } finally {
    const v=page?.video(); await context.close();
    if(v){await v.saveAs(path.join(camera.dir,scene+(entry.events.length?"":"-empty")+".webm"));await v.delete();}
    await fs.rm(scratch,{recursive:true,force:true});
    // Publish the success marker only after the encoder flushes the source file to disk.
    await camera.save();
  }
  entry.status="captured"; await camera.save();
}
async function runRecorder(api,args) {
  process.umask(0o077);
  const opts=options(args);
  assert(!opts.actions || opts.rehearsal,"Isolated rehearsal actions require --rehearsal.");
  assert(!opts.gallery||opts.rehearsal,"Only rehearsals can select the archive gallery.");
  if(opts.rehearsal && opts.names.some(n=>changing.has(n)) && !opts.compose && !opts.cleanup)
    assert(opts.actions,"Wallet, GPU and judge rehearsal scenes require --isolated-rehearsal-actions.");
  if(opts.cleanup||opts.compose) assert(opts.dir,"Resume the original --session for cleanup or composition.");
  const session=path.resolve(opts.dir||path.join(root,"video/out",opts.rehearsal?"rehearsals":"takes",new Date().toISOString().replace(/[:.]/g,"-")));
  await fs.mkdir(session,{recursive:true,mode:0o700});
  return fileLock(path.join(session,"private","session.lock"),async()=>{
    const metaFile=path.join(session,"private/session.json");
    const prior=await read(metaFile,null);
    if(opts.cleanup||opts.compose) assert(prior,"No saved recording exists in this session directory.");
    const base=process.env.CACHET_WEB_URL || prior?.origin;
    assert(base,"Set CACHET_WEB_URL to the target origin");
    const url=new URL(base); assert(url.origin===base.replace(/\/$/,"")&&!url.username&&!url.password,"Supply the bare web origin");
    assert(opts.rehearsal||url.protocol==="https:","Production recording requires HTTPS.");
    if(opts.actions) assert(url.origin===new URL(process.env.CACHET_REHEARSAL_WEB_URL).origin && url.origin!==new URL(process.env.PUBLIC_WEB_URL).origin,
      "Rehearsal writes require the designated independent web origin.");
    if(!opts.rehearsal&&!opts.compose&&!opts.cleanup) assert(url.origin===new URL(process.env.PUBLIC_WEB_URL).origin,"Production origin differs from PUBLIC_WEB_URL.");
    const meta=prior||{session:randomUUID(),origin:url.origin,rehearsal:opts.rehearsal};
    assert(meta.origin===url.origin&&meta.rehearsal===opts.rehearsal,"The original recording origin and mode are required.");
    if(!prior) await atomic(metaFile,meta);
    const cameras=[];
    for(const id of [1,2]){
      const dir=path.join(session,"private","camera-"+id);
      await fs.mkdir(dir,{recursive:true,mode:0o700});
      const f=path.join(dir,"segments.json"), data=await read(f,{...meta,camera:id,segments:{}});
      cameras.push({dir,data,save:()=>atomic(f,data)}); await cameras.at(-1).save();
    }
    let faults=[];
    if(!opts.compose){
      const browser=await api.chromium.launch({channel:process.env.PLAYWRIGHT_CHROMIUM_CHANNEL||undefined});
      try {
        if(opts.cleanup) { for(const c of cameras) await cleanup(c,browser,api); return; }
        for(const c of cameras){
          if(opts.soakOnly || (opts.regularTake && c.data.camera!==opts.regularTake))continue;
          const ordered=regularOrder.filter(scene=>opts.names.includes(scene));
          const runCamera=async()=>{
            let failed=false;
            try {
              for(const scene of ordered) await recordNormal(c,scene,browser,api,opts);
            } catch(e) {
              failed=true;
              const failedScene=regularOrder.find(n=>["incomplete","recording"].includes(c.data.segments[n+"-regular"]?.status));
              faults.push(failedScene==="S4"?"S4 wallet approval is incomplete. Inspect the isolated camera's private journal and release readback.":String(e.message).split("Call log:")[0]);
            } finally {
              if(failed||c.data.segments["S6-regular"]?.status==="captured" ||
                  (ordered.includes("S4") && !ordered.includes("S6"))) {
                try { await cleanup(c,browser,api); } catch(e) { failed=true; faults.push(e.message); }
              }
            }
            return failed;
          };
          // The ordinary wallet, sponsor preparation, original run and global flush all use
          // the shared GPU. Take exactly one lease per camera; S6 uses the enclosing lease.
          const usesGpu=ordered.some(n=>changing.has(n)&&c.data.segments[n+"-regular"]?.status!=="captured");
          let failed;
          if(usesGpu){
            failed=await fileLock(gpuLockPath(),async()=>{
              try { return await runCamera(); }
              finally { await waitForSponsor(c,browser,api); }
            });
          } else failed=await runCamera();
          if(failed&&!opts.allowPartial) break;
        }
        if(opts.regularTake){
          if(faults.length)throw Error(faults.join("\n"));
          console.log("Saved ordinary camera "+opts.regularTake+" in "+session);
          return;
        }
        if(!faults.length||opts.allowPartial){
          for(const scene of ["S5","S6","S7"].filter(n=>opts.names.includes(n))){
            const key=scene+"-soak";
            if(cameras.every(c=>c.data.segments[key]?.status==="captured"))continue;
            try{
              const pairFile=path.join(session,"private","pair-"+scene+".json");
              let savedPair=await read(pairFile,null);
              if(!savedPair){
                const observationFile=path.join(session,"private","observation-"+scene+".json");
                let observation=await read(observationFile,null);
                if(!observation){
                  const sourceDir=scene+"-attempt-"+randomUUID();
                  const recordSoak=()=>recordSoakSegment({scene,browser,baseURL:meta.origin,journalPath:opts.actions?
                    process.env.CACHET_SOAK_REHEARSAL_JOURNAL:process.env.CACHET_SOAK_JOURNAL,
                    outputDirs:cameras.map(c=>path.join(c.dir,sourceDir)),rehearsal:opts.rehearsal,rehearsalActions:opts.actions});
                  const pair=scene==="S7" ? await fileLock(gpuLockPath(),recordSoak) : await recordSoak();
                  observation={sourceDir,pair};
                  await atomic(observationFile,observation);
                }
                savedPair=(scene==="S6" || scene==="S7") && (!opts.rehearsal || opts.actions)
                  ? await restoreSubmittedSoak(scene,cameras,observation.sourceDir,observation.pair) : observation;
                await atomic(pairFile,savedPair);
              }
              for(const [i,c] of cameras.entries()){
                if(c.data.segments[key]?.status==="captured") continue;
                c.data.segments[key]={status:"captured",sourceDir:savedPair.sourceDir,...savedPair.pair[i]};
                await c.save();
              }
            }catch(e){faults.push(scene+": "+e.message);break;}
          }
        }
        if(opts.soakOnly){
          if(faults.length)throw Error(faults.join("\n"));
          console.log("Saved both original soak angles for "+opts.names.join(", ")+" in "+session);
          return;
        }
      } finally {await browser.close();}
    }
    for(const c of cameras){
      const dest=path.join(session,"take-"+c.data.camera);
      await exec(process.env.CACHET_PYTHON||"python3",[path.join(__dirname,"compose_take.py"),c.dir,
        dest,"--scenes",opts.names.join(",")],{cwd:root,maxBuffer:1048576});
      const cut=await read(path.join(dest,"timeline.json"));
      const missing=opts.names.filter(name=>cut.scenes[name]?.status!=="captured");
      if(missing.length) faults.push("Camera "+c.data.camera+" incomplete: "+missing.join(", "));
    }
    console.log(session);
    if(faults.length)throw Error(faults.join("\n"));
  },true);
}
module.exports={runRecorder,options,providerStillStyle};
