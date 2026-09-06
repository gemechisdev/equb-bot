FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=8080
EXPOSE 8080

# Hits our own /health endpoint - works for any platform that runs a
# standard `docker inspect` style healthcheck.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT','8080') + '/health')" || exit 1

# RUN_MODE defaults to webhook here since PORT is set above; override with
# `-e RUN_MODE=polling` at `docker run` time to run as a background worker
# instead (see README for both options).
CMD ["python", "main.py"]
