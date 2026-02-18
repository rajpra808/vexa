FROM python:3.11-slim

WORKDIR /app

COPY services/deepgram-transcriber/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/deepgram-transcriber/server.py .

EXPOSE 9090

CMD ["python", "-u", "server.py"]
