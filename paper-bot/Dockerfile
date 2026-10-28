# For hosts that run containers (Fly.io, Railway, Koyeb, a VPS...). Render does not need it.
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
ENV DB_PATH=/data/paperbot.db PORT=8000
RUN useradd -m bot && mkdir -p /data && chown bot /data
USER bot
EXPOSE 8000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
