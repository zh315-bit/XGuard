/** 规则编号 → 人话。界面默认显示人话，编号只放在悬停提示里。 */
export const RULE_TEXT: Record<string, string> = {
  DEFAULT: "All checks passed, paid automatically",
  LOCKDOWN: "Security incident in progress; payments are paused",
  PROBE: "Fetching the resource failed (error status, connection failure or timeout); nothing was paid",
  R0: "The merchant's quote is malformed and cannot be evaluated; denied",
  R1: "Network, asset or payment scheme not supported; denied",
  R2: "Payee is on the blocklist; denied",
  R3: "Payee has no verified on-chain credential; needs human approval",
  R4: "Above the per-payment auto limit; needs human approval",
  R5: "Over today's budget; needs human approval",
  R6: "Too many payments to the same merchant in a short time; needs human approval",
  R7: "Above the per-payment hard cap; denied",
  R8: "Payee or terms changed before payment; blocked",
  MAX_PRICE: "Quote exceeds the agent's own max price; denied",
  ESCROW: "Large purchase: funds go into escrow and are released only after the delivery is verified",
  TOPUP: "Top-up from the treasury to the hot wallet; needs human approval",
};

export const VERDICT_TEXT: Record<string, string> = {
  AUTO: "Auto-paid",
  HOLD: "Needs approval",
  DENY: "Denied",
  ERROR: "Request failed",
};

export function ruleText(rule?: string | null): string {
  if (!rule || rule === "-") return "";
  return RULE_TEXT[rule] ?? `Rule ${rule}`;
}

/** 人话说明；鼠标悬停显示规则编号。 */
export function RuleText({ rule, className = "text-xs text-slate-400" }: { rule?: string | null; className?: string }) {
  if (!rule || rule === "-") return null;
  return (
    <span className={`${className} cursor-help decoration-dotted underline-offset-2 hover:underline`} title={`Rule ID ${rule}`}>
      {ruleText(rule)}
    </span>
  );
}
