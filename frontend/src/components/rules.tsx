/** 规则编号 → 人话。界面默认显示人话，编号只放在悬停提示里。 */
export const RULE_TEXT: Record<string, string> = {
  DEFAULT: "各项检查都通过，自动付款",
  PROBE: "请求资源失败（错误状态码、连接失败或超时），没有付款",
  R0: "商户的报价格式不对，无法判断，已拒绝",
  R1: "网络、币种或付款方式不受支持，已拒绝",
  R2: "收款方在黑名单上，已拒绝",
  R3: "收款方未通过链上认证，需要人工确认",
  R4: "超过单笔自动上限，需要人工确认",
  R5: "超出今日预算，需要人工确认",
  R6: "短时间内向同一商户付款太频繁，需要人工确认",
  R7: "超过单笔硬上限，已拒绝",
  R8: "收款地址或交易条件在付款前被替换，已拦截",
  MAX_PRICE: "报价超过 agent 自己设定的最高价，已拒绝",
  ESCROW: "大额购买：先锁款托管，验货通过才付给商户",
  TOPUP: "从金库给热钱包补款，需要人工确认",
};

export const VERDICT_TEXT: Record<string, string> = {
  AUTO: "自动付款",
  HOLD: "待人工确认",
  DENY: "已拒绝",
  ERROR: "请求失败",
};

export function ruleText(rule?: string | null): string {
  if (!rule || rule === "-") return "";
  return RULE_TEXT[rule] ?? `规则 ${rule}`;
}

/** 人话说明；鼠标悬停显示规则编号。 */
export function RuleText({ rule, className = "text-xs text-slate-400" }: { rule?: string | null; className?: string }) {
  if (!rule || rule === "-") return null;
  return (
    <span className={`${className} cursor-help decoration-dotted underline-offset-2 hover:underline`} title={`规则编号 ${rule}`}>
      {ruleText(rule)}
    </span>
  );
}
