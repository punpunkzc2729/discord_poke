import discord
from discord.ext import commands
from discord import app_commands
from flask import Flask, render_template, request, redirect, url_for
from gtts import gTTS
import os
import yt_dlp
import threading
import logging
from dotenv import load_dotenv
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

YOUR_GUILD_ID = int(os.environ["GUILD_ID"])

# ตั้งค่า intents
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix="/", intents=intents)
tree = bot.tree
voice_client = None
queue = []
volume = 0.5

# ตั้งค่า Flask
app = Flask(__name__, static_folder="static", template_folder="templates")

# ตั้งค่า log
logging.basicConfig(filename='bot.log', level=logging.INFO, format='%(asctime)s:%(levelname)s:%(message)s')

@bot.event
async def on_ready():
    if not discord.opus.is_loaded():
        discord.opus.load_opus('libopus.so')
    print(f"✅ Bot logged in as {bot.user}")
    logging.info(f"Bot logged in as {bot.user}")

    # sync แบบ global (ใช้เวลานานขึ้น แต่อยู่ทุกเซิร์ฟเวอร์ที่เชิญไป)
    await tree.sync()
    logging.info("Global commands synced.")

    # sync แบบ guild เฉพาะเซิร์ฟเวอร์ที่ระบุ เพื่อทดสอบไวๆ
    guild = discord.Object(id=YOUR_GUILD_ID)
    await tree.sync(guild=guild)
    logging.info(f"Commands synced to guild: {YOUR_GUILD_ID}")
    

@tree.command(name="join", description="เข้าห้อง Voice")
async def join(interaction: discord.Interaction):
    global voice_client
    if interaction.user.voice:
        channel = interaction.user.voice.channel
        voice_client = await channel.connect()
        await interaction.response.send_message(f"✅ เข้าห้อง {channel.name} แล้ว")
        logging.info(f"Joined voice channel: {channel.name}")
    else:
        await interaction.response.send_message("❌ คุณยังไม่ได้อยู่ใน voice channel")

@tree.command(name="leave", description="ออกจากห้อง Voice")
async def leave(interaction: discord.Interaction):
    global voice_client, queue
    if voice_client:
        await voice_client.disconnect()
        voice_client = None
        queue.clear()
        await interaction.response.send_message("✅ ออกจากห้อง Voice แล้ว")
        logging.info("Left voice channel and cleared queue.")
    else:
        await interaction.response.send_message("❌ บอทยังไม่ได้ join ห้อง Voice")

@tree.command(name="play", description="เพิ่มเพลง YouTube")
@app_commands.describe(url="ลิงก์ YouTube")
async def play(interaction: discord.Interaction, url: str):
    global queue
    queue.append(url)
    await interaction.response.send_message(f"🎶 เพิ่มเพลงเข้า queue: {url}")
    logging.info(f"Added to queue: {url}")
    if voice_client and not voice_client.is_playing():
        await play_next(interaction.channel)

async def play_next(channel):
    global queue, voice_client, volume
    if not queue:
        await channel.send("🎵 Queue ว่างแล้ว")
        logging.info("Queue is empty.")
        return
    url = queue.pop(0)
    ydl_opts = {
        'format': 'bestaudio/best',
        'noplaylist': True,
        'cookiefile': 'cookies.txt'
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            audio_url = info['url']
            title = info.get('title', 'Unknown Title')
    except Exception as e:
        await channel.send(f"❌ ไม่สามารถเล่นเพลงได้: {e}")
        logging.error(f"Failed to extract audio: {e}")
        return

    source = discord.PCMVolumeTransformer(discord.FFmpegPCMAudio(audio_url, executable="ffmpeg"), volume=volume)
    voice_client.play(source, after=lambda e: bot.loop.create_task(play_next(channel)))
    await channel.send(f"🎶 Now Playing: {title}")
    logging.info(f"Now playing: {title}")

@tree.command(name="stop", description="หยุดเพลงและเคลียร์คิว")
async def stop(interaction: discord.Interaction):
    global queue
    queue.clear()
    if voice_client and voice_client.is_playing():
        voice_client.stop()
    await interaction.response.send_message("🛑 หยุดเพลงและเคลียร์คิวแล้ว")
    logging.info("Stopped music and cleared queue.")

@tree.command(name="skip", description="ข้ามเพลง")
async def skip(interaction: discord.Interaction):
    if voice_client and voice_client.is_playing():
        voice_client.stop()
        await interaction.response.send_message("⏭️ ข้ามเพลงแล้ว")
        logging.info("Skipped current track.")
    else:
        await interaction.response.send_message("❌ ไม่มีเพลงเล่นอยู่")

@tree.command(name="speak", description="ให้บอทพูด")
@app_commands.describe(message="ข้อความ")
async def speak(interaction: discord.Interaction, message: str):
    if not voice_client:
        await interaction.response.send_message("❌ ยังไม่เข้าห้อง Voice")
        return
    tts = gTTS(message, lang='th')
    tts.save("tts.mp3")
    source = discord.FFmpegPCMAudio("tts.mp3", executable="ffmpeg")
    voice_client.play(source)
    await interaction.response.send_message(f"🗣️ พูด: {message}")
    logging.info(f"TTS message: {message}")

@tree.command(name="wake", description="ปลุกผู้ใช้ด้วย DM", guild=discord.Object(id=YOUR_GUILD_ID))
@app_commands.describe(user="เลือกผู้ใช้")
async def wake(interaction: discord.Interaction, user: discord.User):
    try:
        await user.send(f"⏰ คุณถูก {interaction.user} ปลุก! ตื่นนน!")
        await interaction.response.send_message(f"✅ ปลุก {user.name} แล้ว")
        logging.info(f"{interaction.user} woke up {user.name}.")
    except Exception as e:
        await interaction.response.send_message("❌ ส่ง DM ไม่ได้")
        logging.error(f"Failed to wake user: {e}")


@app.route("/")
def index():
    return render_template("index.html", queue=queue)

@app.route("/add", methods=["POST"])
def add():
    url = request.form["url"]
    queue.append(url)
    logging.info(f"Added to queue from web: {url}")
    return redirect(url_for("index"))

@app.route("/play")
def play_web():
    if voice_client and not voice_client.is_playing():
        threading.Thread(target=lambda: bot.loop.create_task(play_next(bot.get_channel(voice_client.channel.id)))).start()
        logging.info("Triggered play via web.")
    return redirect("/")

@app.route("/pause")
def pause_web():
    if voice_client and voice_client.is_playing():
        voice_client.pause()
        logging.info("Paused via web.")
    return redirect("/")

@app.route("/resume")
def resume_web():
    if voice_client and voice_client.is_paused():
        voice_client.resume()
        logging.info("Resumed via web.")
    return redirect("/")

@app.route("/stop")
def stop_web():
    global queue
    queue.clear()
    if voice_client and voice_client.is_playing():
        voice_client.stop()
    logging.info("Stopped via web and cleared queue.")
    return redirect("/")

@app.route("/skip")
def skip_web():
    if voice_client and voice_client.is_playing():
        voice_client.stop()
        logging.info("Skipped via web.")
    return redirect("/")

@app.route("/volume_up")
def volume_up():
    global volume
    volume = min(volume + 0.1, 2.0)
    logging.info(f"Volume up: {volume}")
    return redirect("/")

@app.route("/volume_down")
def volume_down():
    global volume
    volume = max(volume - 0.1, 0.1)
    logging.info(f"Volume down: {volume}")
    return redirect("/")

@app.route("/upload_cookies", methods=["GET", "POST"])
def upload_cookies():
    if request.method == "POST":
        if 'file' not in request.files:
            return '❌ ไม่พบไฟล์ที่อัปโหลด'
        file = request.files['file']
        if file.filename == '':
            return '❌ ยังไม่ได้เลือกไฟล์'
        if file and file.filename == 'cookies.txt':
            file.save(os.path.join(app.root_path, 'cookies.txt'))
            logging.info("✅ อัปโหลด cookies.txt ใหม่เรียบร้อยแล้ว")
            return redirect(url_for("index"))
        else:
            return '❌ อัปโหลดได้เฉพาะ cookies.txt เท่านั้น'

    return render_template("upload_cookies.html")



def run_web():
    app.run(host="0.0.0.0", port=5000)

if __name__ == "__main__":
    threading.Thread(target=lambda: bot.run(TOKEN)).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

if not discord.opus.is_loaded():
    discord.opus.load_opus('libopus.so')    

bot.run(TOKEN)
