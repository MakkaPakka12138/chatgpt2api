"use client";

import { formatBrowserDateTime } from "@/lib/date-time";

const labels: Record<string, string> = {
  client_setup: "建立客户端", bootstrap: "连接 ChatGPT 首页",
  requirements_prepare: "准备对话凭证", requirements_finalize: "获取对话凭证",
  conversation_submit: "提交对话", conversation_stream: "接收回复",
  tls_connect: "TLS 安全连接失败", dns_failed: "域名解析失败",
  certificate_verify: "TLS 证书验证失败",
  connect_failed: "无法建立连接", timeout: "连接或读取超时",
  connection_reset: "连接中断", token_invalid: "账号登录凭证失效",
  quota_limited: "上游限额", http_error: "上游返回错误", other: "其他错误",
  reconnect: "等待 1 秒后重新连接", refresh_or_switch_account: "刷新凭证或切换账号",
  stop_reply_started: "已开始回复，停止重试", stop_upstream_accepted: "对话已被接受，停止重试",
  stop: "停止重试", global: "统一代理", pool: "代理池", account: "账号专用代理",
  direct: "直连", runtime: "运行时代理", success: "成功", failed: "失败",
  cancelled: "已取消", running: "进行中",
};

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function label(value: unknown) {
  const text = String(value || "-");
  return labels[text] || text;
}

function duration(value: unknown) {
  return typeof value === "number" ? `${(value / 1000).toFixed(2)} s` : "-";
}

export function NetworkTrace({ value }: { value: unknown }) {
  const trace = record(value);
  if (!Array.isArray(trace.attempts) || !trace.attempts.length) return null;
  return (
    <div className="space-y-3 rounded-xl border border-stone-200 bg-white p-4 text-sm">
      <div>
        <h3 className="font-medium text-stone-700">连接与重试记录</h3>
        <p className="mt-1 text-xs text-stone-500">
          共 {trace.attempts.length} 次尝试 · 重试 {Number(trace.retry_count) || 0} 次
          {trace.recovered ? " · 重试后恢复成功" : ""}
        </p>
      </div>
      {trace.attempts.map((value, index) => {
        const attempt = record(value);
        const steps = Array.isArray(attempt.steps) ? attempt.steps.map(record) : [];
        return (
          <div key={index} className="space-y-2 border-t border-stone-100 pt-3">
            <div className="flex flex-wrap justify-between gap-2">
              <span className="font-medium text-stone-700">第 {Number(attempt.attempt) || index + 1} 次 · {label(attempt.status)}</span>
              <span className="text-xs text-stone-500">{duration(attempt.duration_ms)}</span>
            </div>
            <p className="break-all text-xs text-stone-500">
              {String(attempt.account_email || attempt.account_id || "匿名")}
              {" · "}{label(attempt.proxy_source)}{": "}{String(attempt.proxy || "-")}
            </p>
            {typeof attempt.started_at === "string" ? <p className="text-xs text-stone-400">{formatBrowserDateTime(attempt.started_at)}</p> : null}
            {steps.map((step, stepIndex) => (
              <div key={stepIndex} className="flex flex-wrap justify-between gap-2 text-xs text-stone-500">
                <span className="break-all" title={String(step.target || "")}>{label(step.stage)}</span>
                <span>{step.error_category ? label(step.error_category) : `HTTP ${String(step.http_status || "-")}`} · {duration(step.duration_ms)}</span>
              </div>
            ))}
            {attempt.error_category ? (
              <p className="text-xs font-medium text-amber-600 dark:text-amber-400">
                {label(attempt.stage)}：{label(attempt.error_category)}
                {typeof attempt.curl_code === "number" ? `（curl ${attempt.curl_code}）` : ""}
              </p>
            ) : null}
            {attempt.retry_action ? <p className="text-xs text-stone-600">处理：{label(attempt.retry_action)}</p> : null}
          </div>
        );
      })}
    </div>
  );
}
