FROM python:3.12-slim

WORKDIR /app

# Install uv for fast dependency management
RUN pip install --no-cache-dir uv

# Copy project files
COPY pyproject.toml .
COPY bot.py server.py ./

# Install dependencies
RUN uv pip install --system --no-cache .

# Port
EXPOSE 7860

CMD ["python", "server.py"]
