FROM python:3.11-slim AS builder
WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.11-slim AS runtime
RUN groupadd --gid 1000 appuser && useradd --uid 1000 --gid appuser --shell /bin/bash --create-home appuser
COPY --from=builder /install /usr/local
WORKDIR /app
COPY --chown=appuser:appuser . .
RUN find /app/scripts -name '*.sh' -exec sed -i 's/\r$//' {} \; \
    && chmod +x /app/scripts/*.sh \
    && mkdir -p /app/logs && chown appuser:appuser /app/logs
USER appuser
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5000/')" || exit 1
EXPOSE 5000
ENTRYPOINT ["/app/scripts/entrypoint.sh"]
CMD ["waitress-serve", "--host=0.0.0.0", "--port=5000", "--asyncore-use-poll", "app:app"]
