export type Health = {
  mode?: string;
  gpu?: string;
  model?: string;
  version?: string;
  paypal_configured?: boolean;
};
export type Source = {
  slug: string;
  title?: string;
  parent: string | null;
  token_len: number;
};
export type ContextRow = {
  id: string;
  title: string;
  slug: string;
  parent_id: string | null;
  publisher_id: string;
  publisher_name: string;
  status: string;
  token_len: number;
  last_probe_hit_ratio: number | null;
  total_hits: number;
  royalty_micros: number;
  price_shared: number;
  last_probe_at?: string;
  can_manage?: boolean;
};
export type Mandate = {
  id: string;
  status: string;
  cap_micros: number;
  captured_micros: number;
  released_micros?: number;
  accrued_uncaptured_micros?: number;
  refunded_micros?: number;
  authorization_id?: string;
  paypal_order_id?: string;
};
export type Run = {
  id: string;
  status: string;
  decision?: string;
  operator_seeded?: boolean;
};
export type State = {
  operator_seeded?: boolean;
  operator_seed_run_count?: number;
  operator_seed_run_ids?: string[];
  workspace_id: string;
  kind: "judge" | "user";
  mandate: Mandate | null;
  run: Run | null;
  sandbox_buyer_email?: string;
  sandbox_buyer_password?: string;
  publisher_receivers?: Record<string, string>;
  next_public_paypal_client_id?: string;
  card_enabled?: boolean;
};
export type Quote = {
  decision: string;
  buy_micros: number;
  compute_micros: number;
  cold_ttft_ms: number | null;
  warm_ttft_ms: number | null;
  reason?: string;
};
export type Task = {
  id: string;
  title: string;
  source_slug: string;
  turns: number;
  text?: string;
};
export type Span = {
  kind?: string;
  slug?: string;
  owner?: string;
  span_owner?: string;
  label?: string;
  start: number;
  end: number;
};
export type Turn = {
  id: string;
  turn: number;
  prompt_tokens: number;
  cached_tokens: number;
  ttft_ms: number;
  charge_micros: number;
  output_tokens?: number;
  text?: string;
  output_text?: string;
  mode: string;
  spans?: Span[];
  spans_json?: Span[];
  sla_breach?: boolean;
  fault_injected_ms?: number;
};
export type Bench = {
  prefix_len: number;
  cold_ttft_ms_p50: number;
  warm_ttft_ms_p50: number;
  model?: string;
  gpu?: string;
};
export type Royalty = {
  id: string;
  parent_id: string | null;
  title: string;
  hit_tokens: number;
  linear_micros: number;
  shapley_micros: number;
};
export type Reconcile = {
  ok: boolean | null;
  checked: number;
  differences: unknown[];
  [key: string]: unknown;
};
export type FinancialRow = Record<string, unknown>;
