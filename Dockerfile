FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY analysis/ analysis/
COPY app/ app/
ENV DATA_DIR=/data PYTHONUNBUFFERED=1
# Railway injects PORT; default 8000 for local docker
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
