# ใช้ Python Image ที่มีขนาดเล็กจาก Debian/Ubuntu
FROM python:3.10-slim-buster

# อัปเดต package list และติดตั้งไลบรารีระบบที่จำเป็น:
# libopus0 สำหรับการรองรับ Opus ใน Discord voice
# ffmpeg สำหรับการประมวลผลเสียงและวิดีโอ (จำเป็นสำหรับ yt-dlp)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    libopus0 \
    ffmpeg && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# กำหนด Working Directory ใน Container
WORKDIR /app

# คัดลอกไฟล์ requirements.txt เข้าไปใน Working Directory
COPY requirements.txt .

# ติดตั้ง Python Dependencies ตาม requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# คัดลอกโค้ดแอปพลิเคชันที่เหลือทั้งหมดเข้าไป
COPY . .

# กำหนด Port ที่ Flask จะรัน (default คือ 5000)
EXPOSE 5000

# กำหนดคำสั่งที่จะรันแอปพลิเคชันเมื่อ Container เริ่มทำงาน
# (คล้ายกับ Start Command หรือ Procfile)
CMD ["python", "main.py"]