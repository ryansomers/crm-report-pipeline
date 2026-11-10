# Image for both the pipeline CLI and the mock CRM (same package).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY mock_crm ./mock_crm
COPY pipeline ./pipeline
COPY report ./report
RUN pip install . \
    && useradd --create-home app \
    && mkdir -p /app/reports && chown app /app/reports
USER app

ENTRYPOINT ["crm-pipeline"]
CMD ["--help"]
