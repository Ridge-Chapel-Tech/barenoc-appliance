FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy worker code
COPY main.py .
COPY juniper.py .
COPY llm_client.py .
COPY judge.py .
COPY executor.py .
COPY policy.py .
COPY restrictions.py .
COPY test_judge.py .
COPY test_juniper.py .
COPY test_integration.py .

# Copy shared modules (copied alongside worker files on deploy)
COPY sanitizer.py .
COPY action_validator.py .
COPY audit.py .
COPY audit_catalog.py .
COPY worknotes.py .
COPY queue_status.py .
COPY tone_pool.py .
COPY database.py .
COPY models.py .
COPY schemas.py .
COPY llm_providers.py .
COPY emailer.py .

CMD ["python", "-B", "main.py"]
