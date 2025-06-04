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
import httpx
import asyncio

# --- โหลดตัวแปรสภาพแวดล้อมจากไฟล์ .env ---
load_dotenv()

# --- ตั้งค่า Spotify API Credentials ---
SPOTIPY_CLIENT_ID = os.getenv("SPOTIPY_CLIENT_ID")
SPOTIPY_CLIENT_SECRET = os.getenv("SPOTIPY_CLIENT_SECRET")
SPOTIPY_REDIRECT_URI = os.getenv("SPOTIPY_REDIRECT_URI")
SPOTIPY_SCOPES = "user-read-playback-state user-modify-playback-state user-read-currently-playing playlist-read-private playlist-read-collaborative user-library-read"

# --- ตั้งค่า Discord Bot Credentials ---
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
YOUR_GUILD_ID = int(os.environ["GUILD_ID"])
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
DISCORD_REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI")
DISCORD_OAUTH_SCOPES = "identify guilds"

# --- Global Dictionaries / Variables ---
spotify_users = {} # Key: Discord User ID, Value: spotipy.Spotify client
web_logged_in_users = {} # Key: Flask Session ID, Value: Discord User ID
voice_client = None
queue = [] # ยังไม่ใช้สำหรับ Spotify playback ในโค้ดนี้

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
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# เพิ่ม flag เพื่อตรวจสอบว่าบอทพร้อมใช้งานหรือไม่
bot_ready = asyncio.Event()

# --- ตั้งค่า Flask App ---
app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = os.getenv("FLASK_SECRET_KEY") or os.urandom(24) # Fallback secret key

# --- Discord Bot Events ---
@bot.event
async def on_ready():
    global bot_ready
    if not discord.opus.is_loaded():
        try:
            # ตรวจสอบว่า libopus.so อยู่ใน PATH หรือระบุ path ที่ถูกต้อง
            # สำหรับ Windows ใช้ 'opus.dll'
            # สำหรับ macOS ใช้ 'libopus.dylib'
            # สำหรับ Linux ใช้ 'libopus.so'
            discord.opus.load_opus('libopus.so') 
            logging.info("Opus loaded successfully.")
        except Exception as e:
            logging.error(f"Failed to load opus: {e}. Please ensure libopus.so is in your PATH or specified correctly.", exc_info=True)
            print(f"Failed to load opus: {e}. Please ensure libopus.so is in your PATH or specified correctly.")

    print(f"✅ Bot logged in as {bot.user}")
    logging.info(f"Bot logged in as {bot.user}")

    await tree.sync()
    logging.info("Global commands synced.")

    guild_obj = discord.Object(id=YOUR_GUILD_ID)
    await tree.sync(guild=guild_obj)
    logging.info(f"Commands synced to guild: {YOUR_GUILD_ID}")

    try:
        if os.path.exists("spotify_tokens.json"):
            with open("spotify_tokens.json", "r") as f:
                tokens_data = json.load(f)
            for user_id_str, token_info in tokens_data.items():
                user_id = int(user_id_str)
                auth_manager = SpotifyOAuth(
                    client_id=SPOTIPY_CLIENT_ID,
                    client_secret=SPOTIPY_CLIENT_SECRET,
                    redirect_uri=SPOTIPY_REDIRECT_URI,
                    scope=SPOTIPY_SCOPES,
                    token_info=token_info # โหลด token_info ที่บันทึกไว้
                )
                sp_user = spotipy.Spotify(auth_manager=auth_manager)
                try:
                    # ลองเรียก API เพื่อตรวจสอบว่า token ยังใช้ได้หรือไม่
                    sp_user.current_user() 
                    spotify_users[user_id] = sp_user
                    logging.info(f"Loaded and verified Spotify token for user ID: {user_id}")
                except spotipy.exceptions.SpotifyException as e:
                    logging.warning(f"Spotify token for user {user_id} expired or invalid on startup. Removing. Error: {e}")
                    # ลบ token ที่ใช้ไม่ได้ออกจากไฟล์
                    tokens_data.pop(user_id_str, None)
            # เขียนไฟล์คืนหลังจากลบ token ที่หมดอายุ
            with open("spotify_tokens.json", "w") as f:
                json.dump(tokens_data, f, indent=4)
            if not tokens_data:
                logging.info("All saved Spotify tokens were invalid or expired. spotify_tokens.json is now empty.")

        else:
            logging.info("No saved spotify_tokens.json found.")
    except FileNotFoundError: # ไม่น่าจะถูกเรียกเพราะตรวจสอบด้วย os.path.exists แล้ว
        logging.info("No saved Spotify tokens found.")
    except json.JSONDecodeError:
        logging.error("Error decoding spotify_tokens.json. File might be corrupted.", exc_info=True)
    except Exception as e:
        logging.error(f"Error loading Spotify tokens: {e}", exc_info=True)
    
    bot_ready.set() # ตั้งค่า Event เพื่อบอกว่าบอทพร้อมแล้ว

# --- Helper Function: Get Spotify Client for User ---
def get_user_spotify_client(discord_user_id: int):
    sp_client = spotify_users.get(discord_user_id)
    if sp_client:
        try:
            # ใช้ ensure_http_connection เพื่อรีเฟรช token ถ้าจำเป็น
            sp_client.current_user() 
            return sp_client
        except spotipy.exceptions.SpotifyException as e:
            logging.warning(f"Spotify token expired or invalid for user {discord_user_id}: {e}")
            if discord_user_id in spotify_users:
                del spotify_users[discord_user_id]
                save_spotify_tokens() # Save updated tokens after removal
            return None
    return None

# --- Helper Function: Save Spotify Tokens ---
def save_spotify_tokens():
    try:
        tokens_data = {}
        for user_id, sp_client in spotify_users.items():
            # ดึง cached token มาเก็บ
            token_info = sp_client.auth_manager.get_cached_token()
            if token_info:
                tokens_data[str(user_id)] = token_info
        
        with open("spotify_tokens.json", "w") as f:
            json.dump(tokens_data, f, indent=4)
        logging.info("Saved Spotify tokens to file.")
    except Exception as e:
        logging.error(f"Error saving Spotify tokens: {e}", exc_info=True)


# --- Discord Slash Commands ---

@tree.command(name="link_spotify", description="เชื่อมต่อบัญชี Spotify ของคุณ")
async def link_spotify(interaction: discord.Interaction):
    # ตรวจสอบว่าผู้ใช้ Discord คนนี้เคยล็อกอินผ่านหน้าเว็บ Flask หรือไม่
    # นี่เป็นวิธีพื้นฐานในการเชื่อมโยง session บนเว็บกับ Discord User ID
    # ที่ปลอดภัยกว่าคือการให้ผู้ใช้ใช้ `/login/discord` บนเว็บก่อน
    # แล้วค่อยใช้คำสั่งนี้บน Discord เพื่อให้บอทรู้จัก User ID ที่มาจากเว็บ
    
    # หากต้องการให้ทำงานได้ทันที ให้ผู้ใช้เข้าหน้าเว็บ index ก่อน เพื่อสร้าง Flask session
    # และบอทจะบันทึก Discord User ID ลงใน web_logged_in_users เมื่อผู้ใช้ Discord login บนเว็บ
    
    # ในตัวอย่างนี้ เราจะบอกให้ผู้ใช้ไปที่หน้าเว็บเพื่อล็อกอิน Discord ก่อนเสมอ
    
    # ดึง session_id จากการโต้ตอบ Discord เพื่อพยายามผูกกับ web_logged_in_users
    # นี่เป็นแนวทางที่ซับซ้อนขึ้น อาจต้องพิจารณาว่าต้องการให้ผู้ใช้ทำขั้นตอนไหนก่อน
    
    # Simplification: แจ้งให้ผู้ใช้ไปที่หน้าเว็บเพื่อทำ OAuth
    await interaction.response.send_message(
        f"🔗 หากต้องการเชื่อมต่อบัญชี Spotify กับบอท โปรดไปที่หน้าเว็บของเรา:\n"
        f"**{url_for('index', _external=True)}** \n"
        f"จากนั้นล็อกอิน Discord บนหน้าเว็บ **ก่อน** แล้วค่อยกดเชื่อมต่อ Spotify", 
        ephemeral=True
    )
    logging.info(f"Sent Spotify web link instruction to user {interaction.user.id}")


@tree.command(name="join", description="ให้บอทเข้าห้อง Voice Channel ของคุณ")
async def join(interaction: discord.Interaction):
    global voice_client
    if interaction.user.voice:
        channel = interaction.user.voice.channel
        if voice_client and voice_client.is_connected():
            if voice_client.channel.id == channel.id:
                await interaction.response.send_message(f"✅ บอทอยู่ในห้อง **{channel.name}** แล้ว")
            else:
                await voice_client.move_to(channel)
                await interaction.response.send_message(f"✅ ย้ายบอทไปห้อง **{channel.name}** แล้ว")
                logging.info(f"Moved voice client to: {channel.name}")
        else:
            voice_client = await channel.connect()
            await interaction.response.send_message(f"✅ เข้าห้อง **{channel.name}** แล้ว")
            logging.info(f"Joined voice channel: {channel.name}")
    else:
        await interaction.response.send_message("❌ คุณยังไม่ได้อยู่ใน voice channel")

@tree.command(name="leave", description="ให้บอทออกจากห้อง Voice Channel")
async def leave(interaction: discord.Interaction):
    global voice_client, queue
    if voice_client and voice_client.is_connected():
        if voice_client.is_playing():
            voice_client.stop()
        await voice_client.disconnect()
        voice_client = None
        queue.clear()
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
            "❌ คุณยังไม่ได้เชื่อมต่อ Spotify โปรดใช้ `/link_spotify` ก่อน หรือเข้าสู่ระบบบนหน้าเว็บ", 
            ephemeral=True
        )
        return

    await interaction.response.defer()

    try:
        track_uris = []
        response_msg = "🎶"
        context_uri = None

        # ตรวจสอบว่าเป็นลิงก์ Spotify หรือไม่
        if "spotify.com/track/" in query:
            track_id = query.split('/')[-1].split('?')[0]
            track_uri = f"spotify:track:{track_id}"
            track = await asyncio.to_thread(sp_user.track, track_uri)
            track_uris.append(track_uri)
            response_msg += f" กำลังเล่นเพลง: **{track['name']}** โดย **{track['artists'][0]['name']}**"
            logging.info(f"Playing Spotify track URI: {track_uri}")
        elif "spotify.com/playlist/" in query:
            playlist_id = query.split('/')[-1].split('?')[0]
            context_uri = f"spotify:playlist:{playlist_id}"
            playlist = await asyncio.to_thread(sp_user.playlist, playlist_id)
            response_msg += f" กำลังเล่น Spotify Playlist: **{playlist['name']}**"
            logging.info(f"Playing Spotify playlist: {context_uri}")
        elif "spotify.com/album/" in query:
            album_id = query.split('/')[-1].split('?')[0]
            context_uri = f"spotify:album:{album_id}"
            album = await asyncio.to_thread(sp_user.album, album_id)
            response_msg += f" กำลังเล่น Spotify Album: **{album['name']}**"
            logging.info(f"Playing Spotify album: {context_uri}")
        else: # ค้นหาด้วยชื่อเพลง
            results = await asyncio.to_thread(sp_user.search, q=query, type='track', limit=1)
            if not results['tracks']['items']:
                await interaction.followup.send("❌ ไม่พบเพลงนี้บน Spotify")
                return
            track = results['tracks']['items'][0]
            track_uris.append(track['uri'])
            response_msg += f" กำลังเล่นเพลง: **{track['name']}** โดย **{track['artists'][0]['name']}**"
            logging.info(f"Playing Spotify search result: {track['uri']}")

        if not track_uris and not context_uri:
            await interaction.followup.send("❌ ไม่พบเพลงที่ต้องการเล่น หรือลิงก์ไม่ถูกต้อง")
            return

        devices = await asyncio.to_thread(sp_user.devices)
        active_device_id = None
        for device in devices['devices']:
            if device['is_active']:
                active_device_id = device['id']
                break
        
        if not active_device_id:
            await interaction.followup.send("❌ ไม่พบ Spotify Client ที่กำลังทำงานอยู่ กรุณาเปิด Spotify App ของคุณ")
            return

        if context_uri:
            await asyncio.to_thread(sp_user.start_playback, device_id=active_device_id, context_uri=context_uri)
        else:
            await asyncio.to_thread(sp_user.start_playback, device_id=active_device_id, uris=track_uris)
        
        await interaction.followup.send(response_msg)

    except spotipy.exceptions.SpotifyException as e:
        if e.http_status == 401:
            await interaction.followup.send("❌ Spotify Token หมดอายุหรือมีปัญหา โปรดใช้ `/link_spotify` อีกครั้ง หรือเข้าสู่ระบบบนหน้าเว็บ")
            logging.error(f"Spotify token issue for user {interaction.user.id}: {e}")
            if interaction.user.id in spotify_users:
                del spotify_users[interaction.user.id]
                save_spotify_tokens()
        elif e.http_status == 404 and "Device not found" in str(e):
            await interaction.followup.send("❌ ไม่พบ Spotify Client ที่กำลังทำงานอยู่ โปรดเปิด Spotify App ของคุณ")
        elif e.http_status == 403 and "Player command failed" in str(e):
            await interaction.followup.send("❌ การเล่นเพลงล้มเหลว โปรดตรวจสอบว่า Spotify App ของคุณเปิดอยู่และกำลังเล่นเพลง")
        else:
            await interaction.followup.send(f"❌ เกิดข้อผิดพลาดในการเล่นเพลง Spotify: {e}")
        logging.error(f"Spotify API error for user {interaction.user.id}: {e}", exc_info=True)
    except Exception as e:
        await interaction.followup.send(f"❌ เกิดข้อผิดพลาดที่ไม่คาดคิด: {e}")
        logging.error(f"Unexpected error in play command for user {interaction.user.id}: {e}", exc_info=True)

@tree.command(name="pause", description="หยุดเพลง Spotify ชั่วคราว")
async def pause_spotify(interaction: discord.Interaction):
    sp_user = get_user_spotify_client(interaction.user.id)
    if not sp_user:
        await interaction.response.send_message("❌ คุณยังไม่ได้เชื่อมต่อ Spotify โปรดใช้ `/link_spotify`", ephemeral=True)
        return
    
    try:
        await asyncio.to_thread(sp_user.pause_playback)
        await interaction.response.send_message("⏸️ หยุดเพลง Spotify ชั่วคราวแล้ว")
    except spotipy.exceptions.SpotifyException as e:
        await interaction.response.send_message(f"❌ เกิดข้อผิดพลาดในการหยุดเพลง: {e}")
        logging.error(f"Spotify pause error for user {interaction.user.id}: {e}", exc_info=True)

@tree.command(name="resume", description="เล่นเพลง Spotify ต่อ")
async def resume_spotify(interaction: discord.Interaction):
    sp_user = get_user_spotify_client(interaction.user.id)
    if not sp_user:
        await interaction.response.send_message("❌ คุณยังไม่ได้เชื่อมต่อ Spotify โปรดใช้ `/link_spotify`", ephemeral=True)
        return
    
    try:
        await asyncio.to_thread(sp_user.start_playback)
        await interaction.response.send_message("▶️ เล่นเพลง Spotify ต่อแล้ว")
    except spotipy.exceptions.SpotifyException as e:
        await interaction.response.send_message(f"❌ เกิดข้อผิดพลาดในการเล่นเพลงต่อ: {e}")
        logging.error(f"Spotify resume error for user {interaction.user.id}: {e}", exc_info=True)

@tree.command(name="skip", description="ข้ามเพลง Spotify")
async def skip_spotify(interaction: discord.Interaction):
    sp_user = get_user_spotify_client(interaction.user.id)
    if not sp_user:
        await interaction.response.send_message("❌ คุณยังไม่ได้เชื่อมต่อ Spotify โปรดใช้ `/link_spotify`", ephemeral=True)
        return
    
    try:
        await asyncio.to_thread(sp_user.next_track)
        await interaction.response.send_message("⏭️ ข้ามเพลง Spotify แล้ว")
    except spotipy.exceptions.SpotifyException as e:
        await interaction.response.send_message(f"❌ เกิดข้อผิดพลาดในการข้ามเพลง: {e}")
        logging.error(f"Spotify skip error for user {interaction.user.id}: {e}", exc_info=True)

@tree.command(name="previous", description="เล่นเพลง Spotify ก่อนหน้า")
async def previous_spotify(interaction: discord.Interaction):
    sp_user = get_user_spotify_client(interaction.user.id)
    if not sp_user:
        await interaction.response.send_message("❌ คุณยังไม่ได้เชื่อมต่อ Spotify โปรดใช้ `/link_spotify`", ephemeral=True)
        return
    
    try:
        await asyncio.to_thread(sp_user.previous_track)
        await interaction.response.send_message("⏮️ เล่นเพลง Spotify ก่อนหน้าแล้ว")
    except spotipy.exceptions.SpotifyException as e:
        await interaction.response.send_message(f"❌ เกิดข้อผิดพลาดในการเล่นเพลงก่อนหน้า: {e}")
        logging.error(f"Spotify previous error for user {interaction.user.id}: {e}", exc_info=True)

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
        devices = await asyncio.to_thread(sp_user.devices)
        active_device_id = None
        for device in devices['devices']:
            if device['is_active']:
                active_device_id = device['id']
                break
        
        if not active_device_id:
            await interaction.response.send_message("❌ ไม่พบ Spotify Client ที่กำลังทำงานอยู่")
            return

        await asyncio.to_thread(sp_user.volume, level, device_id=active_device_id)
        await interaction.response.send_message(f"🔊 ปรับระดับเสียง Spotify เป็น **{level}%** แล้ว")
    except spotipy.exceptions.SpotifyException as e:
        await interaction.response.send_message(f"❌ เกิดข้อผิดพลาดในการปรับเสียง: {e}")
        logging.error(f"Spotify volume error for user {interaction.user.id}: {e}", exc_info=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ เกิดข้อผิดพลาดที่ไม่คาดคิด: {e}")
        logging.error(f"Unexpected error in set_spotify_volume command for user {interaction.user.id}: {e}", exc_info=True)


@tree.command(name="speak", description="ให้บอทพูดใน Voice Channel")
@app_commands.describe(message="ข้อความที่ต้องการให้บอทพูด")
async def speak(interaction: discord.Interaction, message: str):
    if not voice_client or not voice_client.is_connected():
        await interaction.response.send_message("❌ บอทยังไม่เข้าห้อง Voice โปรดใช้ `/join` ก่อน")
        return
    
    await interaction.response.defer()
    try:
        # gTTS เป็น blocking operation ควรเรียกใน thread pool
        tts_filename = f"tts_discord_{interaction.id}.mp3"
        await asyncio.to_thread(gTTS(message, lang='th').save, tts_filename)
        
        # FFmpegPCMAudio ควรระบุ executable path สำหรับ FFmpeg ถ้าไม่อยู่ใน PATH
        source = discord.FFmpegPCMAudio(tts_filename, executable="ffmpeg") 
        
        if voice_client.is_playing():
            voice_client.pause() # หยุดเพลงที่กำลังเล่นชั่วคราว
            voice_client.play(source, after=lambda e: bot.loop.create_task(resume_audio(e, voice_client, tts_filename)))
        else:
            voice_client.play(source, after=lambda e: bot.loop.create_task(cleanup_audio(e, tts_filename)))
        
        await interaction.followup.send(f"🗣️ พูด: **{message}**")
        logging.info(f"TTS message from Discord: {message}")

    except Exception as e:
        await interaction.followup.send(f"❌ เกิดข้อผิดพลาดในการพูด: {e}\nตรวจสอบว่าติดตั้ง FFmpeg และ Opus แล้ว")
        logging.error(f"Failed to speak in Discord: {e}", exc_info=True)

# Helper for TTS cleanup (async)
async def resume_audio(error, vc, filename):
    if error:
        logging.error(f"TTS playback error during resume: {error}")
    if os.path.exists(filename):
        os.remove(filename)
    # ตรวจสอบว่า vc ยังเชื่อมต่ออยู่ก่อนที่จะ resume
    if vc and vc.is_connected() and vc.is_paused():
        try:
            vc.resume()
            logging.info("Resumed audio playback after TTS.")
        except Exception as e:
            logging.error(f"Error resuming audio after TTS: {e}")


async def cleanup_audio(error, filename):
    if error:
        logging.error(f"TTS playback error during cleanup: {error}")
    if os.path.exists(filename):
        os.remove(filename)
        logging.info(f"Cleaned up TTS file: {filename}")


@tree.command(name="wake", description="ปลุกผู้ใช้ด้วย DM")
@app_commands.describe(user="เลือกผู้ใช้")
async def wake(interaction: discord.Interaction, user: discord.User):
    try:
        await user.send(f"⏰ คุณถูก **{interaction.user.name}** ปลุก! ตื่นนน!")
        await interaction.response.send_message(f"✅ ปลุก **{user.name}** แล้ว")
        logging.info(f"{interaction.user.name} woke up {user.name}.")
    except discord.Forbidden:
        await interaction.response.send_message(f"❌ ไม่สามารถส่งข้อความ DM ไปยัง {user.name} ได้ (อาจจะปิด DM หรือเราไม่สามารถส่งได้)")
        logging.warning(f"Bot could not send DM to {user.name}.")
    except Exception as e:
        await interaction.response.send_message(f"❌ เกิดข้อผิดพลาดในการปลุก: {e}")
        logging.error(f"Failed to wake user {user.name}: {e}", exc_info=True)


# --- Flask Routes ---

@app.route("/")
def index():
    current_session_id = session.get('session_id')
    if not current_session_id:
        current_session_id = os.urandom(16).hex()
        session['session_id'] = current_session_id
        logging.info(f"New Flask session created: {current_session_id}")

    discord_user_id = web_logged_in_users.get(current_session_id)
    
    is_discord_linked = False
    is_spotify_linked = False

    if discord_user_id:
        is_discord_linked = True
        
        # ต้องรอให้ bot_ready ก่อนเรียก get_user_spotify_client ที่เข้าถึง bot.loop
        bot.loop.run_until_complete(bot_ready.wait())

        # เรียก _check_spotify_link_status ใน bot.loop
        future = asyncio.run_coroutine_threadsafe(
            _check_spotify_link_status(discord_user_id),
            bot.loop
        )
        is_spotify_linked = future.result() # รอผลลัพธ์ใน synchronous context

    return render_template(
        "index.html",
        is_discord_linked=is_discord_linked,
        discord_user_id=discord_user_id,
        is_spotify_linked=is_spotify_linked
    )

# Helper function เพื่อตรวจสอบสถานะ Spotify link แบบ Async
async def _check_spotify_link_status(discord_user_id):
    return get_user_spotify_client(discord_user_id) is not None

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
    logging.info(f"Redirecting to Discord for OAuth.")
    return redirect(discord_auth_url)

@app.route("/callback/discord")
def discord_callback():
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
        # รอให้บอทพร้อมก่อน (ถ้ายังไม่พร้อม)
        bot.loop.run_until_complete(bot_ready.wait())

        # ใช้ asyncio.run_coroutine_threadsafe เพื่อรัน coroutine ใน bot.loop
        token_info, user_data = asyncio.run_coroutine_threadsafe(
            _fetch_discord_token_and_user(code),
            bot.loop
        ).result() # ใช้ .result() เพื่อรอผลลัพธ์ใน synchronous context
        
        discord_user_id = int(user_data["id"])
        discord_username = user_data["username"]

        current_session_id = session.get('session_id') # ใช้ session_id เดียวกัน
        if not current_session_id: # หากไม่มี session_id ใน callback (ไม่น่าเกิดขึ้นถ้า index ทำงาน)
            current_session_id = os.urandom(16).hex()
            session['session_id'] = current_session_id
            logging.info(f"New Flask session created during Discord callback: {current_session_id}")

        web_logged_in_users[current_session_id] = discord_user_id
        session['discord_user_id_for_web'] = discord_user_id # เก็บไว้ใน session เพื่อความสะดวก
        
        flash(f"✅ เข้าสู่ระบบ Discord สำเร็จ: {discord_username} ({discord_user_id})", "success")
        logging.info(f"Discord User {discord_username} ({discord_user_id}) logged in via web.")

    except httpx.HTTPStatusError as e:
        flash(f"❌ ข้อผิดพลาด HTTP จาก Discord API: {e.response.text}", "error")
        logging.error(f"Discord OAuth HTTP error: {e.response.text}", exc_info=True)
    except Exception as e:
        flash(f"❌ เกิดข้อผิดพลาดในการเชื่อมต่อ Discord: {e}", "error")
        logging.error(f"Unexpected error during Discord OAuth: {e}", exc_info=True) # เพิ่ม exc_info=True เพื่อดู stack trace
    
    return redirect(url_for("index"))

# Helper function สำหรับ fetch Discord token และ user data แบบ async
async def _fetch_discord_token_and_user(code):
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
    async with httpx.AsyncClient() as client:
        token_response = await client.post("https://discord.com/api/oauth2/token", data=data, headers=headers)
        token_response.raise_for_status()
        token_info = token_response.json()

        access_token = token_info["access_token"]

        user_headers = {
            "Authorization": f"Bearer {access_token}"
        }
        user_response = await client.get("https://discord.com/api/v10/users/@me", headers=user_headers)
        user_response.raise_for_status()
        user_data = user_response.json()
        return token_info, user_data


@app.route("/logout/discord")
def logout_discord():
    current_session_id = session.get('session_id')
    if current_session_id in web_logged_in_users:
        logging.info(f"Logging out Discord user linked to session {current_session_id}")
        del web_logged_in_users[current_session_id]
    if 'discord_user_id_for_web' in session:
        session.pop('discord_user_id_for_web', None)
    
    flash("ออกจากระบบ Discord แล้ว", "success")
    return redirect(url_for("index"))


# --- Spotify Authentication via Web (เชื่อมต่อ Spotify App ของผู้ใช้) ---
@app.route("/login/spotify/<int:discord_user_id_param>")
def login_spotify_web(discord_user_id_param: int):
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
    
    session['spotify_auth_discord_user_id'] = discord_user_id_param
    logging.info(f"Redirecting to Spotify for OAuth for Discord User ID: {discord_user_id_param}")
    return redirect(auth_url)

@app.route("/callback/spotify")
def spotify_callback():
    code = request.args.get("code")
    error = request.args.get("error")

    discord_user_id = session.pop('spotify_auth_discord_user_id', None)
    
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
        logging.error("Spotify callback: spotify_auth_discord_user_id not found in session. This usually means the session expired or is lost.")
        return redirect(url_for("index"))

    auth_manager = SpotifyOAuth(
        client_id=SPOTIPY_CLIENT_ID,
        client_secret=SPOTIPY_CLIENT_SECRET,
        redirect_uri=SPOTIPY_REDIRECT_URI,
        scope=SPOTIPY_SCOPES,
    )

    try:
        # ใช้ asyncio.to_thread เพื่อเรียก get_access_token ที่เป็น sync function
        # จากภายใน async context (bot.loop)
        # รอให้ bot_ready ก่อน (ถ้ายังไม่พร้อม)
        bot.loop.run_until_complete(bot_ready.wait())
        token_info = bot.loop.run_until_complete(asyncio.to_thread(auth_manager.get_access_token, code))
        
        # ต้องสร้าง auth_manager ใหม่ด้วย token_info ที่ได้มา
        auth_manager_with_token = SpotifyOAuth(
            client_id=SPOTIPY_CLIENT_ID,
            client_secret=SPOTIPY_CLIENT_SECRET,
            redirect_uri=SPOTIPY_REDIRECT_URI,
            scope=SPOTIPY_SCOPES,
            token_info=token_info # ส่ง token_info ที่ได้มาเข้าไป
        )
        sp_user = spotipy.Spotify(auth_manager=auth_manager_with_token)
        spotify_users[discord_user_id] = sp_user
        
        save_spotify_tokens()
        
        flash("✅ เชื่อมต่อ Spotify สำเร็จ!", "success")
        logging.info(f"User {discord_user_id} successfully linked Spotify.")
    except Exception as e:
        flash(f"❌ เกิดข้อผิดพลาดในการเชื่อมต่อ Spotify: {e}", "error")
        logging.error(f"Error during Spotify callback for user {discord_user_id}: {e}", exc_info=True)
    
    return redirect(url_for("index"))

# --- Flask Routes สำหรับควบคุม Spotify ---
@app.route("/control_spotify/<action>")
def control_spotify(action: str):
    current_session_id = session.get('session_id')
    discord_user_id = web_logged_in_users.get(current_session_id)
    
    if not discord_user_id:
        flash("❌ โปรดเข้าสู่ระบบ Discord บนหน้าเว็บก่อน", "error")
        return redirect(url_for("index"))
    
    # ต้องรอให้ bot_ready ก่อนเรียก get_user_spotify_client
    bot.loop.run_until_complete(bot_ready.wait())
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
            
            # รัน _play_spotify_from_web ใน bot.loop และรอผลลัพธ์
            response_message = bot.loop.run_until_complete(
                asyncio.run_coroutine_threadsafe(
                    _play_spotify_from_web(sp_user, query),
                    bot.loop
                ).result()
            )
            flash(response_message, "success")

        elif action == "pause":
            bot.loop.run_until_complete(asyncio.to_thread(sp_user.pause_playback))
            flash("⏸️ หยุดเพลง Spotify แล้ว", "success")
        elif action == "resume":
            bot.loop.run_until_complete(asyncio.to_thread(sp_user.start_playback))
            flash("▶️ เล่นเพลง Spotify ต่อแล้ว", "success")
        elif action == "skip":
            bot.loop.run_until_complete(asyncio.to_thread(sp_user.next_track))
            flash("⏭️ ข้ามเพลง Spotify แล้ว", "success")
        elif action == "previous":
            bot.loop.run_until_complete(asyncio.to_thread(sp_user.previous_track))
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
            
            # ต้องหา active device ใน thread pool
            devices = bot.loop.run_until_complete(asyncio.to_thread(sp_user.devices))
            active_device_id = None
            for device in devices['devices']:
                if device['is_active']:
                    active_device_id = device['id']
                    break
            
            if not active_device_id:
                flash("❌ ไม่พบ Spotify Client ที่กำลังทำงานอยู่", "error")
                return redirect(url_for("index"))

            bot.loop.run_until_complete(asyncio.to_thread(sp_user.volume, level, device_id=active_device_id))
            flash(f"🔊 ปรับระดับเสียง Spotify เป็น {level}% แล้ว", "success")
        elif action == "devices":
            # ต้องหา active device ใน thread pool
            devices = bot.loop.run_until_complete(asyncio.to_thread(sp_user.devices))
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
        elif e.http_status == 403 and "Player command failed" in str(e):
            flash("❌ การเล่นเพลงล้มเหลว โปรดตรวจสอบว่า Spotify App ของคุณเปิดอยู่และกำลังเล่นเพลง", "error")
        else:
            flash(f"❌ เกิดข้อผิดพลาด Spotify: {e}", "error")
        logging.error(f"Spotify web control error for user {discord_user_id} ({action}): {e}", exc_info=True)
    except Exception as e:
        flash(f"❌ เกิดข้อผิดพลาดที่ไม่คาดคิด: {e}", "error")
        logging.error(f"Web control error for user {discord_user_id} ({action}): {e}", exc_info=True)
    
    return redirect(url_for("index"))

# Helper function สำหรับ _play_spotify_from_web
async def _play_spotify_from_web(sp_user, query):
    """
    Handles playing Spotify content from the web interface.
    This function should be run within the bot's event loop.
    """
    track_uris = []
    response_msg = "🎶"
    context_uri = None

    try:
        if "spotify.com/track/" in query:
            track_id = query.split('/')[-1].split('?')[0]
            track_uri = f"spotify:track:{track_id}"
            track = await asyncio.to_thread(sp_user.track, track_uri)
            track_uris.append(track_uri)
            response_msg += f" กำลังเล่นเพลง: **{track['name']}** โดย **{track['artists'][0]['name']}**"
        elif "spotify.com/playlist/" in query:
            playlist_id = query.split('/')[-1].split('?')[0]
            context_uri = f"spotify:playlist:{playlist_id}"
            playlist = await asyncio.to_thread(sp_user.playlist, playlist_id)
            response_msg += f" กำลังเล่น Spotify Playlist: **{playlist['name']}**"
        elif "spotify.com/album/" in query:
            album_id = query.split('/')[-1].split('?')[0]
            context_uri = f"spotify:album:{album_id}"
            album = await asyncio.to_thread(sp_user.album, album_id)
            response_msg += f" กำลังเล่น Spotify Album: **{album['name']}**"
        else: # Search by name
            results = await asyncio.to_thread(sp_user.search, q=query, type='track', limit=1)
            if not results['tracks']['items']:
                return "❌ ไม่พบเพลงนี้บน Spotify"
            track = results['tracks']['items'][0]
            track_uris.append(track['uri'])
            response_msg += f" กำลังเล่นเพลง: **{track['name']}** โดย **{track['artists'][0]['name']}**"
        
        if not track_uris and not context_uri:
            return "❌ ไม่พบเพลงที่ต้องการเล่น หรือลิงก์ไม่ถูกต้อง"

        devices = await asyncio.to_thread(sp_user.devices)
        active_device_id = None
        for device in devices['devices']:
            if device['is_active']:
                active_device_id = device['id']
                break
        
        if not active_device_id:
            return "❌ ไม่พบ Spotify Client ที่กำลังทำงานอยู่ กรุณาเปิด Spotify App ของคุณ"

        if context_uri:
            await asyncio.to_thread(sp_user.start_playback, device_id=active_device_id, context_uri=context_uri)
        else:
            await asyncio.to_thread(sp_user.start_playback, device_id=active_device_id, uris=track_uris)
        
        return response_msg

    except spotipy.exceptions.SpotifyException as e:
        if e.http_status == 401:
            return "❌ Spotify Token หมดอายุหรือมีปัญหา โปรดเชื่อมต่อใหม่"
        elif e.http_status == 404 and "Device not found" in str(e):
            return "❌ ไม่พบ Spotify Client ที่กำลังทำงานอยู่ โปรดเปิด Spotify App ของคุณ"
        elif e.http_status == 403 and "Player command failed" in str(e):
            return "❌ การเล่นเพลงล้มเหลว โปรดตรวจสอบว่า Spotify App ของคุณเปิดอยู่และกำลังเล่นเพลง"
        else:
            return f"❌ เกิดข้อผิดพลาดในการเล่นเพลง Spotify: {e}"
    except Exception as e:
        logging.error(f"Error in _play_spotify_from_web: {e}", exc_info=True)
        return f"❌ เกิดข้อผิดพลาดที่ไม่คาดคิด: {e}"


# --- Run Flask + Discord bot ---
def run_web():
    # กำหนดให้ Flask ใช้ event loop เดียวกับ bot เพื่อจัดการ async/sync calls
    # นี่อาจเป็นวิธีที่ซับซ้อนกว่า แต่ช่วยให้การสื่อสารระหว่าง Flask และ bot ทำงานได้ดีขึ้น
    # หรือใช้ threading.Thread(target=lambda: app.run(...)) และจัดการ asyncio.to_thread
    # ใน Flask routes ที่เรียกใช้ bot.loop
    
    # วิธีที่ง่ายที่สุดสำหรับ dev คือรัน Flask ใน thread แยก (เช่นเดิม) และใช้ asyncio.to_thread / run_coroutine_threadsafe
    # ซึ่งเรากำลังทำอยู่
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False) # ปิด debug ใน production

if __name__ == "__main__":
    # ตรวจสอบว่า FLASK_SECRET_KEY ถูกตั้งค่าหรือไม่
    if not os.getenv("FLASK_SECRET_KEY"):
        logging.warning("FLASK_SECRET_KEY is not set in .env. Using a random key for development. Set it for production!")
        app.secret_key = os.urandom(24) # Ensure it's set if not in .env

    # ตรวจสอบว่า ffmpeg และ opus ถูกโหลดหรือไม่
    print("\n--- Initializing Bot & Web Server ---")
    print("Please ensure FFmpeg and Opus are installed and accessible in your system's PATH.")
    print("For Debian/Ubuntu: sudo apt install ffmpeg libopus0")
    print("For Windows: Download ffmpeg.exe and opus.dll, place in script directory or system PATH.")
    print("---------------------------------------\n")

    web_thread = threading.Thread(target=run_web)
    web_thread.start()
    bot.run(DISCORD_TOKEN)