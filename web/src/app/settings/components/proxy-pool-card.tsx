"use client";

import { useEffect, useState } from "react";
import { LoaderCircle, Pencil, Plus, RefreshCw, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Card, CardContent } from "@/components/ui/card";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from "@/components/ui/dialog";
import { checkProxyPoolNode, deleteProxyPoolNode, fetchProxyPool, saveProxyPoolNode, setProxyPoolSettings, type ProxyPoolState, type ProxyPoolNode } from "@/lib/api";

const statusLabel: Record<string, string> = { unknown: "待检测", healthy: "可用", cooldown: "冷却中" };
const reasonLabel: Record<string, string> = { network_error: "连接异常", target_blocked: "目标访问受阻", upstream_error: "上游暂时异常", unexpected_response: "响应异常", disabled: "代理停用", deleted: "代理删除" };

export function ProxyPoolCard() {
  const [pool, setPool] = useState<ProxyPoolState | null>(null);
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState<ProxyPoolNode | null>(null);
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [concurrency, setConcurrency] = useState(2);
  const [nodeEnabled, setNodeEnabled] = useState(true);
  const load = async () => { try { setPool(await fetchProxyPool()); } catch (error) { toast.error(error instanceof Error ? error.message : "加载失败"); } };
  useEffect(() => { void load(); const timer = window.setInterval(() => { void fetchProxyPool().then(setPool).catch(() => {}); }, 15000); return () => window.clearInterval(timer); }, []);
  const action = async (run: () => Promise<ProxyPoolState>) => {
    setBusy(true);
    try { setPool(await run()); return true; } catch (error) { toast.error(error instanceof Error ? error.message : "操作失败"); return false; } finally { setBusy(false); }
  };
  const edit = (node: ProxyPoolNode | null) => { setEditing(node); setName(node?.name ?? ""); setUrl(""); setConcurrency(node?.max_concurrency ?? 2); setNodeEnabled(node?.enabled ?? true); setOpen(true); };
  const formatTime = (time: number) => time ? new Date(time * 1000).toLocaleString("zh-CN") : "尚未检测";
  return <Card className="rounded-2xl border-stone-200 bg-white/90"><CardContent className="space-y-5 p-6">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div><h2 className="text-lg font-semibold">代理池</h2><p className="mt-1 text-sm text-stone-500">自动绑定账号，共享出口；确认故障后整批迁移。</p></div>
      <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={pool?.enabled ?? false} disabled={!pool || busy} onChange={e => void action(() => setProxyPoolSettings(e.target.checked, pool?.check_interval_seconds))} />启用代理池</label>
    </div>
    <div className="rounded-xl bg-stone-50 px-4 py-3 text-sm leading-6 text-stone-600">{pool?.enabled ? "已启用：新账号自动分配健康代理，无可用代理时使用统一代理。原有专属代理继续保留。" : "未启用：保持原有代理用法，池内代理不参与请求，也不自动检测。"}<br />每 5 分钟检测；连续 3 次检测异常后切换，冷却后连续 2 次通过再恢复。单次上游 500 不触发迁移。</div>
    <div className="flex gap-2"><Button disabled={busy} onClick={() => edit(null)}><Plus className="size-4" />添加代理</Button><Button variant="outline" disabled={busy} onClick={() => void load()}><RefreshCw className="size-4" />刷新</Button></div>
    <div className="overflow-x-auto"><table className="w-full min-w-[780px] text-left text-sm"><thead className="border-b text-xs text-stone-500"><tr>{["代理", "出口 IP", "状态", "账号 / 在途", "检测", "操作"].map(t => <th key={t} className="px-3 py-3">{t}</th>)}</tr></thead><tbody>
      {pool?.items.map(node => <tr key={node.id} className="border-b border-stone-100"><td className="px-3 py-3"><div className="font-medium">{node.name}</div><div className="text-xs text-stone-400">{node.address}</div></td><td className="px-3 py-3 font-mono text-xs">{node.exit_ip || "—"}</td><td className="px-3 py-3"><span className={node.enabled && node.status === "healthy" ? "text-emerald-600" : "text-amber-600"}>{node.enabled ? statusLabel[node.status] ?? node.status : "已停用"}</span>{node.category && node.category !== "ok" && <div className="text-xs text-stone-400">{reasonLabel[node.category] ?? "检测异常"}</div>}</td><td className="px-3 py-3">{node.bound_accounts} 个 / {node.inflight}<div className="text-xs text-stone-400">并发上限 {node.max_concurrency}</div></td><td className="px-3 py-3 text-xs text-stone-500">{formatTime(node.checked_at)}<div>{node.latency_ms != null ? `${node.latency_ms} ms` : ""}</div></td><td className="px-3 py-3"><div className="flex gap-1"><Button size="sm" variant="outline" disabled={busy} onClick={() => void action(() => checkProxyPoolNode(node.id))}>检测</Button><Button size="icon" variant="ghost" aria-label={`编辑 ${node.name}`} disabled={busy} onClick={() => edit(node)}><Pencil className="size-4" /></Button><Button size="icon" variant="ghost" aria-label={`删除 ${node.name}`} disabled={busy} onClick={() => { if (window.confirm(`删除 ${node.name}？绑定账号将转到可用代理或统一代理。`)) void action(() => deleteProxyPoolNode(node.id)); }}><Trash2 className="size-4 text-rose-500" /></Button></div></td></tr>)}
    </tbody></table>{pool && !pool.items.length && <p className="py-10 text-center text-sm text-stone-400">还没有代理，账号继续使用统一代理。</p>}</div>
    {!!pool?.events.length && <div className="space-y-2"><h3 className="text-sm font-medium">最近切换</h3>{pool.events.slice(-5).reverse().map((event, i) => <div key={`${event.time}-${i}`} className="text-xs text-stone-500">{formatTime(event.time)} · {event.accounts} 个账号：{pool.items.find(n => n.id === event.from_id)?.name ?? "原代理"} → {pool.items.find(n => n.id === event.to_id)?.name ?? "统一代理"} · {reasonLabel[event.reason] ?? "自动切换"}</div>)}</div>}
    <Dialog open={open} onOpenChange={setOpen}><DialogContent><DialogHeader><DialogTitle>{editing ? "编辑代理" : "添加代理"}</DialogTitle></DialogHeader><div className="space-y-4">
      <label className="block space-y-2 text-sm"><span>名称</span><Input value={name} onChange={e => setName(e.target.value)} placeholder="例如：出口 A" /></label>
      <label className="block space-y-2 text-sm"><span>代理地址</span><Input value={url} onChange={e => setUrl(e.target.value)} placeholder={editing ? "留空保留原地址与密码" : "http://用户名:密码@主机:端口"} autoComplete="off" /></label>
      <label className="block space-y-2 text-sm"><span>同一出口生图并发上限</span><Input type="number" min={1} max={20} value={concurrency} onChange={e => setConcurrency(Number(e.target.value))} /></label>
      <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={nodeEnabled} onChange={e => setNodeEnabled(e.target.checked)} />允许分配此代理</label>
    </div><DialogFooter><Button variant="outline" onClick={() => setOpen(false)} disabled={busy}>取消</Button><Button disabled={busy || !name.trim() || (!editing && !url.trim()) || concurrency < 1 || concurrency > 20} onClick={async () => { if (await action(() => saveProxyPoolNode({ name, ...(url.trim() ? { url: url.trim() } : {}), enabled: nodeEnabled, max_concurrency: concurrency }, editing?.id))) { setOpen(false); toast.success("已保存；新地址检测通过后才会分配"); } }}>{busy && <LoaderCircle className="size-4 animate-spin" />}保存</Button></DialogFooter></DialogContent></Dialog>
  </CardContent></Card>;
}
