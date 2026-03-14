FROM python:3.14-slim

# WeasyPrint system deps
RUN apt-get update && apt-get install -y \
    git \
    libcairo2 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libgdk-pixbuf-xlib-2.0-0 \
    libffi-dev \
    shared-mime-info \
    curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install uv

WORKDIR /app

COPY . .

RUN uv sync --no-dev

EXPOSE 8899

HEALTHCHECK CMD curl -f http://localhost:8899/ || exit 1

CMD ["uv", "run", "hr-breaker", "serve"]
