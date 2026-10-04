# reseed-orchestrator 镜像
# 构建上下文 = 仓库根目录 reseed-toolkit/ ；compose 里指定 dockerfile: data/orchestrator/Dockerfile
FROM python:3.12-slim

LABEL org.opencontainers.image.title="reseed-orchestrator" \
      org.opencontainers.image.description="大包拆包·单种保种编排器"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    RESEED_CONFIG=/config/config.yml

WORKDIR /app

# 先装依赖（利用层缓存）
COPY data/orchestrator/requirements.txt /app/orchestrator/requirements.txt
RUN pip install --no-cache-dir -r /app/orchestrator/requirements.txt

# 再拷代码
COPY data/orchestrator/ /app/orchestrator/

# 默认只做只读预检；实际跑用 `command: ["run", ...]` 或手动 exec
ENTRYPOINT ["python", "-m", "orchestrator.main"]
CMD ["preflight"]
