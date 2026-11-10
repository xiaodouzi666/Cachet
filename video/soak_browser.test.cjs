// This records only an explicit offline transport-test page and deletes both recordings after inspection.
const {test}=require("node:test"),assert=require("node:assert/strict");
const fs=require("node:fs/promises"),os=require("node:os"),path=require("node:path"),http=require("node:http");
const {createRequire}=require("node:module");
const fromFrontend=createRequire(require.resolve("../frontend/package.json"));
const {recordSoakSegment}=require("./soak_scenes.cjs");
test("two independent cameras record the same read-only workspace with clocks and distinct videos",
  {skip:!process.env.PLAYWRIGHT_CHROMIUM_CHANNEL},async()=>{
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),"cachet-camera-transport-"));let browser;
  let writes=0;
  const server=http.createServer((req,res)=>{
    if(req.method!=="GET")writes++;
    if(!req.headers.cookie?.includes("cachet_workspace=offline-camera-cookie")){res.writeHead(403);res.end();return;}
    const route=new URL(req.url,"http://localhost").pathname;
    const mandate={id:"transport",paypal_order_id:"offline",authorization_id:"offline",cap_micros:5_000_000,status:"ACTIVE",accrued_uncaptured_micros:0};
    if(route.startsWith("/api/")){
      res.setHeader("Content-Type","application/json");
      const values={"/api/judge/state":{workspace_id:"transport",mandate,operator_soak_mandate_id:"transport"},
        "/api/mandates/transport":mandate,"/api/health":{mode:"replay"}};
      if(!values[route])res.statusCode=404;res.end(JSON.stringify(values[route]||{}));return;
    }
    res.setHeader("Content-Type","text/html");res.end("<main><h1>Offline camera transport fixture</h1></main>");
  });
  await new Promise(ok=>server.listen(0,"127.0.0.1",ok));
  try{
    const base="http://127.0.0.1:"+server.address().port, journal=path.join(dir,"test.json"),dirs=[path.join(dir,"a"),path.join(dir,"b")];
    Object.assign(process.env,{PUBLIC_WEB_URL:base,PUBLIC_API_URL:base,CACHET_SOAK_JOURNAL:journal});
    await fs.writeFile(journal,JSON.stringify({scenario:"operator-live-soak-v3",origin:base,web_origin:base,workspace_id:"transport",mandate_id:"transport",workspace_cookie:"offline-camera-cookie",latest:{workspace_id:"transport",mandate_id:"transport",paypal_order_id:"offline",authorization_id:"offline"}}),{mode:0o600});
    browser=await fromFrontend("playwright").chromium.launch({channel:process.env.PLAYWRIGHT_CHROMIUM_CHANNEL});
    const shots=await recordSoakSegment({scene:"S6",browser,baseURL:base,journalPath:journal,outputDirs:dirs,rehearsal:true});
    assert.equal(writes,0);assert.equal(shots.length,2);assert.equal(browser.contexts().length,0);
    for(const [i,shot] of shots.entries()){
      assert.equal(shot.evidence.camera,i+1);assert.equal(shot.evidence.action,"read-only rehearsal");
      assert.ok(shot.events[0].ms>=0);assert.ok((await fs.stat(path.join(dirs[i],shot.events[0].screenshot))).size>100);
      const file=path.join(dirs[i],"S6-soak.webm");assert.ok((await fs.stat(file)).size>100);
      const probe=await browser.newPage();
      try {
        const dimensions=await probe.evaluate(data=>new Promise((resolve,reject)=>{
          const element=document.createElement("video");
          element.onloadedmetadata=()=>resolve([element.videoWidth,element.videoHeight]);
          element.onerror=()=>reject(Error("Unable to decode recorded WebM"));
          element.src="data:video/webm;base64,"+data;
        }), (await fs.readFile(file)).toString("base64"));
        assert.deepEqual(dimensions,[1920,1080]);
      }finally{await probe.close();}
    }
  }finally{if(browser)await browser.close();await new Promise(ok=>server.close(ok));await fs.rm(dir,{recursive:true,force:true});}
});
