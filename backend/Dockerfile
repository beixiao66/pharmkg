# pharmkg 后端镜像
#
# ## 一个必须说清的坑：这个镜像需要**整个仓库**，不是只有 backend/
#
# 后端的 `/ask` 通过 `app/l5_service.py` 复用 `scripts/` 里的 L5 流水线
# （意图 / 实体链接 / 改写 / text2cypher / 答案），并且要读
# `data/eval/l5_fewshot.csv`；`/quality/graph` 要读
# `data/quality/graph_quality.json`。这些都在 `backend/` 之外。
#
# 所以构建上下文是**仓库根目录**（compose 里 `context: .`、`dockerfile: backend/Dockerfile`），
# 不能在 `backend/` 里 `.` 构建——那样镜像里没有 scripts/，`/ask` 会在运行时 500，
# 而**构建阶段不会报错**。这类"镜像能建、接口一用就挂"的问题最难在答辩现场排查。
#
# ## 为什么不在镜像里装 torch 之类
#
# 大模型走云接口（决策 #22），本地不需要 GPU、也不需要推理框架。
# 镜像只装 requirements.txt 里那十几个包。

FROM python:3.13-slim

# 不生成 pyc、日志不缓冲（容器里 log 要能立刻看到）
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ⚠️ **刻意不装 curl。** 第一版为健康检查装了 curl（`apt-get install curl`），
# 结果在代理环境下 `apt-get` 直连 deb.debian.org 拿到 502、速度 23 kB/s，构建失败。
# 健康检查用 `python -c "urllib.request..."` 就行——**基础镜像自带 python**，
# 少一个 apt 步骤就少一个网络依赖，镜像也更小。
# 教训：**能不加系统包就不加**，尤其在"用户机器网络环境未知"的场景下。

WORKDIR /app

# 先只拷依赖清单，让依赖层可缓存（改代码不必重装依赖）
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install -r /app/backend/requirements.txt

# 再拷整个仓库（.dockerignore 已排除 node_modules/.venv/.docker-data 等）
COPY . /app

WORKDIR /app/backend

# 非 root 运行
RUN useradd -m -u 10001 pharmkg && chown -R pharmkg:pharmkg /app
USER pharmkg

EXPOSE 8000

# ⚠️ 不用 --reload：容器里 reload 会额外起进程、且吃内存
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
