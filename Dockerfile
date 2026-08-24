FROM swr.cn-north-4.myhuaweicloud.com/ddn-k8s/docker.io/python:3.11-slim 

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app \
    TZ=Asia/Shanghai

# Node.js is required by the Lieju WAF and CNBlogs CAPTCHA helper scripts.
ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get \
        -o Acquire::Retries=3 \
        -o Acquire::http::Timeout=30 \
        -o Acquire::https::Timeout=30 \
        update \
    && apt-get \
        -o Acquire::Retries=3 \
        -o Acquire::http::Timeout=30 \
        -o Acquire::https::Timeout=30 \
        install --no-install-recommends -y nodejs tzdata \
    && ln -snf "/usr/share/zoneinfo/${TZ}" /etc/localtime \
    && echo "${TZ}" > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./

RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.doubanio.com/simple/ --trusted-host pypi.doubanio.com

COPY app ./app
COPY scripts ./scripts
COPY assets ./assets
COPY artifacts ./artifacts
COPY sql ./sql

RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
