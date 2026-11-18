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
  ghcr.io/<owner>/<repo>:latest
```

Gitea 28.4.0 仓库设置 → Webhooks → Gitea：

- 地址：`https://你的中转域名/webhook`（由现有反向代理转到本服务 8080）。
- POST、`application/json`。
- Secret：与 `WEBHOOK_SECRET` 相同；不需要配置 Authorization。
- 自定义事件：只勾选 Workflow Job。
- Gitea 构建 job 使用 `runs-on: github-hosted`（或修改 `RUNNER_LABEL`）。

同机容器也可通过私有 Docker 网络互通，无需公开端口；此时 webhook URL 使用服务名而非 `127.0.0.1`。若 Gitea 阻止访问私网 webhook，按其配置仅允许具体中转主机，不要开放任意目的地。

## 行为与限制

- HMAC-SHA256 校验原始请求体；只转发固定 `{"ref":"main"}`，不转发源码、仓库名或 Gitea token。
- `/health` 返回 200；中转成功返回 202，忽略和重复事件返回 200，签名错误返回 401，上游失败返回 502。
- 内存保存最近 4096 个成功任务标识用于去重，重启后清空；单实例使用。不自动重试，失败可在 Gitea 投递历史中检查后重投。网络超时可能发生在 GitHub 已接受请求之后，重投前先检查 GitHub，避免重复启动。
- 不记录请求体、token 或上游响应。只接受你自己的可信仓库，服务放在反向代理后。它不是持久队列、扩缩容控制器或严格的 exactly-once 调度器。
- Runner 根据作用域和标签领取任务，不保证某次 webhook 创建的 Runner 恰好领取该 job。目标 runner workflow 应有空闲超时，处理完即退出。
- 这不会改变 GitHub Actions 的条款或配额；不要把它作为与目标 GitHub 项目无关的通用算力池。

## 本地测试

```sh
python3 -m unittest -v
```

依据：[Gitea webhook](https://docs.gitea.com/usage/repository/webhooks/)、[GitHub workflow dispatch](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event)。
