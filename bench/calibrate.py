"""Capture the five SLA length bands and fit cold prefill cost from real GPU calls."""
import json
import math
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from bench.ttft_bench import ROOT, Engine, benchmark

RESULT = ROOT / "bench/results/calibration.json"
PLOT = ROOT / "bench/plots/prefix-latency.png"


def fit(bands):
    # Nonnegative least squares for two features, n and n², with zero intercept.
    data = [(b["prefix_tokens"] / 1000, b["cold_p50_ms"]) for b in bands]
    a = sum(x**2 for x, y in data)
    b = sum(x**3 for x, y in data)
    c = sum(x**4 for x, y in data)
    d = sum(x*y for x, y in data)
    e = sum(x*x*y for x, y in data)
    det = a*c-b*b
    candidates = [(d/a, 0), (0, e/c), ((d*c-b*e)/det, (a*e-b*d)/det)]
    linear, quadratic = min((p for p in candidates if min(p) >= 0),
        key=lambda p: sum((p[0]*x + p[1]*x*x - y)**2 for x,y in data))
    return {"alpha": linear/1000, "beta": quadratic/1_000_000, "units": "milliseconds, tokens"}


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
        points = [(left+i*(right-left)/4,y(b[field])) for i,b in enumerate(data["bands"])]
        draw.line(points,fill=color,width=7)
        for (xx,yy),band in zip(points,data["bands"]):
            draw.ellipse((xx-9,yy-9,xx+9,yy+9),fill=color)
            draw.text((xx,yy-18),f"{band[field]:,.1f}",anchor="mb",font=font(23),fill=color)
            if j==0:
                draw.text((xx,bottom+24),f"{band['prefix_tokens']//1000}k",anchor="mt",font=font(29),fill="#111827")
        draw.text((1400+j*185,242),label,font=font(32),fill=color)
    draw.text((110,970),"Client-observed median of five cold/warm pairs per length. Each pair begins with a verified cache flush.",font=font(27),fill="#374151")
    PLOT.parent.mkdir(exist_ok=True)
    im.save(PLOT)


def main():
    with Engine() as engine:
        bands = [benchmark(engine, n, 5) for n in (2000, 4000, 8000, 16000, 26000)]
        meta = engine.get("/server_info").raise_for_status().json()
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
