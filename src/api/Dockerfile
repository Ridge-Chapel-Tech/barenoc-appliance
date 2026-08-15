FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN apt update -qq && apt install -y -qq iputils-ping openssh-client cifs-utils nfs-common && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /opt/barenoc/volumes/db /opt/barenoc/volumes/logs/api

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "info"]
