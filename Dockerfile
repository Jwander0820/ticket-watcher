FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd --uid 10001 --create-home watcher \
    && mkdir /app/data && chown watcher:watcher /app/data
USER watcher
ENTRYPOINT ["ticket-watcher"]
CMD ["--config", "/app/config.yaml", "run"]
