FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install ".[postgres]" \
    && useradd --create-home --uid 10001 stockhold

USER stockhold

EXPOSE 8000

CMD ["stockhold", "serve", "--host", "0.0.0.0", "--port", "8000"]
