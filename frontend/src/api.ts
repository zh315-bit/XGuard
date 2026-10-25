/** 后端（guard/api.py）访问。开发时经 Vite 的 /guard 代理到 127.0.0.1:8100。 */
import { useCallback, useEffect, useRef, useState } from "react";

export const API = "/guard";
export const POLL_MS = 2000;

export async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(API + path);
  if (!res.ok) throw new Error(`${res.status} ${await errorText(res)}`);
  return res.json();
}

export async function postJson<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(API + path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) throw new Error(await errorText(res));
  return res.json();
}

async function errorText(res: Response): Promise<string> {
  try {
    const data = await res.json();
    return typeof data.detail === "string" ? data.detail : data.detail?.message ?? JSON.stringify(data.detail ?? data);
  } catch {
    return res.statusText;
  }
}

/** 每 2 秒拉一次；返回最近一次成功的数据和最近一次错误。 */
export function usePoll<T>(path: string, intervalMs = POLL_MS) {
  const [data, setData] = useState<T>();
  const [error, setError] = useState<string>();
  const alive = useRef(true);

  const refresh = useCallback(async () => {
    try {
      const next = await getJson<T>(path);
      if (alive.current) {
        setData(next);
        setError(undefined);
      }
    } catch (e) {
      if (alive.current) setError(e instanceof Error ? e.message : String(e));
    }
  }, [path]);

  useEffect(() => {
    alive.current = true;
    refresh();
    const timer = setInterval(refresh, intervalMs);
    return () => {
      alive.current = false;
      clearInterval(timer);
    };
  }, [refresh, intervalMs]);

  return { data, error, refresh };
}

// ---- 后端返回的数据形状（只列控制台用到的字段） ----

export type Quote = { amount_xrp: string; asset: string; pay_to: string };
export type Risk = {
  risk_summary: string;
  supporting_facts: string[];
  suggested_review_questions: string[];
  source: "llm" | "template";
};

export type Payment = {
  id: number;
  agent_id: string;
  request_id: string;
  created_at: number;
  updated_at?: number;
  url: string;
  purpose: string;
  method?: "x402" | "escrow" | "topup";
  status: string;
  state: string;
  verdict: string;
  rule_id: string;
  reason: string;
  quote?: Quote;
  tx_hash?: string;
  explorer_url?: string;
  approval_id?: number;
  risk?: Risk | null;
  error?: string;
};

export type SigningRequest = {
  id: number;
  status: "pending" | "signed" | "rejected" | "expired";
  error?: string;
  unsigned_tx_hex: string;
  tx_json: Record<string, unknown>;
  destination: string;
  amount_drops: string;
  account: string;
  expires_in_seconds: number;
};

export type Approval = {
  id: number;
  created_at: string;
  url: string;
  purpose: string;
  rule_id: string;
  reason: string;
  status: "pending" | "approving" | "approved" | "rejected" | "expired";
  quote: { amount: string; payTo: string; asset: string; network: string };
  risk?: Risk | null;
  expires_in_seconds?: number;
  payment_state?: string;
  payment_error?: string;
  reject_reason?: string;
  decided_at?: string | null;
  tx_hash?: string;
  explorer_url?: string;
  signing_request?: SigningRequest | null;
  method?: "x402" | "escrow" | "topup";
  payee_unverified?: boolean;
  acceptance?: {
    description?: string | null;
    resourceDescription?: string | null;
    dataSchema?: { required?: string[] } & Record<string, unknown> | null;
    delivery?: { mimeType?: string | null; escrow?: unknown } | null;
  } | null;
  escrow?: Escrow | null;
  budget_impact: {
    amount_xrp: string;
    total_committed_today_xrp: string;
    daily_total_cap_xrp: string;
    after_approval_xrp?: string;
    within_cap: boolean;
  };
};

export type Escrow = {
  id: number;
  payment_request_id: number;
  approval_id?: number;
  created_at: number;
  owner: string;
  destination: string;
  amount_drops: number;
  condition: string;
  cancel_after: number;
  cancel_after_unix: number;
  refund_in_seconds: number;
  description?: string;
  create_tx?: string;
  create_tx_url?: string;
  finish_tx?: string;
  finish_tx_url?: string;
  cancel_tx?: string;
  cancel_tx_url?: string;
  verification_hash?: string;
  status: string;
  payment_state?: string | null;
  payment_error?: string | null;
  delivery?: unknown;
  verification?: {
    verdict: "pass" | "fail";
    reason: string;
    evidence: string[];
    source: string;
    checks: { name: string; ok: boolean; detail: string }[];
  };
};

export type Config = {
  signer_mode: "speculos" | "software";
  speculos_url: string;
  speculos_model: string;
  demo_mode: boolean;
  treasury_address: string;
  device_address?: string;
};

export type Account = {
  address: string;
  balance_xrp?: number;
  regular_key?: string;
  master_disabled?: boolean;
  explorer_url: string;
  error?: string | null;
};

export type Security = {
  master_disabled: boolean;
  disable_master_tx?: string | null;
  disable_master_tx_url?: string | null;
  regular_key?: string | null;
  regular_key_url?: string | null;
  regular_key_is_device: boolean;
  hot_wallet_cap_xrp: number;
  hot_wallet_balance_xrp?: number;
  hot_wallet_url: string;
  treasury_url: string;
};

export type Treasury = {
  security: Security;
  hot_wallet: Account & { cap_xrp: number; within_cap: boolean };
  treasury: Account;
  signer_mode: string;
  device_address?: string;
  treasury_controlled_by_device: boolean;
  budget: Record<string, string>;
  budget_detail: Record<string, string>;
};

export type MissionEvent = { id: number; ts: number; kind: "text" | "tool_call" | "tool_result"; name?: string; payload: any };
export type Mission = {
  id: number;
  created_at: number;
  task: string;
  status: "running" | "done" | "failed" | "cancelled" | "llm_unavailable";
  source?: "web" | "cli";
  report?: string;
  events: MissionEvent[];
};

export type AuditRow = {
  id: number;
  created_at: string;
  url: string;
  verdict: string;
  rule_id: string;
  reason: string;
  status: string;
  amount_xrp?: string;
  pay_to?: string;
  tx_hash?: string;
  explorer_url?: string;
  payer?: string | null;
};
