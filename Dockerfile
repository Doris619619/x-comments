# 本镜像提供 Python、Playwright Chromium 和 Xvfb，仅运行本项目 POC。
FROM mcr.microsoft.com/playwright/python:v1.61.0-noble

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md alembic.ini ./
COPY app ./app
COPY alembic ./alembic
COPY scripts ./scripts
COPY docs ./docs
COPY THIRD_PARTY_NOTICES.md ./
COPY docker-entrypoint.sh ./

RUN python -m pip install .

EXPOSE 8000

CMD ["bash", "/app/docker-entrypoint.sh"]

