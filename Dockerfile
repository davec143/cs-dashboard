FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --create-home --uid 10001 app
COPY qa ./qa
COPY web ./web
COPY tests ./tests
COPY config.json evaluator.md ./
USER app
CMD ["sh", "-c", "exec gunicorn 'qa.web:create_app()' --bind 0.0.0.0:${PORT:-8080} --workers 2 --threads 2 --timeout 60 --access-logfile /dev/null"]
