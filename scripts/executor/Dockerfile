# 固定分析镜像，只预装 DuckDB（A8 run_sql）；运行时不安装依赖。
ARG PYTHON_IMAGE=python:3.12.12-slim-bookworm
FROM ${PYTHON_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN python -m pip install --no-cache-dir duckdb==1.5.6
USER 65532:65532
WORKDIR /output
ENTRYPOINT ["python", "-I", "-B"]