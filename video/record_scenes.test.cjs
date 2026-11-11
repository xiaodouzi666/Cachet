const { test } = require("node:test");
const assert = require("node:assert/strict");
const { isColdAndFree, isFaultedWarmBreach } = require("./record_scenes.ts");
const buy = require("../backend/app/replay/traces/rate-limit-buy.json");
const compute = require("../backend/app/replay/traces/rate-limit-compute.json");

test("historical GPU measurements cannot stand in for a newly flushed or deliberately delayed call", () => {
  for (const archive of [buy, compute]) {
    for (const measured of archive.turns) {
      assert.equal(isColdAndFree(measured, measured.cache_flush_id), false);
      assert.equal(isFaultedWarmBreach(measured, measured.mode), false);
    }
  }
});


test("a global flush selects the exact recorded cold source without warming it", () => {
  const {recordingContext} = require("./record_scenes.ts");
  const contexts = [
    {slug:"fastapi", id:"public", can_manage:false, status:"hot"},
    {slug:"starlette", id:"parent", can_manage:true, status:"cold"},
    {slug:"fastapi", id:"mine", can_manage:true, status:"cold"}
  ];
  assert.equal(recordingContext(contexts), undefined);
  assert.equal(recordingContext(contexts, {flushedContextId:"mine"}).id, "mine");
  assert.equal(recordingContext(contexts, {flushedContextId:"public"}), undefined);
});

test("a popup return can resume only the exact original active wallet", () => {
  const {originalActiveWallet} = require("./record_scenes.ts");
  const order = {id:"mandate-a",paypal_order_id:"order-a",cap_micros:5000000};
  const authorized = {...order,payment_method:"wallet",status:"ACTIVE",authorization_id:"auth-a"};
  assert.ok(originalActiveWallet(authorized, order));
  for(const difference of [{id:"mandate-b"},{paypal_order_id:"order-b"},{cap_micros:4000000},
      {payment_method:"card"},{status:"VOIDED"},{authorization_id:null}]) {
    assert.equal(originalActiveWallet({...authorized,...difference},order),false);
  }
});

test("sandbox still preserves the selected control while obscuring private fields and sibling surfaces",
    {skip:!process.env.PLAYWRIGHT_CHROMIUM_CHANNEL}, async () => {
  const {createRequire}=require("node:module");
  const browser=await createRequire(require.resolve("../frontend/package.json"))("playwright").chromium.launch({
    channel:process.env.PLAYWRIGHT_CHROMIUM_CHANNEL
  });
  try {
    const page=await browser.newPage({viewport:{width:400,height:240}});
    await page.setContent('<style>body{margin:0} iframe{border:0;width:350px;height:150px}</style>'+
      '<div style="height:30px;background:red">private profile</div><iframe></iframe>');
    const child=page.frames()[1];
    await child.setContent('<div style="background:red;height:30px">private account</div>'+
      '<button style="background:lime;width:200px;height:55px">Pay</button>');
    const {markSandboxApproval}=require("./record_scenes.ts");
    const {providerStillStyle}=require("./record_driver.cjs");
    await markSandboxApproval(page,child,child.getByRole("button",{name:"Pay"}), async(label,surface,_,kind) => {
      assert.equal(label,"sandbox-wallet-open");
      assert.equal(kind,"verified-provider-control");
      const image=await surface.screenshot({style:providerStillStyle});
      const [red,green]=await page.evaluate(async image64=>{
        const img=new Image(); img.src="data:image/png;base64,"+image64;await img.decode();
        const canvas=document.createElement("canvas");canvas.width=img.width;canvas.height=img.height;
        const ctx=canvas.getContext("2d");ctx.drawImage(img,0,0);
        const {data}=ctx.getImageData(0,0,img.width,img.height);
        let red=0,green=0;
        for(let i=0;i<data.length;i+=4){
          if(data[i]===255&&data[i+1]===0&&data[i+2]===0)red++;
          if(data[i]===0&&data[i+1]===255&&data[i+2]===0)green++;
        }
        return [red,green];
      },image.toString("base64"));
      assert.equal(red,0);assert.ok(green>1000);
    },"5.00");
  }finally{await browser.close();}
});
