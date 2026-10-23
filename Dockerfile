FROM python:3.12-slim
WORKDIR /app
COPY scripts/bootstrap_pip.py scripts/verify_ca.py ./scripts/
RUN apt-get update && apt-get install --yes --no-install-recommends ca-certificates \
    && update-ca-certificates && rm -rf /var/lib/apt/lists/* \
    && python scripts/bootstrap_pip.py
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir . && python scripts/verify_ca.py \
    && useradd --uid 10001 --create-home watcher \
    && mkdir /app/data && chown watcher:watcher /app/data
USER watcher
ENTRYPOINT ["ticket-watcher"]
CMD ["--config", "/app/config.yaml", "run"]
