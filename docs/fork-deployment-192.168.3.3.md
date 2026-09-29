# 在 192.168.3.3 升级到 fork，保留原数据

## 已核实的部署状态（2026-09-29）

- SSH：`server@192.168.3.3`。
- 原部署目录：`/home/server/code/chatgpt2api`。
- 容器：`chatgpt2api`，服务名 `app`。
- 原镜像：`ghcr.io/basketikun/chatgpt2api:latest`；镜像标签显示版本 `1.8.0`。
- 原源码仓库：`https://github.com/basketikun/chatgpt2api.git`。
- 访问地址：`http://192.168.3.3:13000`。
- 实际存储类型：`STORAGE_BACKEND=json`。
- 原数据挂载：`/home/server/code/chatgpt2api/data` 到 `/app/data`。
- 原配置挂载：`/home/server/code/chatgpt2api/config.json` 到 `/app/config.json`。
- 服务器的 `config.json` 与 `docker-compose.yml` 有本地修改，需要保留。

本次本地修改仍属于 1.8.0 的存储结构，不切换数据库、不运行数据迁移。账号、鉴权密钥、日志、任务与本地图片继续从原 `data/` 读取。异常账号隔离记录也写入现有存储。参考图上传缓存是内存态，容器替换后重新建立。

## 1. 先发布本地代码到自己的 fork

本地仓库位于 `D:\persons\chatgpt2api`，远程是 `https://github.com/MakkaPakka12138/chatgpt2api.git`，当前分支是 `main`。

先检查本次代码，再提交、推送。必须包含本次新增文件（上传缓存、异常账号页面、测试、文档和覆盖文件）。不要把运行数据、服务器配置、账号凭据或 `.env` 加入提交；本仓库的 `config.json` 本身已被 Git 跟踪，更不能用服务器实值覆盖后提交。

尚未提交推送的本地改动不会被服务器 `git pull` 获取。发布后记录提交 SHA，部署时可将源码固定到该 SHA，便于追踪和回滚。

## 2. 在服务器单独准备 fork 源码

以下命令在服务器 Bash 中执行。不要覆盖原部署目录。

```bash
cd /home/server/code
git clone https://github.com/MakkaPakka12138/chatgpt2api.git chatgpt2api-fork
cd /home/server/code/chatgpt2api-fork
git log -1 --oneline
```

若 fork 源码目录已存在，先确认其远程、分支和工作区，再在干净的正确分支执行 `git pull --ff-only`，避免再次 clone。

## 3. 加上镜像覆盖文件，先构建

保留原 `docker-compose.yml`，仅叠加 fork 的构建来源。

```bash
cp /home/server/code/chatgpt2api-fork/deploy/compose.fork.override.yml /home/server/code/chatgpt2api/compose.fork.override.yml
cd /home/server/code/chatgpt2api
docker compose -f docker-compose.yml -f compose.fork.override.yml build app
```

该覆盖文件只指定新镜像和构建上下文，沿用原部署的端口、挂载、环境变量和重启策略。构建期间原容器继续服务。以后每次发布应使用新版本镜像标签，避免覆盖之前部署的标签。

不要改用 `docker-compose.local.yml`：它的端口、容器名与默认存储类型和此服务器不同。仅更换 Git remote 也不会更换正在运行的官方镜像。

## 4. 保存旧镜像，短暂停机后备份

以下代码按顺序在同一个 Bash 会话执行。备份存放在私有目录，包含账号凭据和配置。

```bash
set -e
cd /home/server/code/chatgpt2api
deploy_stamp=$(date +%Y%m%d-%H%M%S)
backup_dir="/home/server/backups/chatgpt2api-$deploy_stamp"
install -d -m 700 "$backup_dir"
rollback_image="chatgpt2api-fork:rollback-$deploy_stamp"
docker tag "$(docker inspect --format '{{.Image}}' chatgpt2api)" "$rollback_image"
printf '%s\n' "$rollback_image" > "$backup_dir/rollback-image.txt"
docker compose stop app
backup_files=(data config.json docker-compose.yml)
if [ -f .env ]; then backup_files+=(.env); fi
if ! tar -czf "$backup_dir/state.tgz" "${backup_files[@]}"; then
  docker compose start app
  exit 1
fi
printf '备份目录：%s\n' "$backup_dir"
```

先停 app 再备份，避免刷新账号、写日志或写任务时产生不一致快照。备份失败会重新启动旧容器并停止后续步骤。不要清理旧镜像或备份，直到新版本稳定。

## 5. 用新镜像重建 app，仍挂载原数据

```bash
cd /home/server/code/chatgpt2api
docker compose -f docker-compose.yml -f compose.fork.override.yml up -d --no-deps --no-build --pull never app
docker compose -f docker-compose.yml -f compose.fork.override.yml ps
docker logs --tail 80 chatgpt2api
docker inspect --format '{{range .Mounts}}{{.Source}} {{.Destination}}{{println}}{{end}}' chatgpt2api
```

确认仍挂载原 `data/` 和 `config.json`，并访问 `http://192.168.3.3:13000`：原登录密钥可用；账号数量、用户密钥、历史任务和图片仍在；管理员导航出现“异常账号”；账号列表出现“上传额度”。上游未返回上传余量时显示“未知”是正常行为。

不要使用带 `-v` 的容器清理命令、删除原数据目录，或将本地开发 `data/`、`config.json`、`.env` 覆盖到服务器。升级不需要重新导入已有账号。

## 6. 如需回滚

复用第 4 步记录的旧镜像，不依赖会变化的官方 `latest`。在原部署目录新建 `compose.rollback.yml`（替换为实际保存的旧镜像标签）：

```yaml
services:
  app:
    image: chatgpt2api-fork:rollback-实际时间戳
```

然后执行：

```bash
cd /home/server/code/chatgpt2api
docker compose -f docker-compose.yml -f compose.rollback.yml up -d --no-deps --no-build --pull never app
```

旧镜像会继续挂载原数据。如果必须把数据也恢复到升级前，应先停止服务并额外备份升级后数据，再从 `state.tgz` 恢复。恢复旧数据会丢弃升级后新增记录，不应在服务写入期间解压覆盖。通常先回滚镜像，再判断是否真的需要恢复数据。

## 后续升级

继续使用原部署目录中的两个 Compose 文件启动服务；仅执行原 `docker compose up` 会重新使用原官方镜像。每次升级先更新 fork 源码、改用新的镜像标签、构建，再重复备份和替换步骤。
