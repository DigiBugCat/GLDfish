# Use Python 3.11 slim image
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install uv
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"

# Copy project files
COPY pyproject.toml .
COPY uv.lock .
COPY src/ ./src/

# Install Python dependencies using uv
RUN uv sync --frozen

# Set environment to production
ENV PYTHONUNBUFFERED=1

# Run the bot
CMD ["uv", "run", "python", "-m", "src.bot"]
