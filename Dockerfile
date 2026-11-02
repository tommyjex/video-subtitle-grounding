FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home app
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir ".[server]"
USER app
EXPOSE 8010
CMD ["uvicorn", "video_subtitle_grounding.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8010", "--no-access-log"]
