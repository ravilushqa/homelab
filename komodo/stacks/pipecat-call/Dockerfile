FROM python:3.12-slim

WORKDIR /app

# Install uv for fast dependency management
RUN pip install --no-cache-dir uv

# Copy project files
COPY pyproject.toml .
COPY bot.py server.py ./

# Install dependencies only (no package build)
RUN uv pip install --system --no-cache \
    "pipecat-ai[google,silero]>=1.1.0" \
    "fastapi>=0.115.0" \
    "uvicorn[standard]>=0.34.0" \
    "aiohttp>=3.11.0" \
    "python-dotenv>=1.0.0" \
    "loguru>=0.7.0"

# Port
EXPOSE 7860

CMD ["python", "server.py"]
