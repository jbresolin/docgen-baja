# BAJA DocGen Worker - Docker Container
# Generates personalized, watermarked, and signed PDFs

FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies for PDF processing
RUN apt-get update && apt-get install -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first (for better layer caching)
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY docgen_worker.py .

# Google credentials are NOT baked into the image. Provide them at runtime via
# the GOOGLE_SERVICE_ACCOUNT_JSON env var or a read-only mount at
# /app/service-account.json (see docker-compose.yml).

# Create directories for temp files and data persistence
RUN mkdir -p /app/tmp_jobs /app/data

# The certificate and status files will be stored in /app/data
# Mount this as a volume for persistence

# Set environment variables
ENV PYTHONUNBUFFERED=1

# Run the worker
CMD ["python", "docgen_worker.py"]
