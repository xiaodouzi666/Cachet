"""Return only allowlisted proof fields from actual PayPal Sandbox GETs for a recording."""
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.app.paypal.client import PayPalClient, require, settings
from backend.app.settlement.service import cents


def verify(req):
    cfg = settings()
    with PayPalClient(cfg) as pp:
        order = pp.get_order(req["order"])
        auth = pp.get_authorization(req["authorization"])
        linked = [a["id"] for u in order["purchase_units"] for a in u.get("payments", {}).get("authorizations", [])]
        if linked != [auth["id"]] or order["intent"] != "AUTHORIZE" or cents(auth["amount"]) != 500:
            raise ValueError("The recording $5 authorization does not match its sandbox order")
        listed = {c["id"] for u in order["purchase_units"] for c in u.get("payments", {}).get("captures", [])}
        if listed != set(req.get("captures", [])):
            raise ValueError("The recorded capture list differs from the complete PayPal order")
        caps = []
        for cid in req.get("captures", []):
            c = pp.get_capture(cid)
            parent = c.get("supplementary_data", {}).get("related_ids", {})
            if (c["id"] != cid or c["status"] not in {"COMPLETED", "PARTIALLY_REFUNDED", "REFUNDED"}
                or parent.get("authorization_id") != auth["id"] or parent.get("order_id") != order["id"]):
                raise ValueError("Capture belongs to another authorization")
            breakdown = c["seller_receivable_breakdown"]
            caps.append({"id":cid,"amount_cents":cents(c["amount"]),"fee_cents":cents(breakdown["paypal_fee"]),
                         "net_cents":cents(breakdown["net_amount"]), "status":c["status"]})
        returned = []
        for fid in req.get("refunds", []):
            f = pp.get_refund(fid)
            parent = next((urlsplit(l["href"]).path for l in f.get("links",[]) if l["rel"]=="up"), "")
            if not any(parent == "/v2/payments/captures/"+c["id"] for c in caps) or f["id"]!=fid or f["status"]!="COMPLETED":
                raise ValueError("Refund is not a completed refund of this recording mandate")
            returned.append({"id":fid,"amount_cents":cents(f["amount"]),"status":f["status"]})
        out = {"order_id":order["id"],"authorization_id":auth["id"],"authorization_status":auth["status"],
               "authorized_cents":cents(auth["amount"]),"captures":caps,"refunds":returned}
        batch = req.get("batch")
        if batch:
            response = pp.get_payout(batch["id"])
            header = response["batch_header"]
            if header["batch_status"]!="SUCCESS" or header["payout_batch_id"]!=batch["id"] or header["sender_batch_header"]["sender_batch_id"]!=batch["sender_batch_id"]:
                raise ValueError("Sandbox did not confirm the recorded payout batch")
            details = []
            for item in batch["items"]:
                if item["slug"] not in ("starlette","fastapi"):
                    raise ValueError("Unexpected recording recipient")
                raw = pp.get_payout_item(item["id"])
                p = raw["payout_item"]
                if (raw["payout_batch_id"]!=batch["id"] or raw["payout_item_id"]!=item["id"] or
                    raw["transaction_status"]!="SUCCESS" or p["sender_item_id"]!=item["sender_item_id"] or
                    p["receiver"].casefold()!=require(cfg,"PUBLISHER_"+item["slug"].upper()+"_EMAIL").casefold()):
                    raise ValueError("Sandbox did not confirm the configured recording recipient")
                details.append({"id":raw["payout_item_id"],"slug":item["slug"],"amount_cents":cents(p["amount"]),
                                "fee_cents":cents(raw["payout_item_fee"]),"status":raw["transaction_status"]})
            if {x["slug"] for x in details}!={"starlette","fastapi"} or len(details)!=2 or len(response["items"])!=2:
                raise ValueError("Expected two successful recording recipients")
            out["batch"] = {"id":header["payout_batch_id"],"status":header["batch_status"],"fee_cents":cents(header["fees"]),"items":details}
        return out


if __name__ == "__main__":
    try:
        print(json.dumps(verify(json.load(sys.stdin))))
    except Exception as exc:
        print("Sandbox readback failed: "+type(exc).__name__,file=sys.stderr)
        raise SystemExit(1)
