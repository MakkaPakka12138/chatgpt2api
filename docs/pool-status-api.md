# 号池状态缓存接口

`GET /api/pool/status`，请求头 `Authorization: Bearer <管理员密钥>`。无有效密钥返回401，普通用户密钥返回403。响应只包含汇总，不返回账号Token、密码、邮箱或代理凭据。

启动时初始化内存缓存，之后每5分钟（300秒）从本地账号管理状态生成快照。查询只读缓存，不访问ChatGPT；原账号定时刷新任务仍按原配置更新上游额度。客户端无需反复调用账号刷新接口。

响应字段：

| 字段 | 内容 |
| --- | --- |
| `status` | 至少一个正常账号有生图额度时为 `ok`，否则为 `degraded`；表示本地生图池状态，不保证上游请求成功 |
| `version`、`scheduling_mode` | 应用版本及轮询／顺序调度模式 |
| `accounts.total`、`in_pool`、`quarantined` | 当前所有保留账号数、调度池内账号数、隔离账号数；total为后两者之和 |
| `accounts.normal`、`limited`、`abnormal`、`disabled` | 调度池内各状态数量；abnormal不重复计入quarantined |
| `accounts.by_type`、`by_source` | 调度池内套餐及来源分布 |
| `accounts.cumulative_total` | 原有累计入库计数 |
| `images.remaining_total` | 正常且有生图余量的账号额度之和 |
| `images.eligible_accounts`、`inflight` | 有生图额度的正常账号数、当前在途图片数；eligible不扣除已占满并发槽位的账号 |
| `images.success_total`、`fail_total` | 调度池内账号的累计成功／失败计数 |
| `uploads.known_remaining_total` | 调度池内Web账号的已知上传余量总和，含禁用等账号；不含未知余量及Codex账号 |
| `uploads.available_known_remaining` | 符合新上传条件账号的已知上传余量之和 |
| `uploads.unknown_accounts`、`low_accounts`、`blocked_accounts` | Web账号未知额度、低于20、上传冷却中数量；这些分类可能重叠 |
| `uploads.eligible_accounts` | 正常、有生图余量、没有上传冷却且上传余量未知或至少20的Web账号数；不考虑缓存命中或本次具体参考图数量 |
| `uploads.not_applicable_accounts`、`switch_threshold` | Codex账号数及提前切号门槛20 |
| `images.next_reset_at`、`uploads.next_reset_at` | 已知且尚未到达的最早恢复时间，UTC格式；缺失为null |
| `cache.updated_at`、`age_seconds` | 缓存生成时间及距生成的秒数 |
| `cache.refresh_interval_seconds`、`stale` | 定时更新间隔300秒；超过10分钟未更新时stale为true，保留上一次成功快照 |
| `upstream_refresh_interval_minutes` | 原账号定时刷新配置；缓存更新时间不等于上游额度查询时间 |

缓存仅在当前进程内保存，不修改现有配置或运行数据。多进程部署时每个进程独立维护。参考图缓存命中时可以使用低上传额度账号，因此新上传eligible不是所有图生图请求的最终可用账号数。

```bash
curl 'http://192.168.3.3:13000/api/pool/status' \
  -H 'Authorization: Bearer 你的管理员密钥'
```
