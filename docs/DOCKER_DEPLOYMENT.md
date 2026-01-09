# Docker 部署

本文档介绍如何为本项目构建 Docker 镜像，并通过容器运行 Web 控制枱或命令行模式。

## 前提条件

- 在主机上安装 [Docker Engine](https://docs.docker.com/get-docker/)（建议使用 24.x 版及以上）
- 可选：安装 [Docker Compose](https://docs.docker.com/compose/install/)（`docker compose` 已内置于新版 Docker Desktop）
- 项目根目录已经准备好 `.env`（参考项目根目录的 `README.md` 和 `.env.example`）

## 镜像构建

```bash
git clone https://github.com/yanowo/Backpack-MM-Simple.git
cd Backpack-MM-Simple
docker build -t backpack-mm .
```

镜像基于 `python:3.11-slim`，会在构建阶段安装系统依赖（`build-essential/libffi-dev/libsodium-dev`）并通过 `pip` 安装 `requirements.txt`。

## 使用 Docker 运行

### 1. Web 控制枱（默认 `run.py --web`）

```bash
docker run -d \
  --name backpack-web \
  --env-file .env \
  -p 5000:5000 \
  backpack-mm --web
```

访问 `http://localhost:5000` 即可打开控制枱。容器运行时会使用 `.env` 中定义的所有 API 凭证、交易参数和 Web 端口设置。

### 2. 命令行模式（交互 / 快速启动）

```bash
docker run --rm \
  --env-file .env \
  backpack-mm --cli
```

要执行快速启动（如某个策略），在 `docker run` 命令后追加标准 `run.py` 参数，例如：

```bash
docker run --rm --env-file .env backpack-mm --exchange backpack --symbol SOL_USDC --spread 0.01 --max-orders 3
```

### 3. 持久化数据

为了保留日志和订单数据，可在运行容器时挂载项目宿主目录相关文件：

```bash
docker run --rm \
  --env-file .env \
  -v "$(pwd)/market_maker.log:/app/market_maker.log" \
  -v "$(pwd)/orders.db:/app/orders.db" \
  backpack-mm --web
```

## Docker Compose 示例

本仓库提供 `docker-compose.yml`（参考版本）：

```yaml
version: "3.9"

services:
  backpack-bot:
    build:
      context: .
      dockerfile: Dockerfile
    env_file: .env
    volumes:
      - ./market_maker.log:/app/market_maker.log
      - ./orders.db:/app/orders.db
    ports:
      - "5000:5000"
    command: ["--web"]
```

使用 Compose 启动：

```bash
docker compose up -d
```

## 调试与重建

- 若依赖或策略代码更新后需刷新镜像，请运行 `docker compose build --no-cache` 或 `docker build --no-cache -t backpack-mm .`
- 查看容器日志：`docker compose logs -f` 或 `docker logs backpack-web`
- 停止并清理：`docker compose down`

## 安全与配置

- 所有 API 密钥 / JWT / ed25519 key 均通过 `.env` 传入，切勿直接写入镜像。建议在生产环境使用 Docker Secrets 或部署平台的密钥管理。
- 如果需要在容器内临时编辑配置，可使用 `docker exec -it <container> /bin/bash`。
