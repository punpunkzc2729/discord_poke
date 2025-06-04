# ใช้ Python 3.12 slim image
FROM python:3.12-slim

# ลง ffmpeg กับ libopus0 สำหรับ Discord voice
RUN apt-get update && \
    apt-get install -y ffmpeg libopus0 && \
    rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# copy requirements.txt และติดตั้ง dependency ก่อน
COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

# copy ไฟล์ทั้งหมดไปใน container
COPY . .

# expose port 5000 (flask)
EXPOSE 5000

# รันโปรเจกต์
CMD ["python", "main.py"]
