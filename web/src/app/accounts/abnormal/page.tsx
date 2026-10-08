"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { ArrowLeft, CircleAlert, LoaderCircle, RefreshCw, Search, ShieldCheck, Trash2 } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { deleteAbnormalAccount, fetchAbnormalAccounts, recoverAbnormalAccount, type AbnormalAccount } from "@/lib/api";
import { useAuthGuard } from "@/lib/use-auth-guard";
import { formatBrowserDateTime } from "@/lib/date-time";

function formatDate(value?: string | null) {
  return formatBrowserDateTime(value);
}

function tokenLabel(token: string) {
  return token.length > 16 ? `${token.slice(0, 8)}…${token.slice(-6)}` : "已保存凭据";
}

function AbnormalAccountsContent() {
  const [items, setItems] = useState<AbnormalAccount[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [query, setQuery] = useState("");
  const [busyToken, setBusyToken] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<AbnormalAccount | null>(null);
  const [page, setPage] = useState(1);
  const pageSize = 20;

  const load = useCallback(async () => {
    try {
      const data = await fetchAbnormalAccounts();
      setItems(data.items);
      setLoadError("");
    } catch (error) {
      setLoadError(error instanceof Error ? error.message : "加载异常账号失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (busyToken) return;
    void load();
    const timer = window.setInterval(() => void load(), 15000);
    return () => window.clearInterval(timer);
  }, [load, busyToken]);

  const filtered = useMemo(() => {
    const value = query.trim().toLowerCase();
    return items.filter((item) => !value || [item.email, item.access_token, item.type, item.status,
      item.quarantine_reason, item.last_refresh_error, item.quarantine_event].some((field) => field?.toLowerCase().includes(value)));
  }, [items, query]);
  const pageCount = Math.max(1, Math.ceil(filtered.length / pageSize));
  const safePage = Math.min(page, pageCount);
  const rows = filtered.slice((safePage - 1) * pageSize, safePage * pageSize);
  const quarantined = items.filter((item) => item.quarantined_at).length;

  const recover = async (item: AbnormalAccount) => {
    setBusyToken(item.access_token);
    try {
      const result = await recoverAbnormalAccount(item.access_token);
      setItems(result.items);
      if (result.restored) toast.success("验证通过，账号已恢复到号池");
      else toast.error(result.errors[0]?.error || "验证未通过，账号资料仍保留");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "恢复失败");
    } finally {
      setBusyToken(null);
    }
  };

  const confirmDelete = async () => {
    if (!deleting) return;
    setBusyToken(deleting.access_token);
    try {
      const result = await deleteAbnormalAccount(deleting.access_token);
      setItems(result.items);
      setDeleting(null);
      toast.success(result.removed ? "异常账号记录已删除" : "账号已恢复或记录已不存在，请刷新列表");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "删除失败");
    } finally {
      setBusyToken(null);
    }
  };

  return (
    <section aria-labelledby="abnormal-accounts-title" className="mx-auto max-w-7xl space-y-6 px-4 py-8 sm:px-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <Link href="/accounts" className="mb-3 inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground">
            <ArrowLeft className="size-4" />返回号池管理
          </Link>
          <h1 id="abnormal-accounts-title" className="flex items-center gap-2 text-2xl font-semibold tracking-tight"><CircleAlert className="size-6 text-amber-600" />异常账号</h1>
          <p className="mt-2 max-w-2xl text-sm leading-6 text-muted-foreground">
            自动隔离的账号保留资料和原因，不参与请求调度。验证通过后可恢复到号池；验证失败仍保留记录。
          </p>
        </div>
        <Button variant="outline" onClick={() => void load()} disabled={loading || busyToken !== null}>
          <RefreshCw className="size-4" />刷新列表
        </Button>
      </div>

      <div className="grid gap-3 sm:grid-cols-3">
        {[{ label: "待处理账号", count: items.length }, { label: "已移入隔离列表", count: quarantined },
          { label: "号池中标记异常", count: items.length - quarantined }].map((stat) => (
          <Card key={stat.label}><CardContent className="p-5"><p className="text-sm text-muted-foreground">{stat.label}</p><p className="mt-2 text-3xl font-semibold">{stat.count}</p></CardContent></Card>
        ))}
      </div>

      <Card>
        <CardContent className="space-y-4 p-4 sm:p-6">
          <div className="relative max-w-md">
            <Search className="absolute left-3 top-3 size-4 text-muted-foreground" />
            <Input aria-label="搜索异常账号" placeholder="搜索邮箱、状态或异常原因" className="pl-9" value={query}
              onChange={(event) => { setQuery(event.target.value); setPage(1); }} />
          </div>
          {loadError ? <p role="alert" className="text-sm text-destructive">{loadError}</p> : null}
          {loading ? <div className="flex justify-center py-12"><LoaderCircle className="size-6 animate-spin" aria-label="加载中" /></div> : (
            <Table>
              <TableHeader><TableRow>
                <TableHead>账号</TableHead><TableHead>状态</TableHead><TableHead>异常原因</TableHead><TableHead>记录时间</TableHead><TableHead>操作</TableHead>
              </TableRow></TableHeader>
              <TableBody>
                {rows.map((item) => (
                  <TableRow key={item.access_token}>
                    <TableCell className="align-top"><div className="font-medium">{item.email || "未记录邮箱"}</div><div className="mt-1 text-xs text-muted-foreground">{tokenLabel(item.access_token)} · {item.type || "free"} · {item.source_type || "web"}</div></TableCell>
                    <TableCell className="align-top"><Badge variant={item.status === "限流" ? "warning" : "danger"}>{item.status}</Badge><div className="mt-2 text-xs text-muted-foreground">{item.quarantined_at ? "已隔离" : "停止调度"}</div></TableCell>
                    <TableCell className="min-w-64 max-w-md whitespace-pre-wrap break-words align-top text-sm">
                      {item.quarantine_reason || item.last_refresh_error || "未记录详细原因"}
                      {item.quarantine_event ? <div className="mt-2 break-all text-xs text-muted-foreground">来源：{item.quarantine_event}</div> : null}
                    </TableCell>
                    <TableCell className="align-top text-xs text-muted-foreground">{formatDate(item.quarantined_at || item.last_invalid_at || item.last_refresh_error_at)}</TableCell>
                    <TableCell className="align-top"><div className="flex flex-wrap gap-2">
                      <Button size="sm" variant="outline" disabled={busyToken !== null} onClick={() => void recover(item)}>
                        {busyToken === item.access_token && !deleting ? <LoaderCircle className="size-4 animate-spin" /> : <ShieldCheck className="size-4" />}验证并恢复
                      </Button>
                      <Button size="sm" variant="ghost" className="text-destructive" disabled={busyToken !== null} onClick={() => setDeleting(item)}><Trash2 className="size-4" />删除</Button>
                    </div></TableCell>
                  </TableRow>
                ))}
                {rows.length === 0 ? <TableRow><TableCell colSpan={5} className="py-12 text-center text-muted-foreground">{query ? "没有匹配的账号" : "暂无异常账号记录。此前已删除的账号需从备份重新导入。"}</TableCell></TableRow> : null}
              </TableBody>
            </Table>
          )}
          <div className="flex items-center justify-between text-sm text-muted-foreground">
            <span>共 {filtered.length} 条 · 第 {safePage} / {pageCount} 页</span>
            <div className="flex gap-2"><Button size="sm" variant="outline" disabled={safePage <= 1} onClick={() => setPage(safePage - 1)}>上一页</Button><Button size="sm" variant="outline" disabled={safePage >= pageCount} onClick={() => setPage(safePage + 1)}>下一页</Button></div>
          </div>
        </CardContent>
      </Card>

      <Dialog open={deleting !== null} onOpenChange={(open) => { if (!open && !busyToken) setDeleting(null); }}>
        <DialogContent><DialogHeader><DialogTitle>删除异常账号记录</DialogTitle><DialogDescription>将删除 {deleting?.email || "该账号"} 的本地资料和凭据，此操作不能撤销。自动隔离会保留资料，仅手动删除才会清除。</DialogDescription></DialogHeader>
          <DialogFooter><Button variant="outline" disabled={busyToken !== null} onClick={() => setDeleting(null)}>取消</Button><Button variant="destructive" disabled={busyToken !== null} onClick={() => void confirmDelete()}>{busyToken ? <LoaderCircle className="size-4 animate-spin" /> : null}确认删除</Button></DialogFooter>
        </DialogContent>
      </Dialog>
    </section>
  );
}

export default function AbnormalAccountsPage() {
  const { isCheckingAuth, session } = useAuthGuard(["admin"]);
  if (isCheckingAuth) return <div className="flex justify-center p-12"><LoaderCircle className="size-6 animate-spin" aria-label="验证权限" /></div>;
  return session?.role === "admin" ? <AbnormalAccountsContent /> : null;
}
