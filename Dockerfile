FROM python:3.13-slim

WORKDIR /app

COPY pyproject.toml .
COPY server.py .
COPY utils/ utils/

RUN pip install --no-cache-dir .

EXPOSE 8000

CMD ["python", "server.py"]
