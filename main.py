import discord
from discord.ext import commands
from discord import app_commands
from flask import Flask, render_template, request, redirect, url_for, session, flash
from gtts import gTTS
import os
import threading
import logging
import json
from dotenv import load_dotenv
import spotipy
from spotipy.oauth2 import SpotifyOAuth

# --- โหลดตัวแปรสภาพแวดล้อมจากไฟล์ .env ---
load_dotenv()

if os.getenv('RUN_GUNICORN_WEB', 'false').lower() == 'true':
    # ถ้าตัวแปรสภาพแวดล้อมนี้ถูกตั้งค่าเป็น 'true' แสดงว่าเรากำลังรัน gunicorn
    # เพื่อป้องกัน bot.run ถูกเรียกซ้ำโดย gunicorn worker
    os.environ['FLASK_RUNNING_VIA_GUNICORN'] = 'true'


# --- ตั้งค่า Spotify API Credentials ---
SPOTIPY_CLIENT_ID = os.getenv("SPOTIPY_CLIENT_ID")
SPOTIPY_CLIENT_SECRET = os.getenv("SPOTIPY_CLIENT_SECRET")
SPOTIPY_REDIRECT_URI = os.getenv("SPOTIPY_REDIRECT_URI")
SPOTIPY_SCOPES = "user-read-playback-state user-modify-playback-state user-read-currently-playing playlist-read-private playlist-read-collaborative user-library-read"

# --- ตั้งค่า Discord Bot Credentials ---
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
YOUR_GUILD_ID = int(os.environ["GUILD_ID"]) # สำหรับซิงค์คำสั่ง slash command เฉพาะ Guild
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
DISCORD_REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI")
DISCORD_OAUTH_SCOPES = "identify guilds" # identify: เพื่อรับ user ID, guilds: เพื่อดู server ที่ user อยู่ (optional)

# --- Global Dictionaries / Variables ---
# Dictionary สำหรับเก็บ Spotify instances ของแต่ละผู้ใช้ Discord
# Key: Discord User ID, Value: spotipy.Spotify object
spotify_users = {}

# Dictionary สำหรับเก็บข้อมูล Discord user ที่ล็อกอินผ่านเว็บ
# Key: Session ID, Value: Discord User ID
web_logged_in_users = {}

voice_client = None
# Queue จะยังคงมีไว้สำหรับ TTS
queue = []

# --- ตั้งค่า Logger ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s:%(levelname)s:%(message)s',
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)

# --- ตั้งค่า Discord Bot ---
intents = discord.Intents.default()
intents.message_content = True
intents.members = True # จำเป็นสำหรับ Discord OAuth2 เพื่อดึงข้อมูลสมาชิก
intents.voice_states = True # จำเป็นสำหรับ Voice Channel Control

bot = commands.Bot(command_prefix="!", intents=intents) # เปลี่ยน prefix เป็น ! เพื่อหลีกเลี่ยง conflict กับ slash commands
tree = bot.tree # สำหรับ slash commands

# --- ตั้งค่า Flask App ---
app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = os.getenv("FLASK_SECRET_KEY")

# --- Discord Bot Events ---
@bot.event
async def on_ready():
    # โหลด Opus (จำเป็นสำหรับการเล่นเสียงใน Discord)
    if not discord.opus.is_loaded():
        try:
            # ตรวจสอบว่า libopus.so อยู่ใน PATH หรือระบุ path ที่ถูกต้อง
            discord.opus.load_opus('libopus.so') # หรือ discord.opus.load_opus('/path/to/libopus.so')
            logging.info("Opus loaded successfully.")
        except Exception as e:
            logging.error(f"Failed to load opus: {e}. Please ensure libopus.so is in your PATH or specified correctly.")
            print(f"Failed to load opus: {e}. Please ensure libopus.so is in your PATH or specified correctly.")

    print(f"✅ Bot logged in as {bot.user}")
    logging.info(f"Bot logged in as {bot.user}")

    # ซิงค์ Global commands (ใช้เวลา)
    await tree.sync()
    logging.info("Global commands synced.")

    # ซิงค์ commands เฉพาะ Guild (รวดเร็วกว่าสำหรับการทดสอบ)
    guild_obj = discord.Object(id=YOUR_GUILD_ID)
    await tree.sync(guild=guild_obj)
    logging.info(f"Commands synced to guild: {YOUR_GUILD_ID}")

    # --- โหลด Spotify Tokens ที่บันทึกไว้เมื่อบอทเริ่มทำงาน (ถ้ามี) ---
    # ใน Production ควรเก็บ Access/Refresh tokens ใน Database แทนไฟล์
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
            auth_manager.token_info = token_info # โหลด token_info เก่า
            sp_user = spotipy.Spotify(auth_manager=auth_manager)
            
            # ลอง refresh token เพื่อตรวจสอบความถูกต้อง (หาก expired, spotipy จะจัดการให้)
            try:
                sp_user.current_user() 
                spotify_users[int(user_id)] = sp_user
                logging.info(f"Loaded and verified Spotify token for user ID: {user_id}")
            except spotipy.exceptions.SpotifyException as e:
                logging.warning(f"Spotify token for user {user_id} expired or invalid on startup: {e}")
                # อาจจะลบ token นี้ออกจากไฟล์ถ้าต้องการ
    except FileNotFoundError:
        logging.info("No saved Spotify tokens found.")
    except json.JSONDecodeError:
        logging.error("Error decoding spotify_tokens.json. File might be corrupted.")
    except Exception as e:
        logging.error(f"Error loading Spotify tokens: {e}")


# --- Helper Function: Get Spotify Client for User ---
def get_user_spotify_client(discord_user_id: int):
    sp_client = spotify_users.get(discord_user_id)
    if sp_client:
        try:
            # เรียก API เล็กๆ เพื่อตรวจสอบว่า token ยังใช้ได้หรือไม่
            # spotipy จะพยายาม refresh token ให้เองถ้าใกล้หมดอายุ
            sp_client.current_user() 
            return sp_client
        except spotipy.exceptions.SpotifyException as e:
            logging.warning(f"Spotify token expired or invalid for user {discord_user_id}: {e}")
            if discord_user_id in spotify_users:
                del spotify_users[discord_user_id] # ลบ token ที่ไม่ถูกต้องออก
            return None
    return None

# --- Helper Function: Save Spotify Tokens ---
def save_spotify_tokens():
    try:
        tokens_data = {}
        for user_id, sp_client in spotify_users.items():
            # ดึง token_info ล่าสุดจาก auth_manager
            tokens_data[str(user_id)] = sp_client.auth_manager.get_cached_token()
        
        with open("spotify_tokens.json", "w") as f:
            json.dump(tokens_data, f, indent=4)
        logging.info("Saved Spotify tokens to file.")
    except Exception as e:
        logging.error(f"Error saving Spotify tokens: {e}")


# --- Discord Slash Commands ---

@tree.command(name="link_spotify", description="เชื่อมต่อบัญชี Spotify ของคุณ")
async def link_spotify(interaction: discord.Interaction):
    # ผู้ใช้ต้องล็อกอิน Discord บนหน้าเว็บก่อน เพื่อให้เรารู้ว่าใครกำลังเชื่อมต่อ
    if interaction.user.id not in web_logged_in_users.values():
        await interaction.response.send_message(
            "❌ กรุณาเข้าสู่ระบบ Discord บนหน้าเว็บก่อน เพื่อให้บอทรู้ว่าคุณคือใคร:\n"
            f"คลิกที่ลิงก์นี้: **{url_for('login_discord', _external=True)}**", 
            ephemeral=True
        )
        return

    # หากผู้ใช้เข้าสู่ระบบบนเว็บแล้ว บอทจะให้ลิงก์ Spotify login
    auth_manager = SpotifyOAuth(
        client_id=SPOTIPY_CLIENT_ID,
        client_secret=SPOTIPY_CLIENT_SECRET,
        redirect_uri=SPOTIPY_REDIRECT_URI,
        scope=SPOTIPY_SCOPES,
        show_dialog=True
    )
    auth_url = auth_manager.get_authorize_url()
    
    # เพื่อให้ Flask callback รู้ว่า user Discord คนไหนที่กำลังเชื่อมต่อ Spotify
    # เราจะให้ user เข้าถึงลิงก์ที่มี user ID ของเขา
    # ใน Production ควรมีการสร้าง temporary token หรือ UUID เพื่อป้องกันการสวมรอย
    await interaction.response.send_message(
        f"🔗 กรุณาคลิกที่ลิงก์นี้เพื่อเชื่อมต่อ Spotify ของคุณ: \n"
        f"**{auth_url}**\n"
        f"(ลิงก์นี้ใช้ได้สำหรับ Discord User ID: `{interaction.user.id}`)", 
        ephemeral=True
    )
    logging.info(f"Sent Spotify auth link to user {interaction.user.id}")


@tree.command(name="join", description="ให้บอทเข้าห้อง Voice Channel ของคุณ")
async def join(interaction: discord.Interaction):
    global voice_client
    if interaction.user.voice:
        channel = interaction.user.voice.channel
        if voice_client and voice_client.is_connected():
            if voice_client.channel.id == channel.id:
                await interaction.response.send_message(f"✅ บอทอยู่ในห้อง {channel.name} แล้ว")
            else:
                await voice_client.move_to(channel)
                await interaction.response.send_message(f"✅ ย้ายบอทไปห้อง {channel.name} แล้ว")
                logging.info(f"Moved voice client to: {channel.name}")
        else:
            voice_client = await channel.connect()
            await interaction.response.send_message(f"✅ เข้าห้อง {channel.name} แล้ว")
            logging.info(f"Joined voice channel: {channel.name}")
    else:
        await interaction.response.send_message("❌ คุณยังไม่ได้อยู่ใน voice channel")

@tree.command(name="leave", description="ให้บอทออกจากห้อง Voice Channel")
async def leave(interaction: discord.Interaction):
    global voice_client, queue
    if voice_client and voice_client.is_connected():
        await voice_client.disconnect()
        voice_client = None
        queue.clear() # เคลียร์คิว TTS ด้วย
        await interaction.response.send_message("✅ ออกจากห้อง Voice แล้ว")
        logging.info("Left voice channel and cleared queue.")
    else:
        await interaction.response.send_message("❌ บอทยังไม่ได้ join ห้อง Voice")

@tree.command(name="play", description="ค้นหาและเล่นเพลงจาก Spotify (บน Spotify App ของคุณ)")
@app_commands.describe(query="ชื่อเพลง, ศิลปิน, หรือลิงก์ Spotify")
async def play(interaction: discord.Interaction, query: str):
    sp_user = get_user_spotify_client(interaction.user.id)
    if not sp_user:
        await interaction.response.send_message(
            "❌ คุณยังไม่ได้เชื่อมต่อ Spotify โปรดใช้ `/link_spotify` ก่อน", 
            ephemeral=True
        )
        return

    await interaction.response.defer() # Defer เพื่อให้มีเวลาประมวลผล

    try:
        track_uris = []
        response_msg = "🎶"

        if "spotify.com/track/" in query:
            track = sp_user.track(query)
            track_uris.append(track['uri'])
            response_msg += f" กำลังเล่นเพลง: **{track['name']}** โดย **{track['artists'][0]['name']}**"
            logging.info(f"Playing Spotify track URI: {track['uri']}")
        elif "spotify.com/playlist/" in query:
            playlist_id = query.split('/')[-1].split('?')[0]
            results = sp_user.playlist_items(playlist_id)
            for item in results['items']:
                if item['track'] and item['track']['type'] == 'track': # ตรวจสอบว่าเป็น track จริงๆ
                    track_uris.append(item['track']['uri'])
            response_msg += f" กำลังเล่นเพลงจาก Spotify Playlist (พบ {len(track_uris)} เพลง)"
            logging.info(f"Playing Spotify playlist with {len(track_uris)} tracks.")
        elif "spotify.com/album/" in query:
            album_id = query.split('/')[-1].split('?')[0]
            results = sp_user.album_tracks(album_id)
            for item in results['items']:
                track_uris.append(item['uri'])
            response_msg += f" กำลังเล่นเพลงจาก Spotify Album (พบ {len(track_uris)} เพลง)"
            logging.info(f"Playing Spotify album with {len(track_uris)} tracks.")
        else: # ค้นหาด้วยชื่อเพลง
            results = sp_user.search(q=query, type='track', limit=1)
            if not results['tracks']['items']:
                await interaction.followup.send("❌ ไม่พบเพลงนี้บน Spotify")
                return
            track = results['tracks']['items'][0]
            track_uris.append(track['uri'])
            response_msg += f" กำลังเล่นเพลง: **{track['name']}** โดย **{track['artists'][0]['name']}**"
            logging.info(f"Playing Spotify search result: {track['uri']}")

        if not track_uris:
            await interaction.followup.send("❌ ไม่พบเพลงที่ต้องการเล่น หรือลิงก์ไม่ถูกต้อง")
            return

        # --- ส่วนสำคัญ: ควบคุมการเล่นบน Spotify Client ---
        # ค้นหา devices ที่เปิดอยู่และ active
        devices = sp_user.devices()
        active_device_id = None
        for device in devices['devices']:
            if device['is_active']: # ตรวจสอบว่า device กำลัง active อยู่
                active_device_id = device['id']
                break
        
        if not active_device_id:
            await interaction.followup.send("❌ ไม่พบ Spotify Client ที่กำลังทำงานอยู่ กรุณาเปิด Spotify App ของคุณ")
            return

        # เริ่มเล่นเพลง
        if "playlist" in query or "album" in query: # หากเป็น Playlist/Album
            # context_uri จะเล่น playlist/album ทั้งหมด
            if "playlist" in query:
                sp_user.start_playback(device_id=active_device_id, context_uri=f"spotify:playlist:{playlist_id}")
            elif "album" in query:
                sp_user.start_playback(device_id=active_device_id, context_uri=f"spotify:album:{album_id}")
        else: # หากเป็น Single track หรือจากการค้นหา
            sp_user.start_playback(device_id=active_device_id, uris=track_uris)
        
        await interaction.followup.send(response_msg)

    except spotipy.exceptions.SpotifyException as e:
        if e.http_status == 401: # Unauthorized - Token หมดอายุหรือมีปัญหา
            await interaction.followup.send("❌ Spotify Token หมดอายุหรือมีปัญหา โปรดใช้ `/link_spotify` อีกครั้ง")
            logging.error(f"Spotify token issue for user {interaction.user.id}: {e}")
            if interaction.user.id in spotify_users:
                del spotify_users[interaction.user.id] # ลบ token ที่ไม่ถูกต้องออก
            save_spotify_tokens() # บันทึกการเปลี่ยนแปลง
        elif e.http_status == 404 and "Device not found" in str(e):
             await interaction.followup.send("❌ ไม่พบ Spotify Client ที่กำลังทำงานอยู่ โปรดเปิด Spotify App ของคุณ")
        else:
            await interaction.followup.send(f"❌ เกิดข้อผิดพลาดในการเล่นเพลง Spotify: {e}")
            logging.error(f"Spotify API error for user {interaction.user.id}: {e}")
    except Exception as e:
        await interaction.followup.send(f"❌ เกิดข้อผิดพลาดที่ไม่คาดคิด: {e}")
        logging.error(f"Unexpected error in play command for user {interaction.user.id}: {e}")

@tree.command(name="pause", description="หยุดเพลง Spotify ชั่วคราว")
async def pause_spotify(interaction: discord.Interaction):
    sp_user = get_user_spotify_client(interaction.user.id)
    if not sp_user:
        await interaction.response.send_message("❌ คุณยังไม่ได้เชื่อมต่อ Spotify โปรดใช้ `/link_spotify`", ephemeral=True)
        return
    
    try:
        sp_user.pause_playback()
        await interaction.response.send_message("⏸️ หยุดเพลง Spotify ชั่วคราวแล้ว")
    except spotipy.exceptions.SpotifyException as e:
        await interaction.response.send_message(f"❌ เกิดข้อผิดพลาดในการหยุดเพลง: {e}")
        logging.error(f"Spotify pause error for user {interaction.user.id}: {e}")

@tree.command(name="resume", description="เล่นเพลง Spotify ต่อ")
async def resume_spotify(interaction: discord.Interaction):
    sp_user = get_user_spotify_client(interaction.user.id)
    if not sp_user:
        await interaction.response.send_message("❌ คุณยังไม่ได้เชื่อมต่อ Spotify โปรดใช้ `/link_spotify`", ephemeral=True)
        return
    
    try:
        sp_user.start_playback() # resume คือ start_playback โดยไม่มี arguments
        await interaction.response.send_message("▶️ เล่นเพลง Spotify ต่อแล้ว")
    except spotipy.exceptions.SpotifyException as e:
        await interaction.response.send_message(f"❌ เกิดข้อผิดพลาดในการเล่นเพลงต่อ: {e}")
        logging.error(f"Spotify resume error for user {interaction.user.id}: {e}")

@tree.command(name="skip", description="ข้ามเพลง Spotify")
async def skip_spotify(interaction: discord.Interaction):
    sp_user = get_user_spotify_client(interaction.user.id)
    if not sp_user:
        await interaction.response.send_message("❌ คุณยังไม่ได้เชื่อมต่อ Spotify โปรดใช้ `/link_spotify`", ephemeral=True)
        return
    
    try:
        sp_user.next_track()
        await interaction.response.send_message("⏭️ ข้ามเพลง Spotify แล้ว")
    except spotipy.exceptions.SpotifyException as e:
        await interaction.response.send_message(f"❌ เกิดข้อผิดพลาดในการข้ามเพลง: {e}")
        logging.error(f"Spotify skip error for user {interaction.user.id}: {e}")

@tree.command(name="previous", description="เล่นเพลง Spotify ก่อนหน้า")
async def previous_spotify(interaction: discord.Interaction):
    sp_user = get_user_spotify_client(interaction.user.id)
    if not sp_user:
        await interaction.response.send_message("❌ คุณยังไม่ได้เชื่อมต่อ Spotify โปรดใช้ `/link_spotify`", ephemeral=True)
        return
    
    try:
        sp_user.previous_track()
        await interaction.response.send_message("⏮️ เล่นเพลง Spotify ก่อนหน้าแล้ว")
    except spotipy.exceptions.SpotifyException as e:
        await interaction.response.send_message(f"❌ เกิดข้อผิดพลาดในการเล่นเพลงก่อนหน้า: {e}")
        logging.error(f"Spotify previous error for user {interaction.user.id}: {e}")

@tree.command(name="volume", description="ปรับระดับเสียง Spotify (0-100)")
@app_commands.describe(level="ระดับเสียง (0-100)")
async def set_spotify_volume(interaction: discord.Interaction, level: int):
    sp_user = get_user_spotify_client(interaction.user.id)
    if not sp_user:
        await interaction.response.send_message("❌ คุณยังไม่ได้เชื่อมต่อ Spotify โปรดใช้ `/link_spotify`", ephemeral=True)
        return
    
    if not (0 <= level <= 100):
        await interaction.response.send_message("❌ ระดับเสียงต้องอยู่ระหว่าง 0 ถึง 100")
        return

    try:
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


@tree.command(name="speak", description="ให้บอทพูดใน Voice Channel")
@app_commands.describe(message="ข้อความที่ต้องการให้บอทพูด")
async def speak(interaction: discord.Interaction, message: str):
    if not voice_client or not voice_client.is_connected():
        await interaction.response.send_message("❌ บอทยังไม่เข้าห้อง Voice โปรดใช้ `/join` ก่อน")
        return
    
    await interaction.response.defer() # Defer response for long TTS generation
    try:
        tts = gTTS(message, lang='th')
        tts_filename = f"tts_discord_{interaction.id}.mp3" # ใช้ interaction ID เพื่อแยกไฟล์
        tts.save(tts_filename)
        
        source = discord.FFmpegPCMAudio(tts_filename, executable="ffmpeg")
        
        if voice_client.is_playing():
            voice_client.pause()
            voice_client.play(source, after=lambda e: bot.loop.create_task(resume_audio(e, voice_client, tts_filename)))
        else:
            voice_client.play(source, after=lambda e: bot.loop.create_task(cleanup_audio(e, tts_filename)))
        
        await interaction.followup.send(f"🗣️ พูด: {message}")
        logging.info(f"TTS message from Discord: {message}")

    except Exception as e:
        await interaction.followup.send(f"❌ เกิดข้อผิดพลาดในการพูด: {e}")
        logging.error(f"Failed to speak in Discord: {e}")

# Helper for TTS cleanup (async)
async def resume_audio(error, vc, filename):
    if error:
        logging.error(f"TTS playback error: {error}")
    
    # Clean up the temporary file
    if os.path.exists(filename):
        os.remove(filename)
    
    # Resume original audio if any
    if vc and not vc.is_playing() and vc.is_paused():
        vc.resume()

async def cleanup_audio(error, filename):
    if error:
        logging.error(f"TTS playback error: {error}")
    
    # Clean up the temporary file
    if os.path.exists(filename):
        os.remove(filename)


@tree.command(name="wake", description="ปลุกผู้ใช้ด้วย DM") # ลบ guild argument ออก
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

@app.route("/")
def index():
    # ตรวจสอบว่าผู้ใช้ Discord ล็อกอินผ่านเว็บแล้วหรือยัง
    current_session_id = session.get('session_id')
    discord_user_id = web_logged_in_users.get(current_session_id)
    
    is_discord_linked = False
    if discord_user_id:
        is_discord_linked = True

    is_spotify_linked = False
    if discord_user_id:
        if get_user_spotify_client(discord_user_id):
            is_spotify_linked = True

    return render_template(
        "index.html", 
        is_discord_linked=is_discord_linked,
        discord_user_id=discord_user_id,
        is_spotify_linked=is_spotify_linked
    )

# --- Discord OAuth2 Login ---
@app.route("/login/discord")
def login_discord():
    discord_auth_url = (
        f"https://discord.com/api/oauth2/authorize"
        f"?client_id={DISCORD_CLIENT_ID}"
        f"&redirect_uri={DISCORD_REDIRECT_URI}"
        f"&response_type=code"
        f"&scope={'+'.join(DISCORD_OAUTH_SCOPES.split(' '))}"
    )
    # เก็บ session ID ของ Flask เพื่อใช้ตอน callback
    session['auth_session_id'] = os.urandom(16).hex() # สร้าง unique ID สำหรับ session นี้
    logging.info(f"Redirecting to Discord for OAuth. Auth session ID: {session['auth_session_id']}")
    return redirect(discord_auth_url)

@app.route("/callback/discord")
async def discord_callback():
    code = request.args.get("code")
    error = request.args.get("error")

    if error:
        flash(f"❌ เกิดข้อผิดพลาดในการเชื่อมต่อ Discord: {error}", "error")
        logging.error(f"Discord OAuth error: {error}")
        return redirect(url_for("index"))

    if not code:
        flash("❌ ไม่ได้รับ Authorization code จาก Discord", "error")
        logging.error("Discord OAuth: No code received.")
        return redirect(url_for("index"))

    try:
        # แลกเปลี่ยน code เป็น access token
        data = {
            "client_id": DISCORD_CLIENT_ID,
            "client_secret": DISCORD_CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": DISCORD_REDIRECT_URI,
            "scope": DISCORD_OAUTH_SCOPES,
        }
        headers = {
            "Content-Type": "application/x-www-form-urlencoded"
        }
        import httpx # ใช้ httpx แทน requests เพราะ bot.loop ต้องการ async
        async with httpx.AsyncClient() as client:
            token_response = await client.post("https://discord.com/api/oauth2/token", data=data, headers=headers)
            token_response.raise_for_status()
            token_info = token_response.json()

            access_token = token_info["access_token"]

            # ดึงข้อมูลผู้ใช้จาก Discord API
            user_headers = {
                "Authorization": f"Bearer {access_token}"
            }
            user_response = await client.get("https://discord.com/api/v10/users/@me", headers=user_headers)
            user_response.raise_for_status()
            user_data = user_response.json()
            
            discord_user_id = int(user_data["id"])
            discord_username = user_data["username"]

            # เก็บ discord_user_id ใน web_logged_in_users โดยใช้ Flask session ID
            current_session_id = session.get('auth_session_id')
            if current_session_id:
                web_logged_in_users[current_session_id] = discord_user_id
                session['discord_user_id_for_web'] = discord_user_id # ใช้เก็บใน session เพื่อแสดงผล
                flash(f"✅ เข้าสู่ระบบ Discord สำเร็จ: {discord_username} ({discord_user_id})", "success")
                logging.info(f"Discord User {discord_username} ({discord_user_id}) logged in via web.")
            else:
                flash("❌ เกิดข้อผิดพลาด: ไม่พบเซสชัน ID สำหรับการล็อกอิน", "error")
                logging.error("Discord OAuth: auth_session_id not found in session.")

    except httpx.HTTPStatusError as e:
        flash(f"❌ ข้อผิดพลาด HTTP จาก Discord API: {e.response.text}", "error")
        logging.error(f"Discord OAuth HTTP error: {e.response.text}")
    except Exception as e:
        flash(f"❌ เกิดข้อผิดพลาดในการเชื่อมต่อ Discord: {e}", "error")
        logging.error(f"Unexpected error during Discord OAuth: {e}")
    
    return redirect(url_for("index"))

@app.route("/logout/discord")
def logout_discord():
    current_session_id = session.get('session_id')
    if current_session_id in web_logged_in_users:
        del web_logged_in_users[current_session_id]
    if 'discord_user_id_for_web' in session:
        session.pop('discord_user_id_for_web', None)
    
    flash("ออกจากระบบ Discord แล้ว", "success")
    return redirect(url_for("index"))


# --- Spotify Authentication via Web (เชื่อมต่อ Spotify App ของผู้ใช้) ---
# Route สำหรับการ Login Spotify (จะถูกเรียกจาก JavaScript บนเว็บ)
@app.route("/login/spotify/<int:discord_user_id_param>")
def login_spotify_web(discord_user_id_param: int):
    # ตรวจสอบว่า discord_user_id ที่ส่งมาตรงกับที่ล็อกอินอยู่บนเว็บหรือไม่
    current_session_id = session.get('session_id')
    logged_in_discord_user_id = web_logged_in_users.get(current_session_id)

    if logged_in_discord_user_id != discord_user_id_param:
        flash("❌ Discord User ID ไม่ตรงกัน กรุณาล็อกอิน Discord ใหม่บนเว็บ หรือใช้ User ID ที่ถูกต้อง", "error")
        logging.warning(f"Mismatch Discord User ID for Spotify login. Expected {logged_in_discord_user_id}, got {discord_user_id_param}")
        return redirect(url_for("index"))

    auth_manager = SpotifyOAuth(
        client_id=SPOTIPY_CLIENT_ID,
        client_secret=SPOTIPY_CLIENT_SECRET,
        redirect_uri=SPOTIPY_REDIRECT_URI,
        scope=SPOTIPY_SCOPES,
        show_dialog=True
    )
    auth_url = auth_manager.get_authorize_url()
    
    # เก็บ Discord User ID ใน Flask Session เพื่อใช้ตอน callback
    session['spotify_auth_discord_user_id'] = discord_user_id_param
    logging.info(f"Redirecting to Spotify for OAuth for Discord User ID: {discord_user_id_param}")
    return redirect(auth_url)

# Route Callback หลังจากผู้ใช้ยืนยันตัวตนกับ Spotify
@app.route("/callback/spotify")
def spotify_callback():
    code = request.args.get("code")
    error = request.args.get("error")

    discord_user_id = session.pop('spotify_auth_discord_user_id', None) # ดึงและลบออกทันที
    
    if error:
        flash(f"❌ เกิดข้อผิดพลาดในการเชื่อมต่อ Spotify: {error}", "error")
        logging.error(f"Spotify OAuth error for user {discord_user_id}: {error}")
        return redirect(url_for("index"))

    if not code:
        flash("❌ ไม่ได้รับ Authorization code จาก Spotify", "error")
        logging.error(f"Spotify OAuth: No code received for user {discord_user_id}.")
        return redirect(url_for("index"))

    if not discord_user_id:
        flash("❌ เกิดข้อผิดพลาด: ไม่พบ Discord User ID ในเซสชันสำหรับการเชื่อมต่อ Spotify", "error")
        logging.error("Spotify callback: spotify_auth_discord_user_id not found in session.")
        return redirect(url_for("index"))

    auth_manager = SpotifyOAuth(
        client_id=SPOTIPY_CLIENT_ID,
        client_secret=SPOTIPY_CLIENT_SECRET,
        redirect_uri=SPOTIPY_REDIRECT_URI,
        scope=SPOTIPY_SCOPES,
    )

    try:
        token_info = auth_manager.get_access_token(code)
        # สร้าง auth_manager ใหม่ด้วย token_info ที่เพิ่งได้รับ
        auth_manager_with_token = SpotifyOAuth(
            client_id=SPOTIPY_CLIENT_ID,
            client_secret=SPOTIPY_CLIENT_SECRET,
            redirect_uri=SPOTIPY_REDIRECT_URI,
            scope=SPOTIPY_SCOPES,
            token_info=token_info # ส่ง token_info เข้าไปตรงๆ
        )
        sp_user = spotipy.Spotify(auth_manager=auth_manager_with_token)
        spotify_users[discord_user_id] = sp_user
        
        save_spotify_tokens() # บันทึก token ลงไฟล์
        
        flash("✅ เชื่อมต่อ Spotify สำเร็จ!", "success")
        logging.info(f"User {discord_user_id} successfully linked Spotify.")
    except Exception as e:
        flash(f"❌ เกิดข้อผิดพลาดในการเชื่อมต่อ Spotify: {e}", "error")
        logging.error(f"Error during Spotify callback for user {discord_user_id}: {e}")
    
    return redirect(url_for("index"))

# --- Flask Routes สำหรับควบคุม Spotify (ต้องมีผู้ใช้ล็อกอิน Discord และ Spotify ก่อน) ---
@app.route("/control_spotify/<action>")
async def control_spotify(action: str):
    # ต้องมี Discord User ID จากการล็อกอินบนเว็บก่อน
    current_session_id = session.get('session_id')
    discord_user_id = web_logged_in_users.get(current_session_id)
    
    if not discord_user_id:
        flash("❌ โปรดเข้าสู่ระบบ Discord บนหน้าเว็บก่อน", "error")
        return redirect(url_for("index"))
    
    sp_user = get_user_spotify_client(discord_user_id)
    if not sp_user:
        flash("❌ โปรดเชื่อมต่อบัญชี Spotify ของคุณก่อน", "error")
        return redirect(url_for("index"))

    try:
        response_message = ""
        if action == "play_query":
            query = request.args.get('query')
            if not query:
                flash("❌ โปรดใส่ข้อความค้นหาหรือลิงก์ Spotify", "error")
                return redirect(url_for("index"))
            
            # ต้องเรียกฟังก์ชัน play ของบอทผ่าน bot.loop.create_task
            # แต่ฟังก์ชัน play ของบอทรับ interaction ดังนั้นต้องปรับ
            # หรือสร้างฟังก์ชัน helper แยกสำหรับ Flask
            
            track_uris = []
            if "spotify.com/track/" in query:
                track = sp_user.track(query)
                track_uris.append(track['uri'])
                response_message = f"🎶 กำลังเล่นเพลง: **{track['name']}** โดย **{track['artists'][0]['name']}**"
            elif "spotify.com/playlist/" in query:
                playlist_id = query.split('/')[-1].split('?')[0]
                results = sp_user.playlist_items(playlist_id)
                for item in results['items']:
                    if item['track'] and item['track']['type'] == 'track':
                        track_uris.append(item['track']['uri'])
                response_message = f"🎶 กำลังเล่นเพลงจาก Spotify Playlist (พบ {len(track_uris)} เพลง)"
            elif "spotify.com/album/" in query:
                album_id = query.split('/')[-1].split('?')[0]
                results = sp_user.album_tracks(album_id)
                for item in results['items']:
                    track_uris.append(item['uri'])
                response_message = f"🎶 กำลังเล่นเพลงจาก Spotify Album (พบ {len(track_uris)} เพลง)"
            else: # ค้นหาด้วยชื่อเพลง
                results = sp_user.search(q=query, type='track', limit=1)
                if not results['tracks']['items']:
                    flash("❌ ไม่พบเพลงนี้บน Spotify", "error")
                    return redirect(url_for("index"))
                track = results['tracks']['items'][0]
                track_uris.append(track['uri'])
                response_message = f"🎶 กำลังเล่นเพลง: **{track['name']}** โดย **{track['artists'][0]['name']}**"

            if not track_uris:
                flash("❌ ไม่พบเพลงที่ต้องการเล่น หรือลิงก์ไม่ถูกต้อง", "error")
                return redirect(url_for("index"))
            
            devices = sp_user.devices()
            active_device_id = None
            for device in devices['devices']:
                if device['is_active']:
                    active_device_id = device['id']
                    break
            
            if not active_device_id:
                flash("❌ ไม่พบ Spotify Client ที่กำลังทำงานอยู่ กรุณาเปิด Spotify App ของคุณ", "error")
                return redirect(url_for("index"))

            if "playlist" in query or "album" in query:
                if "playlist" in query:
                    sp_user.start_playback(device_id=active_device_id, context_uri=f"spotify:playlist:{playlist_id}")
                elif "album" in query:
                    sp_user.start_playback(device_id=active_device_id, context_uri=f"spotify:album:{album_id}")
            else:
                sp_user.start_playback(device_id=active_device_id, uris=track_uris)

            flash(response_message, "success")

        elif action == "pause":
            sp_user.pause_playback()
            flash("⏸️ หยุดเพลง Spotify แล้ว", "success")
        elif action == "resume":
            sp_user.start_playback()
            flash("▶️ เล่นเพลง Spotify ต่อแล้ว", "success")
        elif action == "skip":
            sp_user.next_track()
            flash("⏭️ ข้ามเพลง Spotify แล้ว", "success")
        elif action == "previous":
            sp_user.previous_track()
            flash("⏮️ เล่นเพลงก่อนหน้าแล้ว", "success")
        elif action == "set_volume":
            level_str = request.args.get('level')
            if not level_str:
                flash("❌ โปรดใส่ระดับเสียง", "error")
                return redirect(url_for("index"))
            level = int(level_str)
            if not (0 <= level <= 100):
                flash("❌ ระดับเสียงต้องอยู่ระหว่าง 0 ถึง 100", "error")
                return redirect(url_for("index"))
            
            devices = sp_user.devices()
            active_device_id = None
            for device in devices['devices']:
                if device['is_active']:
                    active_device_id = device['id']
                    break
            
            if not active_device_id:
                flash("❌ ไม่พบ Spotify Client ที่กำลังทำงานอยู่", "error")
                return redirect(url_for("index"))

            sp_user.volume(level, device_id=active_device_id)
            flash(f"🔊 ปรับระดับเสียง Spotify เป็น {level}% แล้ว", "success")
        elif action == "devices":
            devices = sp_user.devices()
            device_names = [d['name'] for d in devices['devices']]
            if device_names:
                flash(f"📱 อุปกรณ์ที่ใช้งาน: {', '.join(device_names)}", "info")
            else:
                flash("❌ ไม่พบอุปกรณ์ Spotify ที่เชื่อมต่อ", "info")
        else:
            flash("❌ คำสั่งไม่ถูกต้อง", "error")
    
    except spotipy.exceptions.SpotifyException as e:
        if e.http_status == 401:
            flash("❌ Spotify Token หมดอายุ โปรดเชื่อมต่อใหม่", "error")
            if discord_user_id in spotify_users:
                del spotify_users[discord_user_id]
            save_spotify_tokens()
        elif e.http_status == 404 and "Device not found" in str(e):
             flash("❌ ไม่พบ Spotify Client ที่กำลังทำงานอยู่ โปรดเปิด Spotify App ของคุณ", "error")
        else:
            flash(f"❌ เกิดข้อผิดพลาด Spotify: {e}", "error")
        logging.error(f"Spotify web control error for user {discord_user_id} ({action}): {e}")
    except Exception as e:
        flash(f"❌ เกิดข้อผิดพลาดที่ไม่คาดคิด: {e}", "error")
        logging.error(f"Web control error for user {discord_user_id} ({action}): {e}")
    
    return redirect(url_for("index"))


# --- Flask Routes สำหรับควบคุม Discord Voice (TTS) ---
@app.route("/control_discord/join")
async def join_web():
    # สำหรับการ join ผ่านเว็บ ควรมีการระบุ Guild และ Channel ID
    # แต่เนื่องจากบอทไม่ได้รู้ว่าผู้ใช้เว็บอยู่ Guild ไหน
    # คำสั่งนี้จึงทำได้แค่ตรวจสอบสถานะ หรือให้ผู้ใช้ใช้ Discord command แทน
    # เพื่อแก้บัค BuildError เราจะแค่ส่งข้อความกลับ
    flash("💡 โปรดใช้คำสั่ง `/join` ใน Discord โดยตรง เพื่อให้บอทเข้าห้องเสียงที่คุณอยู่", "info")
    return redirect(url_for("index"))

@app.route("/control_discord/leave")
async def leave_web():
    global voice_client, queue
    if voice_client and voice_client.is_connected():
        await voice_client.disconnect()
        voice_client = None
        queue.clear()
        flash("✅ ออกจากห้อง Voice แล้ว", "success")
        logging.info("Left voice channel via web and cleared queue.")
    else:
        flash("❌ บอทยังไม่ได้ join ห้อง Voice", "error")
    return redirect(url_for("index"))

@app.route("/control_discord/speak", methods=["POST"])
async def speak_web():
    message = request.form.get("message")
    if not message:
        flash("❌ โปรดใส่ข้อความที่ต้องการให้บอทพูด", "error")
        return redirect(url_for("index"))
    
    if not voice_client or not voice_client.is_connected():
        flash("❌ บอทยังไม่เข้าห้อง Voice โปรดใช้ `/join` ใน Discord ก่อน", "error")
        return redirect(url_for("index"))
    
    try:
        tts = gTTS(message, lang='th')
        tts_filename = f"tts_web_{os.urandom(8).hex()}.mp3" # ชื่อไฟล์ไม่ซ้ำกัน
        tts.save(tts_filename)
        
        source = discord.FFmpegPCMAudio(tts_filename, executable="ffmpeg")
        
        # รันการเล่นเสียงใน Discord loop
        if voice_client.is_playing():
            voice_client.pause()
            voice_client.play(source, after=lambda e: bot.loop.create_task(resume_audio(e, voice_client, tts_filename)))
        else:
            voice_client.play(source, after=lambda e: bot.loop.create_task(cleanup_audio(e, tts_filename)))
            
        flash(f"🗣️ บอทกำลังพูด: {message}", "success")
        logging.info(f"TTS message from web: {message}")
    except Exception as e:
        flash(f"❌ เกิดข้อผิดพลาดในการพูด: {e}", "error")
        logging.error(f"Failed to speak via web: {e}")
    return redirect(url_for("index"))


# --- Running Bot and Flask ---
def run_web():
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)

if __name__ == "__main__":
    # รัน Flask app ใน main thread
    web_thread = threading.Thread(target=run_web, daemon=True)
    web_thread.start()
    
    if os.getenv('FLASK_RUNNING_VIA_GUNICORN') == 'true':
        # ถ้าเป็น Gunicorn, Flask app จะถูกรันโดย Gunicorn โดยตรง
        # เราแค่ต้องรันบอท Discord ในอีก thread
        logging.info("Running Discord bot in a separate thread for Gunicorn web server.")
        bot_thread = threading.Thread(target=bot.run, args=(DISCORD_TOKEN,), daemon=True)
        bot_thread.start()
        # Gunicorn จะรับผิดชอบการรัน app = Flask(__name__, ...)
    else:
        # ถ้าไม่ได้รันผ่าน Gunicorn (เช่น รัน `python main.py` โดยตรง)
        # เราจะรัน Flask ใน thread และ Discord Bot ใน main thread
        logging.info("Running Flask web server in a separate thread for local development/bot process.")
        web_thread = threading.Thread(target=lambda: app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=True, use_reloader=False), daemon=True)
        web_thread.start()
        # ตั้งค่า use_reloader=False เพื่อป้องกันการรันซ้ำเมื่อ Flask ถูก reload
        
        # กำหนดให้ bot.run ทำงานใน main thread
        bot.run(DISCORD_TOKEN)