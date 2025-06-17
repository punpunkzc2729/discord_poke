# --- Stage 1: Build React Frontend ---
FROM node:18-alpine AS frontend_builder

WORKDIR /app/frontend

COPY frontend/package.json frontend/package-lock.json ./
RUN npm install

COPY frontend/ ./
RUN npm run build


# --- Stage 2: Build Python Backend and Serve ---
FROM python:3.10-slim-buster AS backend_runtime

# ติดตั้ง System Dependencies สำหรับ Discord.py และ yt-dlp
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    libopus0 \
    ffmpeg && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# กำหนด Working Directory สำหรับ Python Backend
WORKDIR /app

# คัดลอก React build output จาก Stage 1
COPY --from=frontend_builder /app/frontend/dist ./frontend/dist

# คัดลอก Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# คัดลอก Python backend code (main.py และอื่นๆ)
COPY main.py .
COPY templates/ templates/
# หากมีไฟล์ .env สำหรับ development จะไม่รวมไว้ใน Docker image ที่ Production
# ENV variables ควรถูกตั้งค่าบน Railway/AWS โดยตรง

# กำหนด Port ที่ Flask จะรัน (5000)
EXPOSE 5000

# กำหนดคำสั่งที่จะรันแอปพลิเคชันเมื่อ Container เริ่มทำงาน
CMD ["python", "main.py"]
