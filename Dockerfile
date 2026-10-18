FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONUTF8=1 HOST=0.0.0.0 PORT=3000
WORKDIR /app
COPY requirements-lock.txt /app/requirements-lock.txt
RUN pip install --no-cache-dir -r requirements-lock.txt
COPY pyproject.toml /app/
COPY gateway /app/gateway
COPY web /app/web
RUN pip install --no-cache-dir --no-deps . && useradd --create-home --uid 10001 gateway
USER gateway
EXPOSE 3000
HEALTHCHECK --interval=10s --timeout=5s --start-period=10s --retries=6 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:3000/api/health', timeout=4)"
CMD ["python", "-m", "gateway.main"]

FROM runtime AS tests
USER root
COPY tests /app/tests
USER gateway
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]
