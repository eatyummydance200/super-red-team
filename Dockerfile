FROM python:3.13-slim
WORKDIR /app
COPY backend /app/backend
RUN pip install --no-cache-dir ./backend && useradd --uid 10001 --create-home app && mkdir /app/data && chown app:app /app/data
USER app
ENV DATA_DIR=/app/data REQUIRE_API_AUTH=true
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.main:app", "--app-dir", "/app/backend", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
