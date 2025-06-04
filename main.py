import discord
from discord.ext import commands
from discord import app_commands
from flask import Flask, render_template, request, redirect, url_for, session, flash
from gtts import gTTS
import os
import threading
import logging
from dotenv import load_dotenv
load_dotenv()
import discord.opus
import spotipy
from spotipy.oauth2 import SpotifyOAuth 

# ตั้งค่า Spotify API Credentials
SPOTIPY_CLIENT_ID = os.getenv("SPOTIPY_CLIENT_ID")
SPOTIPY_CLIENT_SECRET = os.getenv("SPOTIPY_CLIENT_SECRET")
SPOTIPY_REDIRECT_URI = os.getenv("SPOTIPY_REDIRECT_URI")

# กำหนด scope ที่ต้องการ
# user-read-playback-state: อ่านสถานะการเล่นของผู้ใช้
# user-modify-playback-state: ควบคุมการเล่น (เล่น, หยุด, ข้าม)
# user-read-currently-playing: อ่านเพลงที่กำลังเล่นอยู่
# playlist-read-private: อ่านเพลย์ลิสต์ส่วนตัว
# playlist-read-collaborative: อ่านเพลย์ลิสต์ที่ทำงานร่วมกัน
# streaming: สำหรับบางกรณีที่ต้องการสตรีม แต่โดยทั่วไปบอท Discord จะไม่สตรีมเสียงจาก Spotify โดยตรง
SPOTIPY_SCOPES = "user-read-playback-state user-modify-playback-state user-read-currently-playing playlist-read-private playlist-read-collaborative"

# Dictionary สำหรับเก็บ Spotify instances ของแต่ละผู้ใช้ Discord
# Key: Discord User ID, Value: spotipy.Spotify object
spotify_users = {}

# ----------------------------------------------------

TOKEN = os.getenv("DISCORD_TOKEN")
YOUR_GUILD_ID = int(os.environ["GUILD_ID"])

# ตั้งค่า intents
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix="/", intents=intents)
tree = bot.tree
voice_client = None # อาจจะไม่ได้ใช้งาน voice_client สำหรับเล่นเพลงจาก Spotify โดยตรง
queue = [] # Queue จะเก็บ track ID หรือ URI ของ Spotify
volume = 0.5 # Volume อาจจะถูกควบคุมบน Spotify client แทน

# ตั้งค่า Flask
app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = os.getenv("FLASK_SECRET_KEY") # จำเป็นสำหรับ Flask Session

# ตั้งค่า log
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s:%(levelname)s:%(message)s',
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)

@bot.event
async def on_ready():
    if not discord.opus.is_loaded():
        try:
            discord.opus.load_opus('libopus.so')
            logging.info("Opus loaded successfully.")
        except Exception as e:
            logging.error(f"Failed to load opus: {e}. Please ensure libopus.so is in your PATH or specified correctly.")
            print(f"Failed to load opus: {e}. Please ensure libopus.so is in your PATH or specified correctly.")

    print(f"✅ Bot logged in as {bot.user}")
    logging.info(f"Bot logged in as {bot.user}")

    await tree.sync()
    logging.info("Global commands synced.")

    guild = discord.Object(id=YOUR_GUILD_ID)
    await tree.sync(guild=guild)
    logging.info(f"Commands synced to guild: {YOUR_GUILD_ID}")
    
    # --- โหลด Spotify Tokens ที่บันทึกไว้เมื่อบอทเริ่มทำงาน (ถ้ามี) ---
    # ใน Production ควรเก็บ Access/Refresh tokens ใน Database แทนไฟล์
    # ตัวอย่างนี้สาธิตการโหลดจากไฟล์ง่ายๆ
    try:
        with open("spotify_tokens.json", "r") as f:
            tokens_data = json.load(f)
        for user_id, token_info in tokens_data.items():
            auth_manager = SpotifyOAuth(
                client_id=SPOTIPY_CLIENT_ID,
                client_secret=SPOTIPY_CLIENT_SECRET,
                redirect_uri=SPOTIPY_REDIRECT_URI,
                scope=SPOTIPY_SCOPES,
            )
            # โหลด token_info เก่า
            auth_manager.token_info = token_info
            
            # สร้าง Spotify instance ใหม่
            sp_user = spotipy.Spotify(auth_manager=auth_manager)
            spotify_users[int(user_id)] = sp_user
            logging.info(f"Loaded Spotify token for user ID: {user_id}")
    except FileNotFoundError:
        logging.info("No saved Spotify tokens found.")
    except Exception as e:
        logging.error(f"Error loading Spotify tokens: {e}")

# --- Command สำหรับเชื่อมต่อ Spotify ---
@tree.command(name="link_spotify", description="เชื่อมต่อบัญชี Spotify ของคุณ")
async def link_spotify(interaction: discord.Interaction):
    auth_manager = SpotifyOAuth(
        client_id=SPOTIPY_CLIENT_ID,
        client_secret=SPOTIPY_CLIENT_SECRET,
        redirect_uri=SPOTIPY_REDIRECT_URI,
        scope=SPOTIPY_SCOPES,
        show_dialog=True # ให้ผู้ใช้เห็นหน้าอนุมัติทุกครั้ง (ดีสำหรับการทดสอบ)
    )
    auth_url = auth_manager.get_authorize_url()
    
    # เก็บ Discord User ID ไว้ใน Flask Session เพื่อใช้ตอน callback
    session['discord_user_id'] = interaction.user.id

    await interaction.response.send_message(f"กรุณาคลิกที่ลิงก์นี้เพื่อเชื่อมต่อ Spotify ของคุณ: {auth_url}\n(ลิงก์นี้ใช้ได้สำหรับคุณเท่านั้น)", ephemeral=True)
    logging.info(f"Sent Spotify auth link to user {interaction.user.id}")

# --- ตรวจสอบว่าผู้ใช้เชื่อมต่อ Spotify แล้วหรือยัง ---
def get_user_spotify_client(discord_user_id: int):
    sp_client = spotify_users.get(discord_user_id)
    if sp_client:
        # ตรวจสอบว่า token ยังใช้ได้หรือไม่ (spotipy จะจัดการ refresh ให้ถ้าตั้งค่าถูก)
        # ลองเรียก API เล็กๆ น้อยๆ เพื่อตรวจสอบ
        try:
            sp_client.current_user() 
            return sp_client
        except spotipy.exceptions.SpotifyException as e:
            logging.warning(f"Spotify token expired or invalid for user {discord_user_id}: {e}")
            del spotify_users[discord_user_id] # ลบ token ที่ไม่ถูกต้องออก
            return None
    return None

# --- คำสั่ง Join/Leave ยังเหมือนเดิม ---
@tree.command(name="join", description="เข้าห้อง Voice")
async def join(interaction: discord.Interaction):
    global voice_client
    if interaction.user.voice:
        channel = interaction.user.voice.channel
        # บอทจะยังคง Join ห้องเสียงได้ เพื่อใช้ TTS หรือฟังก์ชันอื่นๆ ที่ไม่ใช่การเล่นเพลง Spotify
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

# --- คำสั่ง Play ที่จะควบคุม Spotify Client ของผู้ใช้ ---
@tree.command(name="play", description="ค้นหาและเล่นเพลงจาก Spotify")
@app_commands.describe(query="ชื่อเพลง, ศิลปิน, หรือลิงก์ Spotify")
async def play(interaction: discord.Interaction, query: str):
    sp_user = get_user_spotify_client(interaction.user.id)
    if not sp_user:
        await interaction.response.send_message("❌ คุณยังไม่ได้เชื่อมต่อ Spotify โปรดใช้ `/link_spotify` ก่อน", ephemeral=True)
        return

    await interaction.response.defer() # Defer เพื่อให้มีเวลาประมวลผล

    try:
        track_uris = []
        is_playlist = False
        is_album = False

        if "spotify.com/track/" in query or "spotify.com/playlist/" in query or "spotify.com/album/" in query:
            # ตรวจสอบว่าเป็นลิงก์เพลง, เพลย์ลิสต์ หรืออัลบั้ม
            if "spotify.com/track/" in query:
                track = sp_user.track(query)
                track_uris.append(track['uri'])
                response_msg = f"🎶 กำลังเล่นเพลง: **{track['name']}** โดย **{track['artists'][0]['name']}**"
                logging.info(f"Playing Spotify track URI: {track['uri']}")
            elif "spotify.com/playlist/" in query:
                playlist_id = query.split('/')[-1].split('?')[0]
                results = sp_user.playlist_items(playlist_id)
                for item in results['items']:
                    if item['track']:
                        track_uris.append(item['track']['uri'])
                is_playlist = True
                response_msg = f"🎶 กำลังเล่นเพลงจาก Spotify Playlist: {results['items'][0]['track']['name']} และ {len(track_uris)-1} เพลงต่อมา"
                logging.info(f"Playing Spotify playlist with {len(track_uris)} tracks.")
            elif "spotify.com/album/" in query:
                album_id = query.split('/')[-1].split('?')[0]
                results = sp_user.album_tracks(album_id)
                for item in results['items']:
                    track_uris.append(item['uri'])
                is_album = True
                response_msg = f"🎶 กำลังเล่นเพลงจาก Spotify Album: {len(track_uris)} เพลง"
                logging.info(f"Playing Spotify album with {len(track_uris)} tracks.")
            else:
                await interaction.followup.send("❌ ไม่รองรับลิงก์ Spotify ประเภทนี้")
                return

        else: # ค้นหาด้วยชื่อเพลง
            results = sp_user.search(q=query, type='track', limit=1)
            if not results['tracks']['items']:
                await interaction.followup.send("❌ ไม่พบเพลงนี้บน Spotify")
                return
            track = results['tracks']['items'][0]
            track_uris.append(track['uri'])
            response_msg = f"🎶 กำลังเล่นเพลง: **{track['name']}** โดย **{track['artists'][0]['name']}**"
            logging.info(f"Playing Spotify search result: {track['uri']}")

        if not track_uris:
            await interaction.followup.send("❌ ไม่พบเพลงที่ต้องการเล่น")
            return

        # --- ส่วนสำคัญ: ควบคุมการเล่นบน Spotify Client ---
        # ค้นหา devices ที่เปิดอยู่
        devices = sp_user.devices()
        active_device_id = None
        for device in devices['devices']:
            if device['is_active']:
                active_device_id = device['id']
                break
        
        if not active_device_id:
            await interaction.followup.send("❌ ไม่พบ Spotify Client ที่กำลังทำงานอยู่ กรุณาเปิด Spotify App ของคุณ")
            return

        # เริ่มเล่นเพลง
        if is_playlist or is_album:
            sp_user.start_playback(device_id=active_device_id, uris=track_uris)
        else:
            sp_user.start_playback(device_id=active_device_id, uris=track_uris) # หรือ context_uri ถ้าเป็น album/playlist
        
        await interaction.followup.send(response_msg)

    except spotipy.exceptions.SpotifyException as e:
        if e.http_status == 401: # Unauthorized
            await interaction.followup.send("❌ Spotify Token หมดอายุหรือมีปัญหา โปรดใช้ `/link_spotify` อีกครั้ง")
            logging.error(f"Spotify token issue for user {interaction.user.id}: {e}")
            if interaction.user.id in spotify_users:
                del spotify_users[interaction.user.id] # ลบ token ที่ไม่ถูกต้องออก
        else:
            await interaction.followup.send(f"❌ เกิดข้อผิดพลาดในการเล่นเพลง Spotify: {e}")
            logging.error(f"Spotify API error for user {interaction.user.id}: {e}")
    except Exception as e:
        await interaction.followup.send(f"❌ เกิดข้อผิดพลาดที่ไม่คาดคิด: {e}")
        logging.error(f"Unexpected error in play command: {e}")

# --- คำสั่ง Pause ---
@tree.command(name="pause", description="หยุดเพลง Spotify ชั่วคราว")
async def pause_spotify(interaction: discord.Interaction):
    sp_user = get_user_spotify_client(interaction.user.id)
    if not sp_user:
        await interaction.response.send_message("❌ คุณยังไม่ได้เชื่อมต่อ Spotify", ephemeral=True)
        return
    
    try:
        sp_user.pause_playback()
        await interaction.response.send_message("⏸️ หยุดเพลง Spotify ชั่วคราวแล้ว")
    except spotipy.exceptions.SpotifyException as e:
        await interaction.response.send_message(f"❌ เกิดข้อผิดพลาดในการหยุดเพลง: {e}")
        logging.error(f"Spotify pause error for user {interaction.user.id}: {e}")

# --- คำสั่ง Resume ---
@tree.command(name="resume", description="เล่นเพลง Spotify ต่อ")
async def resume_spotify(interaction: discord.Interaction):
    sp_user = get_user_spotify_client(interaction.user.id)
    if not sp_user:
        await interaction.response.send_message("❌ คุณยังไม่ได้เชื่อมต่อ Spotify", ephemeral=True)
        return
    
    try:
        sp_user.start_playback() # resume คือ start_playback โดยไม่มี arguments
        await interaction.response.send_message("▶️ เล่นเพลง Spotify ต่อแล้ว")
    except spotipy.exceptions.SpotifyException as e:
        await interaction.response.send_message(f"❌ เกิดข้อผิดพลาดในการเล่นเพลงต่อ: {e}")
        logging.error(f"Spotify resume error for user {interaction.user.id}: {e}")

# --- คำสั่ง Skip ---
@tree.command(name="skip", description="ข้ามเพลง Spotify")
async def skip_spotify(interaction: discord.Interaction):
    sp_user = get_user_spotify_client(interaction.user.id)
    if not sp_user:
        await interaction.response.send_message("❌ คุณยังไม่ได้เชื่อมต่อ Spotify", ephemeral=True)
        return
    
    try:
        sp_user.next_track()
        await interaction.response.send_message("⏭️ ข้ามเพลง Spotify แล้ว")
    except spotipy.exceptions.SpotifyException as e:
        await interaction.response.send_message(f"❌ เกิดข้อผิดพลาดในการข้ามเพลง: {e}")
        logging.error(f"Spotify skip error for user {interaction.user.id}: {e}")

# --- คำสั่ง Volume (จะปรับบน Spotify Client ของผู้ใช้) ---
@tree.command(name="volume", description="ปรับระดับเสียง Spotify (0-100)")
@app_commands.describe(level="ระดับเสียง (0-100)")
async def set_spotify_volume(interaction: discord.Interaction, level: int):
    sp_user = get_user_spotify_client(interaction.user.id)
    if not sp_user:
        await interaction.response.send_message("❌ คุณยังไม่ได้เชื่อมต่อ Spotify", ephemeral=True)
        return
    
    if not (0 <= level <= 100):
        await interaction.response.send_message("❌ ระดับเสียงต้องอยู่ระหว่าง 0 ถึง 100")
        return

    try:
        # ค้นหา devices ที่เปิดอยู่
        devices = sp_user.devices()
        active_device_id = None
        for device in devices['devices']:
            if device['is_active']:
                active_device_id = device['id']
                break
        
        if not active_device_id:
            await interaction.response.send_message("❌ ไม่พบ Spotify Client ที่กำลังทำงานอยู่")
            return

        sp_user.volume(level, device_id=active_device_id)
        await interaction.response.send_message(f"🔊 ปรับระดับเสียง Spotify เป็น {level}% แล้ว")
    except spotipy.exceptions.SpotifyException as e:
        await interaction.response.send_message(f"❌ เกิดข้อผิดพลาดในการปรับเสียง: {e}")
        logging.error(f"Spotify volume error for user {interaction.user.id}: {e}")

# --- คำสั่ง Speak ยังคงอยู่ (ใช้ voice_client ที่ Join ห้องไว้) ---
@tree.command(name="speak", description="ให้บอทพูด")
@app_commands.describe(message="ข้อความ")
async def speak(interaction: discord.Interaction, message: str):
    if not voice_client:
        await interaction.response.send_message("❌ ยังไม่เข้าห้อง Voice")
        return
    try:
        tts = gTTS(message, lang='th')
        tts.save("tts.mp3")
        source = discord.FFmpegPCMAudio("tts.mp3", executable="ffmpeg")
        # หากบอทกำลังเล่น TTS อาจหยุดเพลง (ถ้ามี) แล้วกลับมาเล่นต่อ
        if voice_client.is_playing():
            voice_client.pause() # หยุดเพลงชั่วคราว
            voice_client.play(source, after=lambda e: bot.loop.create_task(voice_client.resume())) # เล่น TTS แล้วกลับไปเล่นเพลง
        else:
            voice_client.play(source)
        await interaction.response.send_message(f"🗣️ พูด: {message}")
        logging.info(f"TTS message: {message}")
    except Exception as e:
        await interaction.response.send_message(f"❌ เกิดข้อผิดพลาดในการพูด: {e}")
        logging.error(f"Failed to speak: {e}")

# --- คำสั่ง Wake ยังคงอยู่ ---
@tree.command(name="wake", description="ปลุกผู้ใช้ด้วย DM", guild=discord.Object(id=YOUR_GUILD_ID))
@app_commands.describe(user="เลือกผู้ใช้")
async def wake(interaction: discord.Interaction, user: discord.User):
    try:
        await user.send(f"⏰ คุณถูก {interaction.user} ปลุก! ตื่นนน!")
        await interaction.response.send_message(f"✅ ปลุก {user.name} แล้ว")
        logging.info(f"{interaction.user} woke up {user.name}.")
    except Exception as e:
        await interaction.response.send_message(f"❌ ส่ง DM ไม่ได้: {e}")
        logging.error(f"Failed to wake user: {e}")

# --- Flask Routes ---

# หน้าหลัก แสดงสถานะการเชื่อมต่อและ Queue (อาจต้องปรับ Queue ให้เหมาะกับ Spotify)
@app.route("/")
def index():
    # ตรวจสอบว่าผู้ใช้ Discord ล็อกอินแล้วหรือยัง (ผ่าน Flask Session)
    discord_user_id = session.get('discord_user_id', None)
    is_spotify_linked = False
    if discord_user_id:
        if get_user_spotify_client(discord_user_id):
            is_spotify_linked = True
    return render_template("index.html", 
                           queue=queue, 
                           is_spotify_linked=is_spotify_linked,
                           discord_user_id=discord_user_id)

# Route สำหรับการ Login Spotify
@app.route("/login/spotify/<int:discord_user_id>")
def login_spotify(discord_user_id: int):
    # เก็บ Discord User ID ใน Flask Session (เมื่อถูกเรียกจากเว็บ)
    session['discord_user_id'] = discord_user_id
    auth_manager = SpotifyOAuth(
        client_id=SPOTIPY_CLIENT_ID,
        client_secret=SPOTIPY_CLIENT_SECRET,
        redirect_uri=SPOTIPY_REDIRECT_URI,
        scope=SPOTIPY_SCOPES,
        show_dialog=True # ให้ผู้ใช้เห็นหน้าอนุมัติทุกครั้ง (ดีสำหรับการทดสอบ)
    )
    auth_url = auth_manager.get_authorize_url()
    return redirect(auth_url)

# Route Callback หลังจากผู้ใช้ยืนยันตัวตนกับ Spotify
@app.route("/callback/spotify")
def spotify_callback():
    discord_user_id = session.get('discord_user_id', None)
    if not discord_user_id:
        flash("เกิดข้อผิดพลาด: ไม่พบ Discord User ID ในเซสชัน")
        logging.error("Spotify callback: Discord User ID not found in session.")
        return redirect(url_for("index"))

    auth_manager = SpotifyOAuth(
        client_id=SPOTIPY_CLIENT_ID,
        client_secret=SPOTIPY_CLIENT_SECRET,
        redirect_uri=SPOTIPY_REDIRECT_URI,
        scope=SPOTIPY_SCOPES,
    )

    try:
        token_info = auth_manager.get_access_token(request.args['code'])
        sp_user = spotipy.Spotify(auth_manager=auth_manager)
        spotify_users[discord_user_id] = sp_user
        
        # --- บันทึก Token Info สำหรับการใช้งานในอนาคต ---
        # ใน Production ควรเก็บใน Database เช่น SQLite หรือ Redis
        # ตัวอย่างนี้บันทึกในไฟล์ (ไม่เหมาะสำหรับ Production)
        try:
            with open("spotify_tokens.json", "r") as f:
                tokens_data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            tokens_data = {}
        
        tokens_data[str(discord_user_id)] = token_info
        with open("spotify_tokens.json", "w") as f:
            json.dump(tokens_data, f)
        logging.info(f"Saved Spotify token for user {discord_user_id}")
        # -----------------------------------------------

        flash("เชื่อมต่อ Spotify สำเร็จ!")
        logging.info(f"User {discord_user_id} successfully linked Spotify.")
    except Exception as e:
        flash(f"เกิดข้อผิดพลาดในการเชื่อมต่อ Spotify: {e}")
        logging.error(f"Error during Spotify callback for user {discord_user_id}: {e}")
    
    return redirect(url_for("index"))

# --- Flask Routes สำหรับควบคุม Spotify (ต้องมีผู้ใช้ล็อกอินก่อน) ---
# คุณอาจต้องสร้าง route ที่ซับซ้อนขึ้นเพื่อส่ง query หรือ track ID จากหน้าเว็บ
# และเรียกใช้ฟังก์ชันควบคุม Spotify ที่สร้างไว้ใน bot (เช่น pause_spotify, resume_spotify)
# โดยต้องมั่นใจว่ามี Spotify client ของผู้ใช้ที่เรียกใช้งานอยู่
@app.route("/control_spotify/<action>")
def control_spotify(action: str):
    discord_user_id = session.get('discord_user_id', None)
    if not discord_user_id:
        flash("โปรดเข้าสู่ระบบ Discord บนหน้าเว็บก่อน")
        return redirect(url_for("index"))
    
    sp_user = get_user_spotify_client(discord_user_id)
    if not sp_user:
        flash("โปรดเชื่อมต่อบัญชี Spotify ของคุณก่อน")
        return redirect(url_for("index"))

    try:
        if action == "play":
            # ในเว็บ คุณอาจต้องมีช่องให้ใส่ query หรือเลือกเพลง
            # หรืออาจจะให้แสดงเพลย์ลิสต์/อัลบั้มของผู้ใช้แล้วเลือกเล่น
            flash("การเล่นเพลงจากเว็บยังต้องมีการปรับปรุงเพิ่มเติม")
            # bot.loop.create_task(play_spotify_from_web(discord_user_id, query)) # ต้องสร้างฟังก์ชันนี้
        elif action == "pause":
            sp_user.pause_playback()
            flash("หยุดเพลง Spotify แล้ว")
        elif action == "resume":
            sp_user.start_playback()
            flash("เล่นเพลง Spotify ต่อแล้ว")
        elif action == "skip":
            sp_user.next_track()
            flash("ข้ามเพลง Spotify แล้ว")
        elif action == "previous":
            sp_user.previous_track()
            flash("เล่นเพลงก่อนหน้าแล้ว")
        elif action == "devices":
            devices = sp_user.devices()
            flash(f"อุปกรณ์ที่ใช้งาน: {', '.join([d['name'] for d in devices['devices']])}")
        else:
            flash("คำสั่งไม่ถูกต้อง")
    except spotipy.exceptions.SpotifyException as e:
        if e.http_status == 401:
            flash("Spotify Token หมดอายุ โปรดเชื่อมต่อใหม่")
            if discord_user_id in spotify_users:
                del spotify_users[discord_user_id]
        else:
            flash(f"เกิดข้อผิดพลาด Spotify: {e}")
        logging.error(f"Spotify web control error for user {discord_user_id} ({action}): {e}")
    except Exception as e:
        flash(f"เกิดข้อผิดพลาด: {e}")
        logging.error(f"Web control error for user {discord_user_id} ({action}): {e}")
    
    return redirect(url_for("index"))

def run_web():
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True) # debug=True ช่วยในการพัฒนา

if __name__ == "__main__":
    # Import json here as it's only needed for token loading/saving
    import json 
    
    # รันบอทใน thread แยก
    bot_thread = threading.Thread(target=lambda: bot.run(TOKEN), daemon=True)
    bot_thread.start()
    
    # รัน Flask app ใน main thread
    run_web()