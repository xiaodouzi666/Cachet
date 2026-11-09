"use client";
import { useEffect, useRef, useState } from "react";
import { api, describe, usd } from "../lib/client";
import type { Mandate } from "../lib/types";
import { useSession } from "./shell";
import { Failure, MoneyStrip, Tag } from "./ui";

type Approval = { orderId: string };
type PayPalSdk = {
  createInstance(args: {
    clientId: string;
    components: string[];
    pageType: string;
  }): Promise<{
    findEligibleMethods(args: {
      currencyCode: string;
    }): Promise<{ isEligible: (method: string) => boolean }>;
    createPayPalOneTimePaymentSession(args: {
      onApprove: (data: Approval) => Promise<void>;
      onCancel: () => void;
      onError: (e: unknown) => void;
    }): {
      start: (
        options: { presentationMode: string },
        order: Promise<{ orderId: string }>,
      ) => Promise<void>;
    };
  }>;
};
declare global {
  interface Window {
    paypal?: PayPalSdk;
  }
}
let loading: Promise<PayPalSdk> | null = null;
function sdk() {
  if (window.paypal) return Promise.resolve(window.paypal);
  if (!loading)
    loading = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = "https://www.sandbox.paypal.com/web-sdk/v6/core";
      script.async = true;
      script.onload = () =>
        window.paypal
          ? resolve(window.paypal)
          : reject(new Error("Unable to initialize the PayPal sandbox SDK."));
      script.onerror = () => {
        script.remove();
        reject(new Error("The PayPal sandbox SDK could not be loaded."));
      };
      document.head.append(script);
    }).catch((error) => {
      loading = null;
      throw error;
    }) as Promise<PayPalSdk>;
  return loading;
}
const terminal = new Set(["CLOSED", "VOIDED", "EXPIRED", "FAILED"]);
export function Wallet({
  mandate,
  onUpdate,
}: {
  mandate: Mandate | null;
  onUpdate: () => Promise<void>;
}) {
  const { state } = useSession();
  const [cap, setCap] = useState("5.00"),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [message, setMessage] = useState(""),
    [attempt, setAttempt] = useState(0);
  const host = useRef<HTMLDivElement>(null),
    current = useRef({ cap, mandate, onUpdate }),
    paying = useRef(false);
  current.current = { cap, mandate, onUpdate };
  const clientId = state?.next_public_paypal_client_id;
  const pending =
    !!mandate && ["CREATED", "APPROVED"].includes(mandate.status.toUpperCase());
  const canCreate =
    !mandate || terminal.has(mandate.status.toUpperCase()) || pending;
  useEffect(() => {
    const container = host.current;
    if (!clientId || !canCreate || !container) return;
    let disposed = false;
    let action: { id: string; order: string } | undefined;
    const fail = (e: unknown) => {
      paying.current = false;
      setBusy(false);
      setError(describe(e));
    };
    (async () => {
      const sdkInstance = await (
        await sdk()
      ).createInstance({
        clientId,
        components: ["paypal-payments"],
        pageType: "checkout",
      });
      if (disposed) return;
      const eligibility = await sdkInstance.findEligibleMethods({
        currencyCode: "USD",
      });
      if (!eligibility.isEligible("paypal"))
        throw new Error(
          "PayPal wallet checkout is unavailable for this browser or buyer.",
        );
      const session = await sdkInstance.createPayPalOneTimePaymentSession({
        onApprove: async (data) => {
          try {
            if (!action || data.orderId !== action.order)
              throw new Error(
                "The approved order does not match this mandate.",
              );
            await api(`mandates/${action.id}/authorize`, {});
            await current.current.onUpdate();
            setMessage("Sandbox authorization recorded.");
          } catch (e) {
            fail(e);
            throw e;
          } finally {
            paying.current = false;
            setBusy(false);
          }
        },
        onCancel: () => {
          paying.current = false;
          setBusy(false);
          setMessage("Approval cancelled. You can reopen sandbox checkout.");
          void current.current.onUpdate().catch(fail);
        },
        onError: fail,
      });
      if (disposed) return;
      const button = document.createElement("paypal-button");
      button.setAttribute("aria-label", "Approve with PayPal sandbox");
      button.addEventListener("click", () => {
        if (paying.current) return;
        const value = Number(current.current.cap);
        if (
          !Number.isFinite(value) ||
          value <= 0 ||
          value > 20 ||
          !/^\d+(\.\d{1,2})?$/.test(current.current.cap)
        ) {
          setError(
            "Enter an amount from $0.01 to $20.00 with at most two decimal places.",
          );
          return;
        }
        paying.current = true;
        setBusy(true);
        setError("");
        setMessage("");
        const existing = current.current.mandate;
        const create = async () => {
          if (
            existing?.paypal_order_id &&
            ["CREATED", "APPROVED"].includes(existing.status.toUpperCase())
          )
            action = { id: existing.id, order: existing.paypal_order_id };
          else {
            const result = await api<{
              mandate_id: string;
              paypal_order_id: string;
            }>("mandates", { cap_usd: current.current.cap });
            action = { id: result.mandate_id, order: result.paypal_order_id };
          }
          return { orderId: action.order };
        };
        void session.start({ presentationMode: "auto" }, create()).catch(fail);
      });
      container.replaceChildren(button);
    })().catch((e) => {
      if (!disposed) fail(e);
    });
    return () => {
      disposed = true;
      container.replaceChildren();
    };
  }, [clientId, canCreate, attempt]);
  async function command(action: "card" | "close" | "authorize") {
    setBusy(true);
    setError("");
    setMessage("");
    try {
      if (action === "card") await api("mandates/card", { cap_usd: cap });
      else if (mandate) await api(`mandates/${mandate.id}/${action}`, {});
      await onUpdate();
    } catch (e) {
      setError(describe(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="panel">
      <div className="panel-head">
        <div>
          <h2>Spending mandate</h2>
          <p>Approve a USD budget once in the PayPal sandbox.</p>
        </div>
        {mandate && (
          <Tag
            tone={
              terminal.has(mandate.status.toUpperCase()) ? "neutral" : "good"
            }
          >
            {mandate.status}
          </Tag>
        )}
      </div>
      <MoneyStrip mandate={mandate} />
      {canCreate && (
        <div className="checkout">
          <label className="field">
            Budget (USD)
            <input
              type="number"
              min=".01"
              max="20"
              step=".01"
              value={
                pending && mandate
                  ? (mandate.cap_micros / 1000000).toFixed(2)
                  : cap
              }
              disabled={pending || busy}
              onChange={(e) => setCap(e.target.value)}
            />
          </label>
          <div className="checkout-buttons">
            <div ref={host} />
            {!clientId && (
              <span className="help">
                Sandbox wallet checkout awaits the platform’s PayPal
                configuration.
              </span>
            )}
            {state?.card_enabled && !pending && (
              <button
                className="secondary"
                disabled={busy || Number(cap) <= 0 || Number(cap) > 20}
                onClick={() => void command("card")}
              >
                Use sandbox test card
              </button>
            )}
          </div>
        </div>
      )}
      {mandate && (
        <div className="actions">
          <span className="help">
            Mandate <code>{mandate.id}</code>
          </span>
          {pending && (
            <button
              className="text-button"
              disabled={busy}
              onClick={() => void command("authorize")}
            >
              Check buyer approval
            </button>
          )}
          {pending && (
            <button
              type="button"
              className="secondary"
              disabled={busy}
              onClick={() => void command("close")}
            >
              Cancel unapproved order
            </button>
          )}
          {!canCreate && (
            <button
              className="secondary"
              disabled={busy}
              onClick={() => void command("close")}
            >
              Close mandate
            </button>
          )}
        </div>
      )}
      {mandate?.accrued_uncaptured_micros != null && (
        <small>
          Awaiting capture · {usd(mandate.accrued_uncaptured_micros)}
        </small>
      )}
      {busy && (
        <p className="help" role="status">
          Waiting for PayPal…
        </p>
      )}
      {message && (
        <p role="status" className="help">
          {message}
        </p>
      )}
      <Failure
        error={error}
        retry={() => {
          setError("");
          setAttempt((n) => n + 1);
          void onUpdate().catch((e) => setError(describe(e)));
        }}
      />
    </section>
  );
}
