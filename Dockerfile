FROM python:3.12-slim

# Install uv
RUN pip install uv

# Set working directory
WORKDIR /app

# Copy dependency files
COPY pyproject.toml uv.lock ./

# Install dependencies using uv
RUN uv sync --frozen

# Copy the rest of the application
COPY . .

# Set environment variables for uv to run properly
ENV PATH="/app/.venv/bin:$PATH"

# Set PYTHONPATH
ENV PYTHONPATH=/app

# Run the bot
CMD ["python", "-m", "src.main"]
