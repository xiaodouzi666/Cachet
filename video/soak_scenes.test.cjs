// Offline permission and crash-recovery contracts. These fixtures never contact a provider.
const assert=require("node:assert/strict");
const {test}=require("node:test");
const fs=require("node:fs/promises");
const os=require("node:os");
const path=require("node:path");
const {binding,privateJson,intent,existingIntent,savedBatch,checkDisplayedMoney}=require("./soak_scenes.cjs");
test("separate recording and rehearsal identity, one durable action and exact receipt",async()=>{
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),"cachet-scene-contract-"));
  try{
    const p=path.join(dir,"formal.json"),r=path.join(dir,"rehearsal.json");
    const env={PUBLIC_WEB_URL:"https://web.example.invalid",PUBLIC_API_URL:"https://api.example.invalid",
      CACHET_REHEARSAL_WEB_URL:"http://localhost:3005",CACHET_REHEARSAL_API_URL:"http://localhost:8118",
      CACHET_SOAK_JOURNAL:p,CACHET_SOAK_REHEARSAL_JOURNAL:r};
    function fixture(name,web,api){return {scenario:"operator-live-soak-v3",web_origin:web,origin:api,
      workspace_id:name,mandate_id:name,workspace_cookie:"unit-only",latest:{workspace_id:name,mandate_id:name,paypal_order_id:name,authorization_id:name}};}
    const formal=fixture("formal",env.PUBLIC_WEB_URL,env.PUBLIC_API_URL), rehearsal=fixture("isolated",env.CACHET_REHEARSAL_WEB_URL,env.CACHET_REHEARSAL_API_URL);
    await fs.writeFile(p,JSON.stringify(formal),{mode:0o600});await fs.writeFile(r,JSON.stringify(rehearsal),{mode:0o600});
    const formalOptions={baseURL:env.PUBLIC_WEB_URL,journalPath:p,rehearsal:false};
    assert.equal((await binding(formalOptions,env)).canAct,true);
    assert.equal((await binding({...formalOptions,rehearsal:true},env)).canAct,false);
    const isolated={baseURL:env.CACHET_REHEARSAL_WEB_URL,journalPath:r,rehearsal:true,rehearsalActions:true};
    const b=await binding(isolated,env);assert.equal(b.canAct,true);
    await assert.rejects(binding({...isolated,journalPath:p},env),/designated/);
    await assert.rejects(binding({...formalOptions,baseURL:env.CACHET_REHEARSAL_WEB_URL},env),/Browser origin/);
    await fs.chmod(p,0o644);await assert.rejects(privateJson(p),/0600/);await fs.chmod(p,0o600);
    const alias=path.join(dir,"alias");await fs.symlink(p,alias);await assert.rejects(privateJson(alias));
    await fs.writeFile(r,JSON.stringify({...rehearsal,workspace_id:formal.workspace_id,latest:{...rehearsal.latest,workspace_id:formal.workspace_id}}));
    await assert.rejects(binding(isolated,env),/own workspace/);
    const callers=await Promise.all([intent(b,"S7",{existing_batches:[]}),intent(b,"S7",{existing_batches:[]})]);
    assert.equal(callers.filter(x=>x.fresh).length,1);assert.equal((await existingIntent(b,"S7")).mandate_id,"isolated");
    await savedBatch(b,"local-batch");assert.equal(await savedBatch(b),"local-batch");
    await assert.rejects(savedBatch(b,"another-batch"),/bound/);
    checkDisplayedMoney("Fee (USD) 0.54",540000);checkDisplayedMoney("Forgiven $0.062348",62348);
    assert.throws(()=>checkDisplayedMoney("0.55",540000),/differs/);
  }finally{await fs.rm(dir,{recursive:true,force:true});}
});
