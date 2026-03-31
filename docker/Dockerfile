FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN mkdir -p /app/app/services/ev_forecast/artifacts

ENV FORECASTERS_ARTIFACTS_DIR=/app/app/services/ev_forecast/artifacts

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
