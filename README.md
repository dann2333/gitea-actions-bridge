# gitea-gh-bridge

一个 Python 文件，零第三方依赖。收到签名有效、标签匹配的 Gitea `workflow_job: queued` 事件后，触发指定 GitHub workflow。

只负责中转，不包含 Gitea Runner、不复制源码，也不把 Gitea 事件内容传给 GitHub。

## 部署

仓库的 Docker workflow 在推送 main 时发布 `ghcr.io/<owner>/<repo>:latest`，推送 `v*` 标签时发布同名镜像标签，支持 amd64 / arm64。首次发布后，如需免登录拉取，在 GitHub Package settings 将镜像设为 Public；公开仓库不保证新建 package 自动公开。

创建 `.env`（不要提交，建议 `chmod 600 .env`）：

```dotenv
WEBHOOK_SECRET=替换成随机长密钥
GITHUB_TOKEN=替换成GitHub令牌
GITHUB_REPOSITORY=你的账号/运行器仓库
GITHUB_WORKFLOW=runner.yml
GITHUB_REF=main
RUNNER_LABEL=github-hosted
```

GitHub 令牌使用 fine-grained PAT，只授权目标仓库，权限 `Actions: Read and write`。目标 workflow 必须已在默认分支定义 `workflow_dispatch`，并负责启动一次性 Gitea Runner。这里的 `GITHUB_REPOSITORY` 是**接收触发的仓库**，不一定是本项目。

```sh
docker run -d --name gitea-gh-bridge --restart unless-stopped \
  --env-file .env -p 127.0.0.1:8080:8080 \
  -v gitea-gh-bridge-data:/data \
  ghcr.io/<owner>/<repo>:latest
```

SQLite 默认保存在容器 `/data/bridge.db`（可通过 `SQLITE_PATH` 修改），上面的命名卷保证更新镜像或重建容器后记录仍在。容器以 UID/GID `65534:65534` 运行；若改用宿主目录挂载，须让该用户可写目录。不要删除数据卷。直接运行 Python 时默认使用当前目录的 `bridge.db`。

Gitea 28.4.0 仓库设置 → Webhooks → Gitea：

- 地址：`https://你的中转域名/webhook`（由现有反向代理转到本服务 8080）。
- POST、`application/json`。
- Secret：与 `WEBHOOK_SECRET` 相同；不需要配置 Authorization。
- 自定义事件：只勾选 Workflow Job。
- Gitea 构建 job 使用 `runs-on: github-hosted`（或修改 `RUNNER_LABEL`）。

同机容器也可通过私有 Docker 网络互通，无需公开端口；此时 webhook URL 使用服务名而非 `127.0.0.1`。若 Gitea 阻止访问私网 webhook，按其配置仅允许具体中转主机，不要开放任意目的地。

## 行为与限制

- HMAC-SHA256 校验原始请求体；只转发固定 `{"ref":"main"}`，不转发源码、仓库名或 Gitea token。
- `/health` 返回 200；触发成功且已落库返回 202，忽略/已成功重复返回 200，签名错误返回 401，执行中或结果不确定返回 409，上游失败返回 502，数据库故障返回 503。
- SQLite 按目标 workflow/ref、Gitea 仓库 ID、job ID、attempt 去重，重启后仍有效。先提交 `pending`，再请求 GitHub，成功后写为 `sent`；不同任务可并发请求 GitHub，同一任务只允许一个请求进入。
- 明确的 GitHub 4xx 拒绝（408 除外）释放记录，修正配置后可重投。不自动重试。超时、网络错误、5xx、进程崩溃或成功后落库失败会留下 `pending`，再次投递返回 409，**不会自动再次触发**。
- `pending` 不能证明 GitHub 已接收，也不能证明未接收。SQLite 与 GitHub API 没有共同事务，这不是 exactly-once。先核对 GitHub 运行记录，再决定处理：已触发则标记 `sent`；确认未触发才删除该记录并从 Gitea 重投。无法确认时保留，不能盲目清库。
- 不记录请求体、token 或上游响应。只接受你自己的可信仓库，服务放在反向代理后。它不是持久队列、扩缩容控制器或严格的 exactly-once 调度器。
- Runner 根据作用域和标签领取任务，不保证某次 webhook 创建的 Runner 恰好领取该 job。目标 runner workflow 应有空闲超时，处理完即退出。
- 这不会改变 GitHub Actions 的条款或配额；不要把它作为与目标 GitHub 项目无关的通用算力池。

## 处理不确定记录

先重启中转容器，结束尚在执行的请求，然后查看记录：

```sh
docker exec gitea-gh-bridge python -c 'import os,sqlite3; db=sqlite3.connect(os.environ["SQLITE_PATH"]); print(db.execute("SELECT target,repo,job,attempt,state FROM dispatches WHERE state=\"pending\"").fetchall())'
```

核对 GitHub 后，按查询结果填写下面的 TARGET、REPO_ID、JOB_ID、ATTEMPT。最后一个参数用 `sent` 表示 GitHub 已触发；**确认未触发**才改成 `retry`，清除记录后从 Gitea 重投：

```sh
docker exec -i gitea-gh-bridge python - 'TARGET' REPO_ID JOB_ID ATTEMPT sent <<'PYTHON'
import os, sqlite3, sys
target, repo, job, attempt, mode = sys.argv[1:]
assert mode in ("sent", "retry")
key = (target, int(repo), int(job), int(attempt))
with sqlite3.connect(os.environ["SQLITE_PATH"]) as db:
    where = "WHERE target=? AND repo=? AND job=? AND attempt=? AND state='pending'"
    sql = "UPDATE dispatches SET state='sent' " if mode == "sent" else "DELETE FROM dispatches "
    changed = db.execute(sql + where, key).rowcount
    assert changed == 1, "未找到唯一 pending 记录，未修改"
print("已更新；仅 retry 模式需要回 Gitea 重投")
PYTHON
```

不要删除整个数据库。记录不会自动过期；数据库和卷按普通持久数据备份，单实例部署。

## 本地测试

```sh
python3 -m unittest -v
```

依据：[Gitea webhook](https://docs.gitea.com/usage/repository/webhooks/)、[GitHub workflow dispatch](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event)。
