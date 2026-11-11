"""Capture the five SLA length bands and fit cold prefill cost from real GPU calls."""
import argparse
import json
import math
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from backend.app.gateway.calibration import fit
from backend.app.gateway.prompt_builder import Prefix
from bench.ttft_bench import ROOT, Engine, benchmark
from scripts.gpu_lease import gpu_lease

RESULT = (ROOT / "bench/results/calibration.json").resolve()
PLOT = ROOT / "bench/plots/prefix-latency.png"


def render(data):
    from PIL import Image, ImageDraw, ImageFont
    im = Image.new("RGB", (1920, 1080), "#f7f7f5")
    draw = ImageDraw.Draw(im)
    def font(size):
        return ImageFont.truetype("DejaVuSans.ttf", size)
    draw.text((110, 65), "Time to first text across prefix lengths", fill="#111827", font=font(66))
    draw.text((113, 170), f"{data['gpu']}   /   {data['model']}   /   SGLang {data['sglang']}", fill="#374151", font=font(28))
    top, bottom, left, right = 310, 845, 200, 1780
    values = [b[k] for b in data["bands"] for k in ("cold_p50_ms","warm_p50_ms")]
    low, high = math.floor(math.log10(min(values))), math.ceil(math.log10(max(values)))
    def y(v):
        return bottom - (math.log10(v)-low) * (bottom-top) / (high-low)
    for n in range(low, high+1):
        yy=y(10**n)
        draw.line((left,yy,right,yy),fill="#d1d5db",width=2)
        draw.text((left-20,yy),f"{10**n:,}",font=font(25),fill="#374151",anchor="rm")
    draw.text((110,245),"Milliseconds · logarithmic scale",font=font(26),fill="#374151")
    for j, (field, color, label) in enumerate((("cold_p50_ms","#374151","Cold"),("warm_p50_ms","#0f766e","Warm"))):
        points = [(left+i*(right-left)/(len(data['bands'])-1),y(b[field])) for i,b in enumerate(data["bands"])]
        draw.line(points,fill=color,width=7)
        for (xx,yy),band in zip(points,data["bands"]):
            draw.ellipse((xx-9,yy-9,xx+9,yy+9),fill=color)
            draw.text((xx,yy-18),f"{band[field]:,.1f}",anchor="mb",font=font(23),fill=color)
            if j==0:
                draw.text((xx,bottom+24),f"{band['prefix_tokens']//1000}k",anchor="mt",font=font(29),fill="#111827")
        draw.text((1400+j*185,242),label,font=font(32),fill=color)
    pairs = len(data["bands"][0]["runs"])
    draw.text((110,970),f"Client-observed median of {pairs} cold/warm pairs per length. Each pair begins with a verified cache flush.",font=font(27),fill="#374151")
    PLOT.parent.mkdir(exist_ok=True)
    im.save(PLOT)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reduced", action="store_true", help="Measure three lengths using three cold/warm pairs each.")
    parser.add_argument("--rewarm", action="store_true", help="Restore and verify the two published framework prefixes before releasing the GPU.")
    args = parser.parse_args()
    lengths = (2000, 8000, 26000) if args.reduced else (2000, 4000, 8000, 16000, 26000)
    trials = 3 if args.reduced else 5
    with gpu_lease(), Engine() as engine:
        try:
            bands = [benchmark(engine, n, trials) for n in lengths]
            meta = engine.get("/server_info").raise_for_status().json()
        finally:
            if args.rewarm:
                ratios = {name: Prefix(engine, name).warm(engine) for name in ("starlette", "fastapi")}
                print("Restored public prefix hit ratios:", json.dumps(ratios), flush=True)
                if min(ratios.values()) < 0.95:
                    raise RuntimeError("The published contexts did not return to their warm state.")
    record = {
        "measured_at": datetime.now(timezone.utc).isoformat(), "bands": bands, "fit": fit(bands),
        "model": meta["served_model_name"], "sglang": meta["version"],
        "gpu": subprocess.check_output(["nvidia-smi","--query-gpu=name","--format=csv,noheader"],text=True).strip(),
    }
    if any(b["warm_p50_ms"] >= b["cold_p50_ms"] for b in bands):
        raise ValueError("A length band did not show lower latency on cache reuse.")
    render(record)
    temp = RESULT.with_suffix(".tmp")
    temp.write_text(json.dumps(record,indent=2)+"\n")
    temp.replace(RESULT)
    print(json.dumps({k: record[k] for k in ("measured_at","gpu","model","sglang","fit")}), flush=True)


if __name__ == "__main__":
    main()
