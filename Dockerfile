# Use a Python base image
FROM python:3.10-slim-buster

# Install system dependencies for Discord.py (voice) and yt-dlp (media processing)
# libopus0: Opus audio codec library for Discord voice
# ffmpeg: Tool for processing multimedia multimedia files, used by yt-dlp
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    libopus0 \
    ffmpeg && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Set the working directory inside the container
WORKDIR /app

# Copy the Python dependencies file and install them
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code
COPY main.py .
COPY templates/ templates/
COPY static/ static/

# Expose the port on which the Flask application will run
EXPOSE 5000

# Define the command to run the application when the container starts
CMD ["python", "main.py"]
