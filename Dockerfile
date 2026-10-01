FROM python:3.13-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY templates ./templates
COPY static ./static
COPY tests ./tests
RUN useradd --system --uid 1000 tally && mkdir -p /data && chown tally /data
USER tally
ENV STORAGE_DB=/data/storage.db STORAGE_UPLOADS=/data/uploads
VOLUME /data
EXPOSE 8000
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "app.main:app"]
