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
spotify_users = {}
web_logged_in_users = {} # Key: Flask Session ID, Value: Discord User ID
voice_client = None
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
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# เพิ่ม flag เพื่อตรวจสอบว่าบอทพร้อมใช้งานหรือไม่
bot_ready = asyncio.Event()

# --- ตั้งค่า Flask App ---
app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = os.getenv("FLASK_SECRET_KEY")

# --- Discord Bot Events ---
@bot.event
async def on_ready():
    global bot_ready
    if not discord.opus.is_loaded():
        try:
            # ตรวจสอบว่า libopus.so อยู่ใน PATH หรือระบุ path ที่ถูกต้อง
            discord.opus.load_opus('libopus.so')
            logging.info("Opus loaded successfully.")
        except Exception as e:
            logging.error(f"Failed to load opus: {e}. Please ensure libopus.so is in your PATH or specified correctly.")
            print(f"Failed to load opus: {e}. Please ensure libopus.so is in your PATH or specified correctly.")

    print(f"✅ Bot logged in as {bot.user}")
    logging.info(f"Bot logged in as {bot.user}")

    await tree.sync()
    logging.info("Global commands synced.")

    guild_obj = discord.Object(id=YOUR_GUILD_ID)
    await tree.sync(guild=guild_obj)
    logging.info(f"Commands synced to guild: {YOUR_GUILD_ID}")

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
            auth_manager.token_info = token_info
            sp_user = spotipy.Spotify(auth_manager=auth_manager)
            try:
                sp_user.current_user() 
                spotify_users[int(user_id)] = sp_user
                logging.info(f"Loaded and verified Spotify token for user ID: {user_id}")
            except spotipy.exceptions.SpotifyException as e:
                logging.warning(f"Spotify token for user {user_id} expired or invalid on startup: {e}")
    except FileNotFoundError:
        logging.info("No saved Spotify tokens found.")
    except json.JSONDecodeError:
        logging.error("Error decoding spotify_tokens.json. File might be corrupted.")
    except Exception as e:
        logging.error(f"Error loading Spotify tokens: {e}")
    
    bot_ready.set() # ตั้งค่า Event เพื่อบอกว่าบอทพร้อมแล้ว

# --- Helper Function: Get Spotify Client for User ---
def get_user_spotify_client(discord_user_id: int):
    sp_client = spotify_users.get(discord_user_id)
    if sp_client:
        try:
            sp_client.current_user() 
            return sp_client
        except spotipy.exceptions.SpotifyException as e:
            logging.warning(f"Spotify token expired or invalid for user {discord_user_id}: {e}")
            if discord_user_id in spotify_users:
                del spotify_users[discord_user_id]
            return None
    return None

# --- Helper Function: Save Spotify Tokens ---
def save_spotify_tokens():
    try:
        tokens_data = {}
        for user_id, sp_client in spotify_users.items():
            tokens_data[str(user_id)] = sp_client.auth_manager.get_cached_token()
        
        with open("spotify_tokens.json", "w") as f:
            json.dump(tokens_data, f, indent=4)
        logging.info("Saved Spotify tokens to file.")
    except Exception as e:
        logging.error(f"Error saving Spotify tokens: {e}")


# --- Discord Slash Commands (ไม่มีการเปลี่ยนแปลงในส่วนนี้) ---

@tree.command(name="link_spotify", description="เชื่อมต่อบัญชี Spotify ของคุณ")
async def link_spotify(interaction: discord.Interaction):
    if interaction.user.id not in web_logged_in_users.values():
        await interaction.response.send_message(
            "❌ กรุณาเข้าสู่ระบบ Discord บนหน้าเว็บก่อน เพื่อให้บอทรู้ว่าคุณคือใคร:\n"
            f"คลิกที่ลิงก์นี้: **{url_for('login_discord', _external=True)}**", 
            ephemeral=True
        )
        return

    auth_manager = SpotifyOAuth(
        client_id=SPOTIPY_CLIENT_ID,
        client_secret=SPOTIPY_CLIENT_SECRET,
        redirect_uri=SPOTIPY_REDIRECT_URI,
        scope=SPOTIPY_SCOPES,
        show_dialog=True
    )
    auth_url = auth_manager.get_authorize_url()
    
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
            "❌ คุณยังไม่ได้เชื่อมต่อ Spotify โปรดใช้ `/link_spotify` ก่อน", 
            ephemeral=True
        )
        return

    await interaction.response.defer()

    try:
        track_uris = []
        response_msg = "🎶"

        # แก้ไข URL Spotify ให้ถูกต้อง
        if "https://open.spotify.com/track/" in query:
            track = sp_user.track(query)
            track_uris.append(track['uri'])
            response_msg += f" กำลังเล่นเพลง: **{track['name']}** โดย **{track['artists'][0]['name']}**"
            logging.info(f"Playing Spotify track URI: {track['uri']}")
        elif "https://open.spotify.com/playlist/" in query:
            playlist_id = query.split('/')[-1].split('?')[0]
            results = sp_user.playlist_items(playlist_id)
            for item in results['items']:
                if item['track'] and item['track']['type'] == 'track':
                    track_uris.append(item['track']['uri'])
            response_msg += f" กำลังเล่นเพลงจาก Spotify Playlist (พบ {len(track_uris)} เพลง)"
            logging.info(f"Playing Spotify playlist with {len(track_uris)} tracks.")
        elif "https://open.spotify.com/album/" in query:
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

        devices = sp_user.devices()
        active_device_id = None
        for device in devices['devices']:
            if device['is_active']:
                active_device_id = device['id']
                break
        
        if not active_device_id:
            await interaction.followup.send("❌ ไม่พบ Spotify Client ที่กำลังทำงานอยู่ กรุณาเปิด Spotify App ของคุณ")
            return

        if "playlist" in query or "album" in query:
            # ใช้ context_uri สำหรับ Playlist/Album
            if "playlist" in query:
                sp_user.start_playback(device_id=active_device_id, context_uri=f"spotify:playlist:{playlist_id}")
            elif "album" in query:
                sp_user.start_playback(device_id=active_device_id, context_uri=f"spotify:album:{album_id}")
        else:
            # ใช้ uris สำหรับ Single Track
            sp_user.start_playback(device_id=active_device_id, uris=track_uris)
        
        await interaction.followup.send(response_msg)

    except spotipy.exceptions.SpotifyException as e:
        if e.http_status == 401:
            await interaction.followup.send("❌ Spotify Token หมดอายุหรือมีปัญหา โปรดใช้ `/link_spotify` อีกครั้ง")
            logging.error(f"Spotify token issue for user {interaction.user.id}: {e}")
            if interaction.user.id in spotify_users:
                del spotify_users[interaction.user.id]
            save_spotify_tokens()
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
        sp_user.start_playback()
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
    
    await interaction.response.defer()
    try:
        tts = gTTS(message, lang='th')
        tts_filename = f"tts_discord_{interaction.id}.mp3"
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
    if os.path.exists(filename):
        os.remove(filename)
    if vc and not vc.is_playing() and vc.is_paused():
        vc.resume()

async def cleanup_audio(error, filename):
    if error:
        logging.error(f"TTS playback error: {error}")
    if os.path.exists(filename):
        os.remove(filename)


@tree.command(name="wake", description="ปลุกผู้ใช้ด้วย DM")
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
    current_session_id = session.get('session_id')
    # If there's no session_id, create one for new users
    if not current_session_id:
        current_session_id = os.urandom(16).hex()
        session['session_id'] = current_session_id
        logging.info(f"New Flask session created: {current_session_id}")

    discord_user_id = web_logged_in_users.get(current_session_id)
    
    is_discord_linked = False
    if discord_user_id:
        is_discord_linked = True

    is_spotify_linked = False
    if discord_user_id:
        # ใช้ asyncio.run_coroutine_threadsafe เพื่อรัน async function ใน bot's loop
        # ต้องรอให้บอทพร้อมก่อน
        bot.loop.run_until_complete(bot_ready.wait())
        is_spotify_linked = asyncio.run_coroutine_threadsafe(
            _check_spotify_link_status(discord_user_id),
            bot.loop
        ).result() # ใช้ .result() เพื่อรอผลลัพธ์ใน synchronous context

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
    # session['auth_session_id'] = os.urandom(16).hex() # ไม่จำเป็นต้องใช้ auth_session_id แยก
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
        logging.error("Spotify callback: spotify_auth_discord_user_id not found in session.")
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
        token_info = bot.loop.run_until_complete(asyncio.to_thread(auth_manager.get_access_token, code))
        
        auth_manager_with_token = SpotifyOAuth(
            client_id=SPOTIPY_CLIENT_ID,
            client_secret=SPOTIPY_CLIENT_SECRET,
            redirect_uri=SPOTIPY_REDIRECT_URI,
            scope=SPOTIPY_SCOPES,
            token_info=token_info
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
    
    sp_user = get_user_spotify_client(discord_user_id)
    if not sp_user:
        flash("❌ โปรดเชื่อมต่อบัญชี Spotify ของคุณก่อน", "error")
        return redirect(url_for("index"))

    try:
        response_message = ""
        # รัน Spotify API calls ใน thread pool ของ bot.loop
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
        else:
            flash(f"❌ เกิดข้อผิดพลาด Spotify: {e}", "error")
        logging.error(f"Spotify web control error for user {discord_user_id} ({action}): {e}", exc_info=True)
    except Exception as e:
        flash(f"❌ เกิดข้อผิดพลาดที่ไม่คาดคิด: {e}", "error")
        logging.error(f"Web control error for user {discord_user_id} ({action}): {e}", exc_info=True)
    
    return redirect(url_for("index"))

# Helper function สำหรับ _play_spotify_from_web
async def _play_spotify_from_web(sp_user, query):
    track_uris = []
    response_msg = ""
    
    # แก้ไข URL Spotify ให้ถูกต้อง
    if "https://open.spotify.com/track/" in query:
        track = await asyncio.to_thread(sp_user.track, query)
        track_uris.append(track['uri'])
        response_msg = f"🎶 กำลังเล่นเพลง: **{track['name']}** โดย **{track['artists'][0]['name']}**"
    elif "https://open.spotify.com/playlist/" in query:
        playlist_id = query.split('/')[-1].split('?')[0]
        results = await asyncio.to_thread(sp_user.playlist_items, playlist_id)
        for item in results['items']:
            if item['track'] and item['track']['type'] == 'track':
                track_uris.append(item['track']['uri'])
        response_msg = f"🎶 กำลังเล่นเพลงจาก Spotify Playlist (พบ {len(track_uris)} เพลง)"
    elif "https://open.spotify.com/album/" in query:
        album_id = query.split('/')[-1].split('?')[0]
        results = await asyncio.to_thread(sp_user.album_tracks, album_id)
        for item in results['items']:
            track_uris.append(item['uri'])
        response_msg = f"🎶 กำลังเล่นเพลงจาก Spotify Album (พบ {len(track_uris)} เพลง)"
    else: # ค้นหาด้วยชื่อเพลง
        results = await asyncio.to_thread(sp_user.search, q=query, type='track', limit=1)
        if not results['tracks']['items']:
            return "❌ ไม่พบเพลงนี้บน Spotify"
        track = results['tracks']['items'][0]
        track_uris.append(track['uri'])
        response_msg = f"🎶 กำลังเล่นเพลง: **{track['name']}** โดย **{track['artists'][0]['name']}**"

    if not track_uris:
        return "❌ ไม่พบเพลงที่ต้องการเล่น หรือลิงก์ไม่ถูกต้อง"

    devices = await asyncio.to_thread(sp_user.devices)
    active_device_id = None
    for device in devices['devices']:
        if device['is_active']:
            active_device_id = device['id']
            break
    
    if not active_device_id:
        return "❌ ไม่พบ Spotify Client ที่กำลังทำงานอยู่ กรุณาเปิด Spotify App ของคุณ"

    if "playlist" in query or "album" in query:
        if "playlist" in query:
            await asyncio.to_thread(sp_user.start_playback, device_id=active_device_id, context_uri=f"spotify:playlist:{playlist_id}")
        elif "album" in query:
            await asyncio.to_thread(sp_user.start_playback, device_id=active_device_id, context_uri=f"spotify:album:{album_id}")
    else:
        await asyncio.to_thread(sp_user.start_playback, device_id=active_device_id, uris=track_uris)
    
    return response_msg

# --- Running Bot and Flask ---
def run_flask():
    # Gunicorn จะเป็นตัวจัดการรัน Flask app บน Railway
    # ไม่ต้องเรียก app.run() ตรงๆ ในโค้ดนี้เมื่อใช้ Gunicorn
    pass 

if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.start()
    bot.run(DISCORD_TOKEN)