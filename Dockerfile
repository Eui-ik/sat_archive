ARG BASE_IMAGE=python:3.13-slim
FROM ${BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY sentinel_viewer/ /app/sentinel_viewer/

EXPOSE 8765

ENTRYPOINT []
CMD ["python", "sentinel_viewer/app.py", "--data-dir", "/data", "--host", "0.0.0.0", "--port", "8765"]
