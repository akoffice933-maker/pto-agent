# MCP-сервер ИИ-агента ПТО в контейнере: транспорт http (для сети/облака),
# рабочая папка проекта — /app. Модель в образ не входит: инференс — отдельно.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MPLBACKEND=Agg \
    PTO_MCP_BASE_DIR=/app \
    PTO_MCP_TRANSPORT=http \
    PTO_MCP_HOST=0.0.0.0 \
    PTO_MCP_PORT=8765

WORKDIR /app

# слой зависимостей отдельно — чтобы правки кода не пересобирали всё
COPY requirements-pto.txt ./
RUN pip install --no-cache-dir -r requirements-pto.txt docker==7.* \
    && pip uninstall -y pytest

COPY . .

EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=3).status==200 else 1)"

# Токен доступа обязателен: контейнер слушает 0.0.0.0, а без токена сервер откажется
# стартовать (см. _resolve_http_security). Задайте PTO_MCP_TOKEN при запуске.
CMD ["python", "-m", "pto_agent.mcp_server", "--transport", "http", "--host", "0.0.0.0", "--port", "8765"]
