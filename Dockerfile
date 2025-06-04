FROM python:3.12-slim

# Install system dependencies for FFmpeg and Opus
RUN apt-get update && apt-get install -y \
    ffmpeg \
    libopus0 \
    --no-install-recommends && \
    rm -rf /var/lib/apt/lists/*

# Add common lib paths to LD_LIBRARY_PATH
ENV LD_LIBRARY_PATH=/usr/local/lib:/usr/lib:$LD_LIBRARY_PATH

# Set working directory
WORKDIR /app

# Copy requirements file and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY . .

# Expose the port (only for the web service)
EXPOSE 5000

# Command to run (this will be overridden by Procfile)
CMD ["python", "main.py"]