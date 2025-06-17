import discord
from discord.ext import commands
from discord import app_commands
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, send_from_directory
from gtts import gTTS
import os
import threading
import logging
import json
import base64
from dotenv import load_dotenv
import spotipy
from spotipy.oauth2 import SpotifyOAuth
import httpx
import asyncio
import yt_dlp
import random

# For CORS (if frontend and backend are on different ports/domains during development)
from flask_cors import CORS

# Firestore imports
import firebase_admin
from firebase_admin import credentials, firestore
from firebase_admin import exceptions as firebase_exceptions

# ปิดข้อความรายงานบั๊กของ yt_dlp เพื่อไม่ให้แสดงในคอนโซล
yt_dlp.utils.bug_reports_message = lambda: ''

# --- โหลดตัวแปรสภาพแวดล้อม ---
load_dotenv()

# --- ข้อมูลประจำตัว Spotify API ---
SPOTIPY_CLIENT_ID = os.getenv("SPOTIPY_CLIENT_ID")
SPOTIPY_CLIENT_SECRET = os.getenv("SPOTIPY_CLIENT_SECRET")
SPOTIPY_REDIRECT_URI = os.getenv("SPOTIPY_REDIRECT_URI")
SPOTIPY_SCOPES = "user-read-playback-state user-modify-playback-state user-read-currently-playing playlist-read-private playlist-read-collaborative user-library-read"

# --- ข้อมูลประจำตัว Discord Bot ---
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
# ตรวจสอบ GUILD_ID ให้แน่ใจว่าเป็นตัวเลขเท่านั้นและไม่มีช่องว่างหรือ #
try:
    YOUR_GUILD_ID = int(os.environ["GUILD_ID"].strip().split('#')[0])
except (KeyError, ValueError):
    logging.error("GUILD_ID environment variable is missing or invalid. Please set it to your Discord server ID.")
    YOUR_GUILD_ID = None # ตั้งค่าเป็น None หรือ ID ทดสอบหากไม่มี

DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
DISCORD_REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI")
DISCORD_OAUTH_SCOPES = "identify guilds"

# --- Firebase Setup ---
firebase_credentials_base64 = os.getenv("FIREBASE_CREDENTIALS_BASE64")
db = None
if not firebase_credentials_base64:
    logging.error("FIREBASE_CREDENTIALS_BASE64 environment variable not set. Firestore will not work.")
else:
    try:
        decoded_credentials = base64.b64decode(firebase_credentials_base64).decode('utf-8')
        cred = credentials.Certificate(json.loads(decoded_credentials))
        firebase_admin.initialize_app(cred)
        db = firestore.client()
        logging.info("Firebase Admin SDK initialized successfully.")
    except Exception as e:
        logging.error(f"ข้อผิดพลาดในการเริ่มต้น Firebase Admin SDK: {e}", exc_info=True)
        db = None

# --- ตัวแปร Global ---
spotify_users = {}  # Key: Discord User ID, Value: Spotify client object
web_logged_in_users = {}  # Key: Flask Session ID, Value: Discord User ID
voice_client = None
queue = []  # คิวเพลงสำหรับเล่น (รองรับ YouTube/SoundCloud URL)
current_playing_youtube_info = {} # Stores {'title', 'duration', 'thumbnail'} for YouTube/SoundCloud playback
# PRD ระบุว่าคิวควรคงอยู่ แต่ในเวอร์ชันพื้นฐานนี้ เราจะเก็บคิวในหน่วยความจำก่อน
# การทำให้คิวคงอยู่ข้ามการรีสตาร์ทบอทจะต้องใช้การจัดเก็บใน Firestore เพิ่มเติม
volume = 1.0 # ระดับเสียงเริ่มต้น (ไม่ตรงกับ UI ในรูป แต่เป็นค่าเริ่มต้นของบอท)

# --- ตัวแปร Global สำหรับระบบโพลล์ (ตาม PRD) ---
active_polls = {}

# --- ตั้งค่าการบันทึก Log ---
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
intents.voice_states = True
intents.members = True # ต้องเปิดใช้งาน Privileged Intent ใน Discord Developer Portal ด้วย!

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree
bot_ready = asyncio.Event()

# --- ตั้งค่า Flask App ---
# กำหนดเส้นทางไปยังโฟลเดอร์ build ของ React frontend
REACT_BUILD_DIR = os.path.join(os.path.dirname(__file__), 'frontend', 'dist')
# ตรวจสอบว่าโฟลเดอร์ dist มีอยู่หรือไม่
if not os.path.exists(REACT_BUILD_DIR):
    logging.warning(f"React build directory '{REACT_BUILD_DIR}' does not exist. Ensure you have built the React frontend (npm run build).")

app = Flask(__name__, static_folder=REACT_BUILD_DIR, template_folder="templates")
app.secret_key = os.getenv("FLASK_SECRET_KEY") or os.urandom(24) 

# CORS setup for development (if frontend is served from a different port)
# In production, this might be handled by Nginx/Apache.
CORS(app) # Allow all origins for simplicity in development

# --- ฟังก์ชันช่วยสำหรับ Firestore Persistence (ตาม PRD) ---
async def update_user_data_in_firestore(discord_user_id: int, spotify_token_info: dict = None, flask_session_to_add: str = None, flask_session_to_remove: str = None):
    """
    อัปเดตข้อมูลผู้ใช้ใน Firestore รวมถึงโทเค็น Spotify และเซสชัน Flask
    """
    if db is None:
        logging.error("Firestore DB ไม่ได้ถูกเริ่มต้น ไม่สามารถอัปเดตข้อมูลผู้ใช้ได้.")
        return

    user_ref = db.collection('users').document(str(discord_user_id))
    user_data_to_update = {}

    try:
        # ใช้ run_in_executor สำหรับการเรียก Firestore API ที่อาจบล็อก
        doc = await asyncio.to_thread(user_ref.get)
        current_data = doc.to_dict() if doc.exists else {}
    except firebase_exceptions.FirebaseError as e:
        logging.error(f"ข้อผิดพลาดในการดึงข้อมูลผู้ใช้ {discord_user_id} จาก Firestore: {e}", exc_info=True)
        return

    if spotify_token_info:
        # หาก spotify_token_info เป็น firestore.DELETE_FIELD ให้ลบ field
        if spotify_token_info == firestore.DELETE_FIELD:
            user_data_to_update['spotify_token_info'] = firestore.DELETE_FIELD
        else:
            user_data_to_update['spotify_token_info'] = spotify_token_info
    
    flask_sessions = set(current_data.get('flask_sessions', []))

    if flask_session_to_add:
        flask_sessions.add(flask_session_to_add)
    if flask_session_to_remove and flask_session_to_remove in flask_sessions:
        flask_sessions.remove(flask_session_to_remove)

    user_data_to_update['flask_sessions'] = list(flask_sessions)

    if user_data_to_update:
        try:
            await asyncio.to_thread(user_ref.set, user_data_to_update, merge=True)
            logging.info(f"ข้อมูลผู้ใช้ {discord_user_id} อัปเดตใน Firestore แล้ว.")
        except firebase_exceptions.FirebaseError as e:
            logging.error(f"ข้อผิดพลาดในการอัปเดตข้อมูลผู้ใช้ {discord_user_id} ใน Firestore: {e}", exc_info=True)

async def load_all_user_data_from_firestore():
    """
    โหลดข้อมูลผู้ใช้ทั้งหมด (โทเค็น Spotify, เซสชัน Flask) จาก Firestore เข้าสู่ตัวแปร global.
    """
    global spotify_users, web_logged_in_users

    if db is None:
        logging.warning("Firestore DB ไม่ได้ถูกเริ่มต้น ไม่สามารถโหลดข้อมูลผู้ใช้ได้.")
        return

    try:
        users_ref = db.collection('users')
        docs = await asyncio.to_thread(users_ref.get) # ใช้ asyncio.to_thread

        for doc in docs:
            user_id = int(doc.id)
            data = doc.to_dict()
            
            token_info = data.get('spotify_token_info')
            if token_info:
                auth_manager = SpotifyOAuth(
                    client_id=SPOTIPY_CLIENT_ID,
                    client_secret=SPOTIPY_CLIENT_SECRET,
                    redirect_uri=SPOTIPY_REDIRECT_URI,
                    scope=SPOTIPY_SCOPES,
                )
                auth_manager.set_cached_token(token_info)
                sp_user = spotipy.Spotify(auth_manager=auth_manager)
                try:
                    await asyncio.to_thread(sp_user.current_user) # Validate token
                    spotify_users[user_id] = sp_user
                    logging.info(f"โหลดโทเค็น Spotify ที่ถูกต้องสำหรับผู้ใช้ ID: {user_id} จาก Firestore แล้ว")
                except spotipy.exceptions.SpotifyException as e:
                    logging.warning(f"โทเค็น Spotify สำหรับผู้ใช้ {user_id} หมดอายุเมื่อเริ่มต้น (Firestore): {e}. ลบออกจากแคชในเครื่องและ Firestore.")
                    # ลบข้อมูลโทเค็น Spotify ที่หมดอายุออกจาก Firestore
                    await update_user_data_in_firestore(user_id, spotify_token_info=firestore.DELETE_FIELD)
                    if user_id in spotify_users:
                        del spotify_users[user_id]
                except Exception as e:
                    logging.error(f"ข้อผิดพลาดในการตรวจสอบโทเค็น Spotify ที่โหลดสำหรับผู้ใช้ {user_id}: {e}", exc_info=True)

            flask_sessions_list = data.get('flask_sessions', [])
            for session_id in flask_sessions_list:
                web_logged_in_users[session_id] = user_id
            
        logging.info("โหลดข้อมูลผู้ใช้ทั้งหมด (โทเค็น Spotify และเซสชัน Flask) จาก Firestore แล้ว.")
    except firebase_exceptions.FirebaseError as e:
        logging.error(f"ข้อผิดพลาดในการโหลดข้อมูลผู้ใช้ทั้งหมดจาก Firestore: {e}", exc_info=True)
    except Exception as e:
        logging.error(f"ข้อผิดพลาดที่ไม่คาดคิดในการโหลดข้อมูลผู้ใช้จาก Firestore: {e}", exc_info=True)


def get_user_spotify_client(discord_user_id: int):
    """
    ดึง Spotify client สำหรับผู้ใช้ Discord
    ฟังก์ชันนี้เพียงดึงจากแคช ไม่ได้ตรวจสอบความถูกต้องของโทเค็น
    การตรวจสอบโทเค็นควรทำใน async context โดยใช้ _check_spotify_link_status
    """
    return spotify_users.get(discord_user_id)

async def _check_spotify_link_status(discord_user_id: int) -> bool:
    """
    ตรวจสอบสถานะการเชื่อมโยง Spotify ของผู้ใช้โดยการตรวจสอบความถูกต้องของโทเค็น
    """
    await bot_ready.wait() # รอจนกว่า bot loop จะทำงาน
    sp_client = spotify_users.get(discord_user_id)
    if sp_client:
        try:
            await asyncio.to_thread(sp_client.current_user) # ตรวจสอบ Token แบบ async
            return True
        except spotipy.exceptions.SpotifyException as e:
            logging.warning(f"โทเค็น Spotify หมดอายุสำหรับผู้ใช้ {discord_user_id}: {e}")
            if discord_user_id in spotify_users:
                del spotify_users[discord_user_id]
                await update_user_data_in_firestore(discord_user_id, spotify_token_info=firestore.DELETE_FIELD)
            return False
        except Exception as e:
            logging.error(f"ข้อผิดพลาดที่ไม่คาดคิดระหว่างการตรวจสอบโทเค็น Spotify สำหรับผู้ใช้ {discord_user_id}: {e}", exc_info=True)
            return False
    return False

async def _fetch_discord_token_and_user(code: str):
    """
    แลกเปลี่ยนรหัสอนุญาต Discord เพื่อรับโทเค็นและข้อมูลผู้ใช้
    """
    async with httpx.AsyncClient() as client:
        token_response = await client.post(
            "https://discord.com/api/oauth2/token",
            data={
                "client_id": DISCORD_CLIENT_ID,
                "client_secret": DISCORD_CLIENT_SECRET,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": DISCORD_REDIRECT_URI,
                "scope": DISCORD_OAUTH_SCOPES
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        token_response.raise_for_status()
        token_info = token_response.json()

        user_response = await client.get(
            "https://discord.com/api/users/@me",
            headers={
                "Authorization": f"Bearer {token_info['access_token']}"
            }
        )
        user_response.raise_for_status()
        user_data = user_response.json()
        return token_info, user_data

async def _after_playback_cleanup(error, channel_id):
    global current_playing_youtube_info
    if error:
        logging.error(f"ข้อผิดพลาดในการเล่นเสียง: {error}")
        channel = bot.get_channel(channel_id)
        if channel:
            await channel.send(f"❌ เกิดข้อผิดพลาดระหว่างเล่น: {error}")
    
    # ล้างข้อมูลเพลงที่กำลังเล่นอยู่
    current_playing_youtube_info = {}

    # ตรวจสอบว่าบอทยังคงอยู่ในช่องเสียงและไม่มีการเล่นอยู่ก่อนที่จะเรียก _play_next_in_queue
    if voice_client and voice_client.is_connected() and not voice_client.is_playing() and queue:
        channel = bot.get_channel(channel_id)
        if channel:
            await _play_next_in_queue(channel)
    elif not queue and voice_client and voice_client.is_connected() and not voice_client.is_playing():
        logging.info("คิวเพลงเล่นเสร็จสิ้น.")
        channel = bot.get_channel(channel_id)
        if channel:
            await channel.send("✅ เล่นเพลงในคิวทั้งหมดแล้ว!")


async def _play_next_in_queue(channel: discord.VoiceChannel):
    """เล่นเพลงถัดไปในคิว รองรับ URL ของ YouTube/SoundCloud"""
    global voice_client, queue, volume, current_playing_youtube_info

    if not voice_client or not voice_client.is_connected():
        logging.warning("บอทไม่ได้อยู่ในช่องเสียงเพื่อเล่นเพลงในคิว.")
        current_playing_youtube_info = {} # Clear info if bot is not in voice channel
        return

    if voice_client.is_playing():
        voice_client.stop()

    if not queue:
        logging.info("คิวเพลงว่างเปล่า.")
        await channel.send("✅ เล่นเพลงในคิวทั้งหมดแล้ว!")
        current_playing_youtube_info = {} # Clear info when queue is empty
        return

    url_to_play = queue.pop(0)
    logging.info(f"พยายามเล่นจากคิว: {url_to_play}")
    
    ydl_opts = {
        'format': 'bestaudio/best',
        'default_search': 'ytsearch', # สำหรับกรณีที่ผู้ใช้ใส่แค่ชื่อเพลง
        'source_address': '0.0.0.0',
        'verbose': False,
        'extract_flat': True # เพื่อดึงข้อมูลเพลย์ลิสต์ได้เร็วขึ้น
    }

    try:
        loop = asyncio.get_event_loop() # ใช้ loop ของเธรดที่เรียก
        info = await loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(ydl_opts).extract_info(url_to_play, download=False))
        
        audio_url = None
        title = 'Unknown Title'
        duration = 0
        thumbnail = "https://placehold.co/400x400/FF0000/FFFFFF?text=YouTube"

        if info.get('_type') == 'playlist':
            playlist_title = info.get('title', 'Unknown Playlist')
            await channel.send(f"🎶 เพิ่มเพลย์ลิสต์: **{playlist_title}** ลงในคิว...")
            
            # เพิ่มวิดีโอทั้งหมดในเพลย์ลิสต์ลงในคิว
            # เนื่องจาก extract_flat=True, info.get('entries') จะมีแค่ id และ url ของแต่ละวิดีโอ
            for entry in info.get('entries', []):
                if entry and entry.get('url'):
                    queue.append(entry['url'])
            
            # เล่นวิดีโอแรกสุดของเพลย์ลิสต์ (ซึ่งถูกเพิ่มไปเป็นตัวแรกสุดในคิว)
            if queue:
                # ดึง URL สำหรับวิดีโอที่จะเล่นถัดไปจากคิว (ซึ่งตอนนี้คือวิดีโอแรกของเพลย์ลิสต์)
                first_video_url = queue.pop(0)
                # ดึงข้อมูลจริงของวิดีโอแรกสุดอีกครั้งโดยไม่ต้อง extract_flat
                single_video_ydl_opts = {
                    'format': 'bestaudio/best',
                    'source_address': '0.0.0.0',
                    'verbose': False,
                }
                first_video_info = await loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(single_video_ydl_opts).extract_info(first_video_url, download=False))
                audio_url = first_video_info['url']
                title = first_video_info.get('title', 'Unknown Title')
                duration = first_video_info.get('duration', 0)
                thumbnail = first_video_info.get('thumbnail', thumbnail)
            else:
                raise Exception("ไม่สามารถดึงวิดีโอจากเพลย์ลิสต์ได้.")
            
        elif info.get('url'): # วิดีโอเดี่ยว
            audio_url = info['url']
            title = info.get('title', 'Unknown Title')
            duration = info.get('duration', 0)
            thumbnail = info.get('thumbnail', thumbnail)
        else:
            raise Exception("ไม่พบ URL เสียงที่สามารถเล่นได้.")
        
        current_playing_youtube_info = {
            'title': title,
            'duration': duration, # in seconds
            'thumbnail': thumbnail
        }
        
        source = discord.FFmpegPCMAudio(audio_url, executable="ffmpeg")
        
        # ใช้ bot.loop เพื่อรัน callback ในเธรดหลักของบอท
        voice_client.play(source, after=lambda e: asyncio.run_coroutine_threadsafe(
            _after_playback_cleanup(e, channel.id), bot.loop))
        
        await channel.send(f"🎶 กำลังเล่น YouTube: **{title}**")

    except yt_dlp.utils.ExtractorError as e:
        error_message = str(e)
        if "Sign in to confirm you’re not a bot" in error_message or "requires login" in error_message or "age-restricted" in error_message or "unavailable in your country" in error_message:
            await channel.send(
                f"❌ ไม่สามารถเล่น **{url_to_play}** ได้: วิดีโอนี้อาจต้องเข้าสู่ระบบ, ถูกจำกัดอายุ, หรือไม่พร้อมใช้งานในภูมิภาคของคุณ โปรดลองใช้วิดีโอสาธารณะอื่น."
            )
            logging.warning(f"วิดีโอ YouTube ถูกจำกัด: {url_to_play}")
        else:
            await channel.send(f"❌ เกิดข้อผิดพลาดที่ไม่คาดคิดในการเล่น YouTube สำหรับ {url_to_play}: {e}")
            logging.error(f"ข้อผิดพลาดในการเล่นรายการ YouTube {url_to_play}: {e}", exc_info=True)
        
        current_playing_youtube_info = {} # Clear info on error
        # พยายามเล่นเพลงถัดไปหลังจากเกิดข้อผิดพลาด
        if queue and voice_client and voice_client.is_connected():
            await _play_next_in_queue(channel)
        elif not queue:
            await channel.send("✅ เล่นเพลงในคิวทั้งหมดแล้ว!")
    except Exception as e:
        logging.error(f"ข้อผิดพลาดในการเล่นรายการ YouTube {url_to_play}: {e}", exc_info=True)
        await channel.send(f"❌ ไม่สามารถเล่นวิดีโอ YouTube: {url_to_play} ได้ เกิดข้อผิดพลาด: {e}")
        
        current_playing_youtube_info = {} # Clear info on error
        # พยายามเล่นเพลงถัดไปหลังจากเกิดข้อผิดพลาด
        if queue and voice_client and voice_client.is_connected():
            await _play_next_in_queue(channel)
        elif not queue:
            await channel.send("✅ เล่นเพลงในคิวทั้งหมดแล้ว!")


# --- Discord Bot Events ---
@bot.event
async def on_ready():
    # โหลด Opus สำหรับฟังก์ชันเสียง
    if not discord.opus.is_loaded():
        try:
            discord.opus.load_opus('libopus.so')
            logging.info("Opus โหลดสำเร็จ.")
        except Exception as e:
            logging.error(f"ไม่สามารถโหลด opus: {e} ได้ คำสั่งเสียงอาจไม่ทำงาน.")
            print(f"ไม่สามารถโหลด opus: {e} ได้ โปรดตรวจสอบให้แน่ใจว่าติดตั้งและเข้าถึงได้.")

    print(f"✅ บอทเข้าสู่ระบบในฐานะ {bot.user}")
    logging.info(f"บอทเข้าสู่ระบบในฐานะ {bot.user}")

    try:
        # ซิงค์คำสั่ง Global
        await tree.sync() 
        logging.info("Global commands synced.")
        
        # ซิงค์คำสั่งไปยัง Guild เฉพาะ (ถ้า GUILD_ID ถูกกำหนด)
        if YOUR_GUILD_ID:
            guild_obj = discord.Object(id=YOUR_GUILD_ID)
            await tree.sync(guild=guild_obj)
            logging.info(f"Commands synced to guild: {YOUR_GUILD_ID}")
        else:
            logging.warning("GUILD_ID ไม่ได้ถูกกำหนดไว้ ไม่ได้ซิงค์คำสั่งไปยัง Guild เฉพาะ")
    except Exception as e:
        logging.error(f"ไม่สามารถซิงค์คำสั่ง: {e} ได้", exc_info=True)

    # โหลด Spotify tokens และ Flask sessions จาก Firestore
    # เนื่องจาก on_ready เป็น async context อยู่แล้ว เราสามารถเรียก await ได้โดยตรง
    await load_all_user_data_from_firestore()
    
    bot_ready.set() # ตั้งค่า Event เพื่อให้ Flask ทราบว่าบอทพร้อมแล้ว
    logging.info("บอทพร้อมใช้งานเต็มที่แล้ว.")

# --- Discord Slash Commands (ตาม PRD) ---

@tree.command(name="join", description="เข้าร่วมช่องเสียงของคุณ")
async def join(interaction: discord.Interaction):
    global voice_client
    if interaction.user.voice:
        channel = interaction.user.voice.channel
        if voice_client and voice_client.is_connected():
            if voice_client.channel.id == channel.id:
                await interaction.response.send_message(f"✅ อยู่ใน **{channel.name}** แล้ว", ephemeral=True)
            else:
                await voice_client.move_to(channel)
                await interaction.response.send_message(f"✅ ย้ายไปยัง **{channel.name}** แล้ว", ephemeral=True)
        else:
            try:
                voice_client = await channel.connect()
                await interaction.response.send_message(f"✅ เข้าร่วม **{channel.name}** แล้ว", ephemeral=True)
            except discord.ClientException as e:
                logging.error(f"ไม่สามารถเชื่อมต่อช่องเสียง: {e} ได้")
                await interaction.response.send_message(f"❌ ไม่สามารถเชื่อมต่อช่องเสียง: {e} ได้", ephemeral=True)
            except Exception as e:
                logging.error(f"เกิดข้อผิดพลาดที่ไม่คาดคิดขณะเข้าร่วมช่องเสียง: {e}")
                await interaction.response.send_message(f"❌ เกิดข้อผิดพลาดที่ไม่คาดคิด: {e}", ephemeral=True)
    else:
        await interaction.response.send_message("❌ คุณไม่ได้อยู่ในช่องเสียง", ephemeral=True)

@tree.command(name="leave", description="ออกจากช่องเสียง")
async def leave(interaction: discord.Interaction):
    global voice_client, queue
    if voice_client and voice_client.is_connected():
        if voice_client.is_playing():
            voice_client.stop()
        queue.clear() # ล้างคิวเมื่อออกจากช่องเสียง
        await voice_client.disconnect()
        voice_client = None
        await interaction.response.send_message("✅ ออกจากช่องเสียงแล้ว และล้างคิวเพลง", ephemeral=True)
    else:
        await interaction.response.send_message("❌ ไม่ได้อยู่ในช่องเสียง", ephemeral=True)

@tree.command(name="link_spotify", description="เชื่อมโยงบัญชี Spotify ของคุณ")
async def link_spotify(interaction: discord.Interaction):
    # ส่ง base_url ไปให้ user เพื่อให้ลิงก์ทำงานในทุกสภาพแวดล้อม
    # ในกรณีนี้ Flask เป็นคนเสิร์ฟหน้า React App
    base_url = request.url_root if request else "YOUR_APP_BASE_URL_HERE" 
    await interaction.response.send_message(
        f"🔗 ในการเชื่อมโยงบัญชี Spotify ของคุณ โปรดไปที่:\n"
        f"**{base_url}**\n"
        f"เข้าสู่ระบบด้วย Discord ก่อน จากนั้นจึงเชื่อมต่อ Spotify", 
        ephemeral=True
    )

@tree.command(name="play", description="เล่นเพลงจาก Spotify")
@app_commands.describe(query="ชื่อเพลง, ศิลปิน, หรือลิงก์ Spotify")
async def play(interaction: discord.Interaction, query: str):
    sp_user = get_user_spotify_client(interaction.user.id)
    if not sp_user:
        await interaction.response.send_message(
            "❌ กรุณาเชื่อมโยงบัญชี Spotify ของคุณก่อนโดยใช้ /link_spotify", 
            ephemeral=True
        )
        return

    await interaction.response.defer()

    try:
        track_uris = []
        context_uri = None
        response_msg = "🎶"

        if "spotify.com/track/" in query:
            track_id = query.split('/')[-1].split('?')[0]
            track_uri = f"spotify:track:{track_id}"
            track = await asyncio.to_thread(sp_user.track, track_uri)
            track_uris.append(track_uri)
            response_msg += f" กำลังเล่น: **{track['name']}** โดย **{track['artists'][0]['name']}**"
        elif "spotify.com/playlist/" in query:
            playlist_id = query.split('/')[-1].split('?')[0]
            context_uri = f"spotify:playlist:{playlist_id}"
            playlist = await asyncio.to_thread(sp_user.playlist, playlist_id)
            response_msg += f" กำลังเล่นเพลย์ลิสต์: **{playlist['name']}**"
        elif "spotify.com/album/" in query:
            album_id = query.split('/')[-1].split('?')[0]
            context_uri = f"spotify:album:{album_id}"
            album = await asyncio.to_thread(sp_user.album, album_id)
            response_msg += f" กำลังเล่นอัลบั้ม: **{album['name']}**"
        else:
            results = await asyncio.to_thread(sp_user.search, q=query, type='track', limit=1)
            if not results['tracks']['items']:
                await interaction.followup.send("❌ ไม่พบเพลงบน Spotify")
                return
            track = results['tracks']['items'][0]
            track_uris.append(track['uri'])
            response_msg += f" กำลังเล่น: **{track['name']}** โดย **{track['artists'][0]['name']}**"

        devices = await asyncio.to_thread(sp_user.devices)
        active_device_id = None
        for device in devices['devices']:
            if device['is_active']:
                active_device_id = device['id']
                break
        
        if not active_device_id:
            await interaction.followup.send("❌ ไม่พบ Spotify client ที่ใช้งานอยู่ กรุณาเปิดแอป Spotify ของคุณและเล่นเพลงอะไรก็ได้ที่นั่นก่อน หรือเลือกอุปกรณ์สำหรับเล่นใน Spotify.")
            return

        if context_uri:
            await asyncio.to_thread(sp_user.start_playback, device_id=active_device_id, context_uri=context_uri)
        else:
            await asyncio.to_thread(sp_user.start_playback, device_id=active_device_id, uris=track_uris)
        
        await interaction.followup.send(response_msg)

    except spotipy.exceptions.SpotifyException as e:
        if e.http_status == 401:
            await interaction.followup.send("❌ โทเค็น Spotify หมดอายุ กรุณาเชื่อมโยงบัญชีของคุณใหม่โดยใช้ /link_spotify.")
            if interaction.user.id in spotify_users:
                del spotify_users[interaction.user.id]
                await update_user_data_in_firestore(interaction.user.id, spotify_token_info=firestore.DELETE_FIELD)
        elif e.http_status == 404 and "Device not found" in str(e):
            await interaction.followup.send("❌ ไม่พบ Spotify client ที่ใช้งานอยู่ กรุณาเปิดแอป Spotify ของคุณ.")
        elif e.http_status == 403:
            await interaction.followup.send("❌ ข้อผิดพลาดในการเล่น Spotify: คุณอาจต้องมีบัญชี Spotify Premium หรือมีข้อจำกัดในการเล่น.")
        else:
            await interaction.followup.send(f"❌ ข้อผิดพลาด Spotify: {e}. โปรดลองอีกครั้ง.")
        logging.error(f"ข้อผิดพลาด Spotify สำหรับผู้ใช้ {interaction.user.id}: {e}", exc_info=True)
    except Exception as e:
        await interaction.followup.send(f"❌ เกิดข้อผิดพลาดที่ไม่คาดคิด: {e}")
        logging.error(f"ข้อผิดพลาดที่ไม่คาดคิดในคำสั่ง play: {e}", exc_info=True)

@tree.command(name="pause", description="หยุดเล่น Spotify ชั่วคราว")
async def pause_spotify(interaction: discord.Interaction):
    sp_user = get_user_spotify_client(interaction.user.id)
    if not sp_user:
        await interaction.response.send_message("❌ กรุณาเชื่อมโยงบัญชี Spotify ของคุณก่อนโดยใช้ /link_spotify", ephemeral=True)
        return
    
    try:
        await asyncio.to_thread(sp_user.pause_playback)
        await interaction.response.send_message("⏸️ หยุดเล่น Spotify ชั่วคราว", ephemeral=True)
    except spotipy.exceptions.SpotifyException as e:
        await interaction.response.send_message(f"❌ ข้อผิดพลาดในการหยุดเล่น Spotify: {e}", ephemeral=True)
        logging.error(f"ข้อผิดพลาดในการหยุดเล่น Spotify สำหรับผู้ใช้ {interaction.user.id}: {e}", exc_info=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ เกิดข้อผิดพลาดที่ไม่คาดคิด: {e}", ephemeral=True)
        logging.error(f"ข้อผิดพลาดที่ไม่คาดคิดในคำสั่ง pause: {e}", exc_info=True)

@tree.command(name="resume", description="เล่น Spotify ต่อ")
async def resume_spotify(interaction: discord.Interaction):
    sp_user = get_user_spotify_client(interaction.user.id)
    if not sp_user:
        await interaction.response.send_message("❌ กรุณาเชื่อมโยงบัญชี Spotify ของคุณก่อนโดยใช้ /link_spotify", ephemeral=True)
        return
    
    try:
        await asyncio.to_thread(sp_user.start_playback)
        await interaction.response.send_message("▶️ เล่น Spotify ต่อ", ephemeral=True)
    except spotipy.exceptions.SpotifyException as e:
        await interaction.response.send_message(f"❌ ข้อผิดพลาดในการเล่น Spotify ต่อ: {e}", ephemeral=True)
        logging.error(f"ข้อผิดพลาดในการเล่น Spotify ต่อสำหรับผู้ใช้ {interaction.user.id}: {e}", exc_info=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ เกิดข้อผิดพลาดที่ไม่คาดคิด: {e}", ephemeral=True)
        logging.error(f"ข้อผิดพลาดที่ไม่คาดคิดในคำสั่ง resume: {e}", exc_info=True)

@tree.command(name="skip", description="ข้ามเพลงปัจจุบัน")
async def skip_spotify(interaction: discord.Interaction):
    sp_user = get_user_spotify_client(interaction.user.id)
    if not sp_user:
        await interaction.response.send_message("❌ กรุณาเชื่อมโยงบัญชี Spotify ของคุณก่อนโดยใช้ /link_spotify", ephemeral=True)
        return
    
    try:
        await asyncio.to_thread(sp_user.next_track)
        await interaction.response.send_message("⏭️ ข้ามเพลงแล้ว", ephemeral=True)
    except spotipy.exceptions.SpotifyException as e:
        await interaction.response.send_message(f"❌ ข้อผิดพลาดในการข้าม Spotify: {e}", ephemeral=True)
        logging.error(f"ข้อผิดพลาดในการข้าม Spotify สำหรับผู้ใช้ {interaction.user.id}: {e}", exc_info=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ เกิดข้อผิดพลาดที่ไม่คาดคิด: {e}", ephemeral=True)
        logging.error(f"ข้อผิดพลาดที่ไม่คาดคิดในคำสั่ง skip: {e}", exc_info=True)

@tree.command(name="speak", description="ให้บอทพูดในช่องเสียง")
@app_commands.describe(message="ข้อความที่จะให้บอทพูด")
@app_commands.describe(lang="ภาษา (เช่น 'en', 'th')")
async def speak(interaction: discord.Interaction, message: str, lang: str = 'en'):
    global voice_client
    if not voice_client or not voice_client.is_connected():
        await interaction.response.send_message("❌ บอทไม่ได้อยู่ในช่องเสียง. ใช้ /join ก่อน", ephemeral=True)
        return
    
    await interaction.response.defer()
    try:
        tts_filename = f"tts_discord_{interaction.id}.mp3"
        await asyncio.to_thread(gTTS(message, lang=lang).save, tts_filename)
        
        source = discord.FFmpegPCMAudio(tts_filename, executable="ffmpeg")
        # ใช้ bot.loop เพื่อรัน callback ในเธรดหลักของบอท
        voice_client.play(source, after=lambda e: asyncio.run_coroutine_threadsafe(cleanup_audio(e, tts_filename), bot.loop))
        
        await interaction.followup.send(f"🗣️ กำลังพูด: **{message}** (ภาษา: {lang})")

    except Exception as e:
        await interaction.followup.send(f"❌ เกิดข้อผิดพลาดในการพูด: {e}")
        logging.error(f"ข้อผิดพลาด TTS: {e}", exc_info=True)

async def cleanup_audio(error, filename):
    """ล้างไฟล์เสียง TTS หลังจากเล่น"""
    if error:
        logging.error(f"ข้อผิดพลาดในการเล่น TTS: {error}")
    if os.path.exists(filename):
        os.remove(filename)
        logging.info(f"ล้างไฟล์เสียง: {filename}")

@tree.command(name="random_name", description="สุ่มเลือกชื่อจากรายการที่ให้มา")
@app_commands.describe(names="ชื่อหรือรายการที่คั่นด้วยเครื่องหมายจุลภาค (เช่น John, Doe, Alice)")
async def random_name(interaction: discord.Interaction, names: str):
    try:
        name_list = [name.strip() for name in names.split(',') if name.strip()]
        
        if not name_list:
            await interaction.response.send_message("❌ โปรดระบุชื่ออย่างน้อยหนึ่งชื่อที่คั่นด้วยจุลภาค", ephemeral=True)
            return

        selected_name = random.choice(name_list)
        await interaction.response.send_message(f"✨ ชื่อที่ถูกสุ่มเลือกคือ: **{selected_name}**")
        logging.info(f"{interaction.user.display_name} สุ่มชื่อ: '{names}' และได้ '{selected_name}'")

    except Exception as e:
        logging.error(f"ข้อผิดพลาดในคำสั่ง random_name: {e}", exc_info=True)
        await interaction.response.send_message(f"❌ เกิดข้อผิดพลาดในการสุ่มชื่อ: {e}", ephemeral=True)

class PollView(discord.ui.View):
    def __init__(self, poll_id, question, options):
        super().__init__(timeout=None)
        self.poll_id = poll_id
        self.question = question
        self.options = options
        
        if poll_id not in active_polls:
            active_polls[poll_id] = {
                "question": question,
                "options": options,
                "votes": {option: set() for option in options}
            }
        
        for i, option in enumerate(options):
            button = discord.ui.Button(label=option, custom_id=f"poll_{poll_id}_{i}", style=discord.ButtonStyle.primary)
            button.callback = self._button_callback
            self.add_item(button)

        show_results_button_item = discord.ui.Button(label="แสดงผลลัพธ์", style=discord.ButtonStyle.secondary, custom_id=f"poll_show_results_{poll_id}")
        show_results_button_item.callback = self.show_results_button
        self.add_item(show_results_button_item)

    async def on_timeout(self):
        logging.info(f"Poll {self.poll_id} หมดเวลา.")

    @discord.ui.button(label="แสดงผลลัพธ์", style=discord.ButtonStyle.secondary, custom_id="show_results_placeholder")
    async def show_results_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        expected_custom_id = f"poll_show_results_{self.poll_id}"
        if button.custom_id != expected_custom_id:
            await interaction.response.send_message("❌ ปุ่ม 'แสดงผลลัพธ์' นี้ไม่ได้เชื่อมโยงกับโพลล์ที่ใช้งานอยู่.", ephemeral=True)
            return

        await self.update_poll_message(interaction.message)
        await interaction.response.defer()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return True 

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item):
        logging.error(f"ข้อผิดพลาดในการโต้ตอบโพลล์: {error}", exc_info=True)
        try:
            await interaction.followup.send(f"❌ เกิดข้อผิดพลาดขณะประมวลผลการโหวตของคุณ: {error}", ephemeral=True)
        except discord.errors.NotFound:
            logging.warning(f"ไม่สามารถส่งข้อความแสดงข้อผิดพลาดไปยัง webhook (Unknown Webhook) สำหรับการโต้ตอบ {interaction.id}. ข้อผิดพลาดดั้งเดิม: {error}")
        except Exception as e:
            logging.error(f"ไม่สามารถส่งข้อความแสดงข้อผิดพลาดใน on_error. ข้อผิดพลาดรอง: {e}", exc_info=True)

    async def update_poll_message(self, message: discord.Message):
        """อัปเดตข้อความโพลล์พร้อมจำนวนคะแนนโหวตปัจจุบัน"""
        poll_data = active_polls.get(message.id)
        if not poll_data:
            logging.warning(f"พยายามอัปเดตโพลล์ที่ไม่มีอยู่: {message.id}")
            return

        embed = discord.Embed(
            title=f"📊 โพลล์: {poll_data['question']}",
            color=discord.Color.purple()
        )

        results_text = ""
        for option, voters in poll_data['votes'].items():
            results_text += f"**{option}**: {len(voters)} โหวต\n"
        
        embed.description = results_text if results_text else "ยังไม่มีคะแนนโหวต."
        embed.set_footer(text=f"Poll ID: {message.id}")
        
        await message.edit(embed=embed, view=self)

    async def _button_callback(self, interaction: discord.Interaction):
        custom_id = interaction.data['custom_id']
        parts = custom_id.split('_')
        if len(parts) != 3 or parts[0] != "poll":
            await interaction.response.send_message("❌ เกิดข้อผิดพลาดกับปุ่มโพลล์นี้", ephemeral=True)
            return

        poll_id = int(parts[1])
        option_index = int(parts[2])
        user_id = interaction.user.id
        
        poll_data = active_polls.get(poll_id)
        if not poll_data:
            await interaction.response.send_message("❌ โพลล์นี้ไม่ทำงานแล้ว.", ephemeral=True)
            return
        
        selected_option = poll_data['options'][option_index]

        user_changed_vote = False
        for option_key, voters_set in poll_data['votes'].items():
            if user_id in voters_set and option_key != selected_option:
                voters_set.remove(user_id)
                user_changed_vote = True
                logging.info(f"ผู้ใช้ {user_id} ลบการโหวตจาก {option_key} ในโพลล์ {poll_id}")
                break
        
        if user_id not in poll_data['votes'][selected_option]:
            poll_data['votes'][selected_option].add(user_id)
            logging.info(f"ผู้ใช้ {user_id} โหวตให้ {selected_option} ในโพลล์ {poll_id}")
            status_message = f"✅ คุณได้โหวตให้: **{selected_option}**"
        else:
            status_message = f"✅ คุณยังคงโหวตให้: **{selected_option}**"
            logging.info(f"ผู้ใช้ {user_id} ยืนยันการโหวตสำหรับ {selected_option} ในโพลล์ {poll_id}")

        await self.update_poll_message(interaction.message)
        await interaction.response.send_message(status_message, ephemeral=True)


@tree.command(name="poll", description="สร้างโพลล์ด้วยตัวเลือก")
@app_commands.describe(question="คำถามสำหรับโพลล์")
@app_commands.describe(options="ตัวเลือกสำหรับโพลล์ (คั่นด้วยจุลภาค เช่น ตัวเลือก A, ตัวเลือก B)")
async def create_poll(interaction: discord.Interaction, question: str, options: str):
    option_list = [opt.strip() for opt in options.split(',') if opt.strip()]

    if not option_list:
        await interaction.response.send_message("❌ โปรดระบุตัวเลือกอย่างน้อยหนึ่งตัวเลือกสำหรับโพลล์", ephemeral=True)
        return
    
    if len(option_list) > 25:
        await interaction.response.send_message("❌ รองรับสูงสุด 25 ตัวเลือกสำหรับโพลล์เท่านั้น", ephemeral=True)
        return

    embed = discord.Embed(
        title=f"📊 โพลล์: {question}",
        description="คลิกปุ่มด้านล่างเพื่อโหวต!",
        color=discord.Color.blue()
    )
    embed.set_footer(text=f"โพลล์สร้างโดย: {interaction.user.display_name}")

    initial_results_text = ""
    for option in option_list:
        initial_results_text += f"**{option}**: 0 โหวต\n"
    embed.add_field(name="ผลโหวตเบื้องต้น", value=initial_results_text, inline=False)


    await interaction.response.defer(ephemeral=False)

    message = await interaction.followup.send(embed=embed)
    
    active_polls[message.id] = {
        "question": question,
        "options": option_list,
        "votes": {option: set() for option in option_list}
    }

    poll_view = PollView(message.id, question, option_list)
    
    await message.edit(embed=embed, view=poll_view) # แก้ไขตรงนี้ให้ใช้ embed ที่อัปเดตแล้ว
    logging.info(f"โพลล์สร้างโดย {interaction.user.display_name}: ID {message.id}, คำถาม: {question}, ตัวเลือก: {options}")


@tree.command(name="wake", description="ปลุกผู้ใช้ด้วย DM")
@app_commands.describe(user="เลือกผู้ใช้")
async def wake(interaction: discord.Interaction, user: discord.User):
    try:
        await user.send(f"⏰ คุณถูก {interaction.user.display_name} ปลุก! ตื่นนน!")
        await interaction.response.send_message(f"✅ ปลุก {user.name} แล้ว", ephemeral=True)
        logging.info(f"{interaction.user.display_name} ปลุก {user.name}.")
    except discord.Forbidden:
        await interaction.response.send_message(f"❌ ไม่สามารถส่ง DM ถึง {user.name} ได้ (อาจจะปิด DM หรือเป็นบอท)", ephemeral=True)
        logging.warning(f"ไม่สามารถส่ง DM ไปยัง {user.name} (Forbidden).")
    except Exception as e:
        await interaction.response.send_message(f"❌ เกิดข้อผิดพลาดในการส่ง DM: {e}", ephemeral=True)
        logging.error(f"ไม่สามารถปลุกผู้ใช้: {e} ได้", exc_info=True)


# --- Flask Routes for serving React frontend ---
@app.route("/", defaults={'path': ''})
@app.route("/<path:path>")
async def serve_react_app(path):
    if path != "" and os.path.exists(os.path.join(REACT_BUILD_DIR, path)):
        return send_from_directory(REACT_BUILD_DIR, path)
    else:
        return send_from_directory(REACT_BUILD_DIR, 'index.html')


# --- Flask Routes (API Endpoints) ---
# เนื่องจาก Web UI จะเป็น React แล้ว เราไม่จำเป็นต้องส่ง template variables
# แต่จะส่งข้อมูลผ่าน JSON API แทน
@app.route("/api/auth_status")
async def get_auth_status():
    current_session_id = session.get('session_id')
    discord_user_id = web_logged_in_users.get(current_session_id)
    is_discord_linked = bool(discord_user_id)
    is_spotify_linked = False
    discord_username = None # Add username field

    if discord_user_id:
        try:
            # ดึง username จาก bot cache
            user_obj = bot.get_user(discord_user_id)
            if user_obj:
                discord_username = user_obj.name
            else:
                # Fallback: fetch user if not in cache (may require privileged intents)
                # This could be slow, consider implications for frequent API calls
                try:
                    user_obj = await bot.fetch_user(discord_user_id)
                    discord_username = user_obj.name
                except Exception as e:
                    logging.warning(f"Could not fetch Discord user {discord_user_id} for API: {e}")
                    discord_username = str(discord_user_id) # Fallback to ID

            is_spotify_linked = await _check_spotify_link_status(discord_user_id)
        except Exception as e:
            logging.error(f"ข้อผิดพลาดในการตรวจสอบสถานะการเชื่อมโยง Spotify สำหรับผู้ใช้เว็บ {discord_user_id}: {e}")
            is_spotify_linked = False

    return jsonify({
        "is_discord_linked": is_discord_linked,
        "is_spotify_linked": is_spotify_linked,
        "discord_user_id": discord_user_id,
        "discord_username": discord_username
    })


@app.route("/api/discord_user_id")
def get_discord_user_id_api():
    current_session_id = session.get('session_id')
    discord_user_id = web_logged_in_users.get(current_session_id)
    return jsonify({"discord_user_id": discord_user_id})


@app.route("/login/discord")
def login_discord():
    discord_auth_url = (
        f"https://discord.com/api/oauth2/authorize"
        f"?client_id={DISCORD_CLIENT_ID}"
        f"&redirect_uri={DISCORD_REDIRECT_URI}"
        f"&response_type=code"
        f"&scope={'+'.join(DISCORD_OAUTH_SCOPES.split(' '))}"
    )
    return redirect(discord_auth_url)

@app.route("/callback/discord")
async def discord_callback(): # เปลี่ยนเป็น async def
    code = request.args.get("code")
    error = request.args.get("error")
    current_session_id = session.get('session_id') 

    if error:
        flash(f"❌ ข้อผิดพลาด Discord OAuth: {error}", "error")
        return redirect(url_for("serve_react_app")) # Redirect ไปยังหน้า React App

    if not code:
        flash("❌ ไม่ได้รับรหัสอนุญาต", "error")
        return redirect(url_for("serve_react_app")) # Redirect ไปยังหน้า React App

    try:
        # เรียก _fetch_discord_token_and_user โดยตรงใน async context ของ Flask
        token_info, user_data = await _fetch_discord_token_and_user(code)
        
        discord_user_id = int(user_data["id"])
        # discord_username = user_data["username"] # Use this if 'username' is consistently available

        if not current_session_id:
            current_session_id = os.urandom(16).hex()
            session['session_id'] = current_session_id

        # เรียก update_user_data_in_firestore ใน async context ของบอท
        await update_user_data_in_firestore(discord_user_id, flask_session_to_add=current_session_id)

        web_logged_in_users[current_session_id] = discord_user_id
        session['discord_user_id_for_web'] = discord_user_id

        flash(f"✅ เข้าสู่ระบบ Discord สำเร็จ!", "success") # ไม่แสดง username ตรงนี้
        logging.info(f"Discord login successful for user ID: {discord_user_id}")

    except Exception as e:
        flash(f"❌ ข้อผิดพลาดในการเข้าสู่ระบบ Discord: {e}", "error")
        logging.error(f"ข้อผิดพลาด Discord OAuth: {e}", exc_info=True)
    
    return redirect(url_for("serve_react_app")) # Redirect ไปยังหน้า React App

@app.route("/login/spotify/<int:discord_user_id_param>")
async def login_spotify_web(discord_user_id_param: int): # เปลี่ยนเป็น async def
    current_session_id = session.get('session_id')
    logged_in_discord_user_id = web_logged_in_users.get(current_session_id)

    if logged_in_discord_user_id != discord_user_id_param:
        flash("❌ Discord User ID ไม่ตรงกัน กรุณาเข้าสู่ระบบด้วย Discord อีกครั้ง.", "error")
        return redirect(url_for("serve_react_app")) # Redirect ไปยังหน้า React App

    auth_manager = SpotifyOAuth(
        client_id=SPOTIPY_CLIENT_ID,
        client_secret=SPOTIPY_CLIENT_SECRET,
        redirect_uri=SPOTIPY_REDIRECT_URI,
        scope=SPOTIPY_SCOPES,
        show_dialog=True
    )
    # Blocking call, run in executor
    auth_url = await asyncio.to_thread(auth_manager.get_authorize_url)
    
    session['spotify_auth_discord_user_id'] = discord_user_id_param
    return redirect(auth_url)

@app.route("/callback/spotify")
async def spotify_callback(): # เปลี่ยนเป็น async def
    code = request.args.get("code")
    error = request.args.get("error")
    discord_user_id = session.pop('spotify_auth_discord_user_id', None) 
    
    if error:
        flash(f"❌ ข้อผิดพลาด Spotify OAuth: {error}", "error")
        return redirect(url_for("serve_react_app")) # Redirect ไปยังหน้า React App

    if not code or not discord_user_id:
        flash("❌ รหัสอนุญาตหรือ Discord user ID สำหรับการเชื่อมโยง Spotify หายไป โปรดลองอีกครั้ง.", "error")
        return redirect(url_for("serve_react_app")) # Redirect ไปยังหน้า React App

    try:
        auth_manager = SpotifyOAuth(
            client_id=SPOTIPY_CLIENT_ID,
            client_secret=SPOTIPY_CLIENT_SECRET,
            redirect_uri=SPOTIPY_REDIRECT_URI,
            scope=SPOTIPY_SCOPES,
        )

        # ใช้ asyncio.to_thread สำหรับการเรียก blocking call (get_access_token)
        token_info = await asyncio.to_thread(auth_manager.get_access_token, code)

        sp_user = spotipy.Spotify(auth_manager=auth_manager)
        spotify_users[discord_user_id] = sp_user
        
        # ใช้ await โดยตรงเนื่องจาก spotify_callback เป็น async แล้ว
        await update_user_data_in_firestore(discord_user_id, spotify_token_info=token_info)

        flash("✅ เชื่อมโยง Spotify สำเร็จ!", "success")
        logging.info(f"Spotify login successful for user ID: {discord_user_id}")
        
    except Exception as e:
        flash(f"❌ ข้อผิดพลาดในการเชื่อมโยง Spotify: {e}. โปรดตรวจสอบให้แน่ใจว่า redirect URI ของคุณถูกต้องใน Spotify Developer Dashboard.", "error")
        logging.error(f"ข้อผิดพลาด Spotify callback สำหรับผู้ใช้ {discord_user_id}: {e}", exc_info=True)
    
    return redirect(url_for("serve_react_app")) # Redirect ไปยังหน้า React App

# --- Flask routes for controlling bot from web ---
@app.route("/web_control/add", methods=["POST"])
async def add_web_queue(): # เปลี่ยนเป็น async def
    global voice_client, queue
    url = request.json.get("url") # รับข้อมูลเป็น JSON
    if not url:
        return jsonify({"status": "error", "message": "ไม่ได้ระบุ URL เพื่อเพิ่มลงในคิว."}), 400

    current_session_id = session.get('session_id')
    discord_user_id = web_logged_in_users.get(current_session_id)

    if not discord_user_id:
        return jsonify({"status": "error", "message": "กรุณาเข้าสู่ระบบด้วย Discord ก่อนเพื่อใช้การควบคุมเพลง."}), 401

    target_channel = None
    if voice_client and voice_client.is_connected():
        target_channel = voice_client.channel
    else:
        try:
            # รอจนกว่าบอทพร้อม
            await bot_ready.wait() 
            user = bot.get_user(discord_user_id)
            if user and user.voice and user.voice.channel:
                target_channel = user.voice.channel
                if not voice_client or not voice_client.is_connected():
                    try:
                        voice_client = await target_channel.connect()
                        logging.info(f"บอทเข้าร่วม {target_channel.name} โดยอัตโนมัติสำหรับการเล่นผ่านเว็บ.")
                    except discord.ClientException as e:
                        logging.error(f"ไม่สามารถเข้าร่วมช่องเสียงโดยอัตโนมัติ: {e}")
                        return jsonify({"status": "error", "message": f"❌ ไม่สามารถเข้าร่วมช่องเสียงโดยอัตโนมัติ: {e}"}), 500
            else:
                return jsonify({"status": "error", "message": "❌ คุณไม่ได้อยู่ในช่องเสียง Discord หรือบอทไม่สามารถเข้าถึงได้"}), 400
        except Exception as e:
            logging.error(f"ข้อผิดพลาดในการค้นหา Voice Channel ของผู้ใช้สำหรับการเพิ่มคิวเว็บ: {e}", exc_info=True)
            return jsonify({"status": "error", "message": f"❌ ข้อผิดพลาด: {e}"}), 500

    if not target_channel:
        return jsonify({"status": "error", "message": "❌ บอทไม่ได้อยู่ในช่องเสียง. กรุณาใช้คำสั่ง `/join` ใน Discord ก่อน."}), 400

    queue.append(url)
    logging.info(f"เพิ่มลงในคิวจากเว็บ: {url}")

    if not voice_client.is_playing() and not voice_client.is_paused():
        await _play_next_in_queue(target_channel) # เรียกโดยตรงใน async context

    return jsonify({"status": "success", "message": f"เพิ่ม '{url}' ลงในคิวแล้ว!"})

@app.route("/web_control/play_spotify_search", methods=["POST"])
async def web_control_play_spotify_search():
    query = request.json.get("query")
    if not query:
        return jsonify({"status": "error", "message": "ไม่ได้ระบุคำค้นหา."}), 400
    
    current_session_id = session.get('session_id')
    discord_user_id = web_logged_in_users.get(current_session_id)
    if not discord_user_id:
        return jsonify({"status": "error", "message": "กรุณาเข้าสู่ระบบด้วย Discord ก่อนเพื่อใช้การควบคุมเพลง Spotify."}), 401

    sp_user = get_user_spotify_client(discord_user_id)
    if not sp_user:
        return jsonify({"status": "error", "message": "กรุณาเชื่อมโยงบัญชี Spotify ของคุณก่อน."}), 403

    try:
        track_uris = []
        context_uri = None
        response_msg_title = ""

        if "spotify.com/track/" in query:
            track_id = query.split('/')[-1].split('?')[0]
            track_uri = f"spotify:track:{track_id}"
            track = await asyncio.to_thread(sp_user.track, track_uri)
            track_uris.append(track_uri)
            response_msg_title = f"**{track['name']}** โดย **{track['artists'][0]['name']}**"
        elif "spotify.com/playlist/" in query:
            playlist_id = query.split('/')[-1].split('?')[0]
            context_uri = f"spotify:playlist:{playlist_id}"
            playlist = await asyncio.to_thread(sp_user.playlist, playlist_id)
            response_msg_title = f"เพลย์ลิสต์: **{playlist['name']}**"
        elif "spotify.com/album/" in query:
            album_id = query.split('/')[-1].split('?')[0]
            context_uri = f"spotify:album:{album_id}"
            album = await asyncio.to_thread(sp_user.album, album_id)
            response_msg_title = f"อัลบั้ม: **{album['name']}**"
        else:
            results = await asyncio.to_thread(sp_user.search, q=query, type='track', limit=1)
            if not results['tracks']['items']:
                return jsonify({"status": "error", "message": "ไม่พบเพลงบน Spotify."}), 404
            track = results['tracks']['items'][0]
            track_uris.append(track['uri'])
            response_msg_title = f"**{track['name']}** โดย **{track['artists'][0]['name']}**"

        devices = await asyncio.to_thread(sp_user.devices)
        active_device_id = None
        for device in devices['devices']:
            if device['is_active']:
                active_device_id = device['id']
                break
        
        if not active_device_id:
            return jsonify({"status": "error", "message": "ไม่พบ Spotify client ที่ใช้งานอยู่ กรุณาเปิดแอป Spotify ของคุณและเล่นเพลงอะไรก็ได้ที่นั่นก่อน."}), 404

        if context_uri:
            await asyncio.to_thread(sp_user.start_playback, device_id=active_device_id, context_uri=context_uri)
        else:
            await asyncio.to_thread(sp_user.start_playback, device_id=active_device_id, uris=track_uris)
        
        return jsonify({"status": "success", "message": f"กำลังเล่น Spotify: {response_msg_title}"})

    except spotipy.exceptions.SpotifyException as e:
        error_message = f"ข้อผิดพลาด Spotify: {e.msg}"
        if e.http_status == 401:
            error_message = "โทเค็น Spotify หมดอายุ กรุณาเชื่อมโยงบัญชีของคุณใหม่."
            # Clear token in Firestore
            await update_user_data_in_firestore(discord_user_id, spotify_token_info=firestore.DELETE_FIELD)
            if discord_user_id in spotify_users:
                del spotify_users[discord_user_id]
        elif e.http_status == 404 and "Device not found" in str(e):
            error_message = "ไม่พบ Spotify client ที่ใช้งานอยู่ กรุณาเปิดแอป Spotify ของคุณ."
        elif e.http_status == 403:
            error_message = "ข้อผิดพลาดในการเล่น Spotify: คุณอาจต้องมีบัญชี Spotify Premium หรือมีข้อจำกัดในการเล่น."
        
        logging.error(f"ข้อผิดพลาด Spotify สำหรับผู้ใช้ {discord_user_id}: {e}", exc_info=True)
        return jsonify({"status": "error", "message": error_message}), e.http_status or 500
    except Exception as e:
        logging.error(f"ข้อผิดพลาดที่ไม่คาดคิดใน web_control_play_spotify_search: {e}", exc_info=True)
        return jsonify({"status": "error", "message": f"เกิดข้อผิดพลาดที่ไม่คาดคิด: {e}"}), 500


@app.route("/web_control/pause")
async def pause_web_control(): # เปลี่ยนเป็น async def
    current_session_id = session.get('session_id')
    discord_user_id = web_logged_in_users.get(current_session_id)
    if not discord_user_id:
        return jsonify({"status": "error", "message": "กรุณาเข้าสู่ระบบด้วย Discord ก่อน."}), 401
    
    # Check if bot is playing from its queue first
    if voice_client and voice_client.is_playing():
        voice_client.pause()
        return jsonify({"status": "success", "message": "หยุดเล่นคิวบอทชั่วคราวแล้ว."})
    
    # If not, try to pause Spotify playback
    sp_user = get_user_spotify_client(discord_user_id)
    if sp_user:
        try:
            await asyncio.to_thread(sp_user.pause_playback)
            return jsonify({"status": "success", "message": "หยุดเล่น Spotify ชั่วคราวแล้ว."})
        except spotipy.exceptions.SpotifyException as e:
            logging.error(f"ข้อผิดพลาดในการหยุดเล่น Spotify จากเว็บ: {e}", exc_info=True)
            return jsonify({"status": "error", "message": f"ข้อผิดพลาด Spotify: {e.msg}"}), e.http_status or 500
    
    return jsonify({"status": "warning", "message": "ไม่มีอะไรให้หยุดเล่น."})


@app.route("/web_control/resume")
async def resume_web_control(): # เปลี่ยนเป็น async def
    current_session_id = session.get('session_id')
    discord_user_id = web_logged_in_users.get(current_session_id)
    if not discord_user_id:
        return jsonify({"status": "error", "message": "กรุณาเข้าสู่ระบบด้วย Discord ก่อน."}), 401

    # Check if bot has a paused queue
    if voice_client and voice_client.is_paused():
        voice_client.resume()
        return jsonify({"status": "success", "message": "เล่นคิวบอทต่อแล้ว."})

    # If not, try to resume Spotify playback
    sp_user = get_user_spotify_client(discord_user_id)
    if sp_user:
        try:
            await asyncio.to_thread(sp_user.start_playback)
            return jsonify({"status": "success", "message": "เล่น Spotify ต่อแล้ว."})
        except spotipy.exceptions.SpotifyException as e:
            logging.error(f"ข้อผิดพลาดในการเล่น Spotify ต่อจากเว็บ: {e}", exc_info=True)
            return jsonify({"status": "error", "message": f"ข้อผิดพลาด Spotify: {e.msg}"}), e.http_status or 500

    return jsonify({"status": "warning", "message": "ไม่มีอะไรให้เล่นต่อ."})


@app.route("/web_control/stop")
async def stop_web_control(): # เปลี่ยนเป็น async def
    global queue, voice_client, current_playing_youtube_info
    
    current_session_id = session.get('session_id')
    discord_user_id = web_logged_in_users.get(current_session_id)
    if not discord_user_id:
        return jsonify({"status": "error", "message": "กรุณาเข้าสู่ระบบด้วย Discord ก่อน."}), 401

    queue.clear()
    current_playing_youtube_info = {} # Clear current playing info
    if voice_client and (voice_client.is_playing() or voice_client.is_paused()):
        voice_client.stop()
        await asyncio.to_thread(voice_client.disconnect) # ออกจากช่องเสียงด้วย
        voice_client = None
        return jsonify({"status": "success", "message": "หยุดเล่นและล้างคิวเพลงแล้ว."})
    
    # If no bot queue, try to stop Spotify playback
    sp_user = get_user_spotify_client(discord_user_id)
    if sp_user:
        try:
            await asyncio.to_thread(sp_user.pause_playback) # Spotify doesn't have a direct 'stop', pause is closest
            return jsonify({"status": "success", "message": "หยุดเล่น Spotify แล้ว."})
        except spotipy.exceptions.SpotifyException as e:
            logging.error(f"ข้อผิดพลาดในการหยุดเล่น Spotify จากเว็บ: {e}", exc_info=True)
            return jsonify({"status": "error", "message": f"ข้อผิดพลาด Spotify: {e.msg}"}), e.http_status or 500

    return jsonify({"status": "warning", "message": "ไม่มีอะไรให้หยุดเล่น."})

@app.route("/web_control/skip")
async def skip_web_control(): # เปลี่ยนเป็น async def
    global voice_client
    current_session_id = session.get('session_id')
    discord_user_id = web_logged_in_users.get(current_session_id)
    if not discord_user_id:
        return jsonify({"status": "error", "message": "กรุณาเข้าสู่ระบบด้วย Discord ก่อน."}), 401

    # Prioritize skipping bot's internal queue
    if voice_client and voice_client.is_playing():
        voice_client.stop() # Stopping effectively skips by triggering the 'after' callback
        return jsonify({"status": "success", "message": "ข้ามเพลงในคิวบอทแล้ว."})

    # If not playing bot's queue, try to skip Spotify
    sp_user = get_user_spotify_client(discord_user_id)
    if sp_user:
        try:
            await asyncio.to_thread(sp_user.next_track)
            return jsonify({"status": "success", "message": "ข้ามเพลง Spotify แล้ว."})
        except spotipy.exceptions.SpotifyException as e:
            logging.error(f"ข้อผิดพลาดในการข้าม Spotify จากเว็บ: {e}", exc_info=True)
            return jsonify({"status": "error", "message": f"ข้อผิดพลาด Spotify: {e.msg}"}), e.http_status or 500
    
    return jsonify({"status": "warning", "message": "ไม่มีอะไรให้ข้าม."})

@app.route("/web_control/skip_previous")
async def skip_previous_web_control(): # Add this route for previous track
    current_session_id = session.get('session_id')
    discord_user_id = web_logged_in_users.get(current_session_id)
    if not discord_user_id:
        return jsonify({"status": "error", "message": "กรุณาเข้าสู่ระบบด้วย Discord ก่อน."}), 401
    
    sp_user = get_user_spotify_client(discord_user_id)
    if sp_user:
        try:
            await asyncio.to_thread(sp_user.previous_track)
            return jsonify({"status": "success", "message": "เล่นเพลงก่อนหน้าบน Spotify แล้ว."})
        except spotipy.exceptions.SpotifyException as e:
            logging.error(f"ข้อผิดพลาดในการเล่นเพลงก่อนหน้า Spotify จากเว็บ: {e}", exc_info=True)
            return jsonify({"status": "error", "message": f"ข้อผิดพลาด Spotify: {e.msg}"}), e.http_status or 500
    
    return jsonify({"status": "warning", "message": "ฟังก์ชันเล่นเพลงก่อนหน้าใช้ได้เฉพาะ Spotify."})


@app.route("/web_control/set_volume", methods=["GET"]) # Changed to GET to easily pass volume in URL
async def set_volume_web_control(): # เปลี่ยนเป็น async def
    global volume, voice_client
    vol_str = request.args.get("vol")
    if not vol_str:
        return jsonify({"status": "error", "message": "ไม่ได้ระบุระดับเสียง."}), 400
    
    try:
        new_volume = float(vol_str)
        if not (0.0 <= new_volume <= 2.0):
            return jsonify({"status": "error", "message": "ระดับเสียงต้องอยู่ระหว่าง 0.0 ถึง 2.0"}), 400
        
        volume = new_volume
        if voice_client and voice_client.source:
            voice_client.source.volume = volume
        
        return jsonify({"status": "success", "message": f"ปรับระดับเสียงเป็น {volume*100:.0f}%"})
    except ValueError:
        return jsonify({"status": "error", "message": "ระดับเสียงไม่ถูกต้อง."}), 400
    except Exception as e:
        logging.error(f"ข้อผิดพลาดในการปรับระดับเสียงจากเว็บ: {e}", exc_info=True)
        return jsonify({"status": "error", "message": f"เกิดข้อผิดพลาด: {e}"}), 500


# --- New API Endpoints for Web UI Data ---
@app.route("/api/now_playing_data")
async def get_now_playing_data():
    current_session_id = session.get('session_id')
    discord_user_id = web_logged_in_users.get(current_session_id)

    if not discord_user_id:
        return jsonify({"status": "not_logged_in"}), 200 # Return 200 with status for UI to handle

    sp_user = get_user_spotify_client(discord_user_id)
    if sp_user:
        try:
            # Fetch current Spotify playback state
            playback_state = await asyncio.to_thread(sp_user.current_playback)
            if playback_state and playback_state['is_playing']:
                track = playback_state['item']
                progress_ms = playback_state['progress_ms']
                duration_ms = track['duration_ms']
                
                return jsonify({
                    "status": "playing_spotify",
                    "title": track['name'],
                    "artist": track['artists'][0]['name'],
                    "album_cover_url": track['album']['images'][0]['url'] if track['album']['images'] else "https://placehold.co/400x400/3498db/ffffff?text=No+Cover",
                    "progress_ms": progress_ms,
                    "duration_ms": duration_ms
                })
            else:
                # If Spotify is paused/stopped, but bot queue is playing
                if voice_client and voice_client.is_playing() and current_playing_youtube_info:
                     return jsonify({
                        "status": "playing_youtube",
                        "title": current_playing_youtube_info.get('title', 'Unknown Title'),
                        "artist": "YouTube/SoundCloud", # Or get uploader if available from yt_dlp info
                        "album_cover_url": current_playing_youtube_info.get('thumbnail', 'https://placehold.co/400x400/FF0000/FFFFFF?text=YouTube'),
                        "progress_ms": voice_client.source.play_time * 1000 if voice_client.source else 0, # Approximate progress
                        "duration_ms": current_playing_youtube_info.get('duration', 0) * 1000
                    })
                return jsonify({"status": "spotify_paused_or_stopped"})
        except spotipy.exceptions.SpotifyException as e:
            logging.error(f"Spotify API error fetching playback for {discord_user_id}: {e}")
            # Consider unlinking Spotify if 401 error (expired token)
            if e.http_status == 401:
                 # Schedule to run in bot's loop to update Firestore
                asyncio.run_coroutine_threadsafe(
                    update_user_data_in_firestore(discord_user_id, spotify_token_info=firestore.DELETE_FIELD),
                    bot.loop
                )
                if discord_user_id in spotify_users:
                    del spotify_users[discord_user_id]
                return jsonify({"status": "spotify_error", "message": "Spotify token expired. Please relink."}), 200 # Return 200, UI handles error status
            return jsonify({"status": "spotify_error", "message": str(e)}), 200
        except Exception as e:
            logging.error(f"Unexpected error fetching Spotify playback for {discord_user_id}: {e}")
            return jsonify({"status": "error", "message": "Failed to fetch Spotify playback."}), 200
    
    # If not playing Spotify, check bot's internal queue playback (YouTube/SoundCloud)
    if voice_client and voice_client.is_playing() and current_playing_youtube_info:
        return jsonify({
            "status": "playing_youtube",
            "title": current_playing_youtube_info.get('title', 'Unknown Title'),
            "artist": "YouTube/SoundCloud", # Or get uploader if available from yt_dlp info
            "album_cover_url": current_playing_youtube_info.get('thumbnail', 'https://placehold.co/400x400/FF0000/FFFFFF?text=YouTube'),
            "progress_ms": voice_client.source.play_time * 1000 if voice_client.source else 0, # Approximate progress
            "duration_ms": current_playing_youtube_info.get('duration', 0) * 1000
        })
    elif voice_client and voice_client.is_paused() and current_playing_youtube_info:
         return jsonify({
            "status": "youtube_paused",
            "title": current_playing_youtube_info.get('title', 'Unknown Title'),
            "artist": "YouTube/SoundCloud", # Or get uploader if available from yt_dlp info
            "album_cover_url": current_playing_youtube_info.get('thumbnail', 'https://placehold.co/400x400/FF0000/FFFFFF?text=YouTube'),
            "progress_ms": voice_client.source.play_time * 1000 if voice_client.source else 0, # Approximate progress
            "duration_ms": current_playing_youtube_info.get('duration', 0) * 1000
        })
    
    return jsonify({"status": "no_music_playing"})

@app.route("/api/queue_data")
async def get_queue_data(): # make it async if it might call async functions later
    # Return the global queue list (which stores URLs)
    # In a real app, you might want to fetch more metadata for each item in the queue.
    # For now, we'll return the URLs.
    
    # Optionally, fetch titles for items in queue (can be slow for large queues)
    # This requires more complex async handling and could be inefficient
    # For simplicity, we return URLs only as PRD doesn't specify complex queue display
    
    return jsonify({"queue": queue}) 


# --- Run Flask + Discord bot ---
def run_web_app(): # เปลี่ยนชื่อฟังก์ชันเพื่อให้ชัดเจนว่าเป็นสำหรับ Flask
    # โหลดเซสชัน Discord web login เมื่อ Flask app เริ่มทำงาน
    # ทำการโหลดข้อมูล Firebase ในเธรด Flask โดยใช้ asyncio.run
    # เพื่อให้มี Event Loop เป็นของตัวเอง
    logging.info("กำลังโหลดข้อมูลผู้ใช้จาก Firestore ในเธรดเว็บ...")
    asyncio.run(load_all_user_data_from_firestore()) 
    logging.info("โหลดข้อมูลผู้ใช้จาก Firestore ในเธรดเว็บสำเร็จแล้ว.")

    # Flask app should run in its own thread
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)

if __name__ == "__main__":
    print("\n--- กำลังเริ่มต้นบอทและเว็บเซิร์ฟเวอร์ ---")
    print("ตรวจสอบให้แน่ใจว่าติดตั้ง FFmpeg และ Opus แล้วสำหรับฟังก์ชันเสียง")
    print("---------------------------------------\n")

    # เริ่ม Flask web server ในเธรดแยก
    web_thread = threading.Thread(target=run_web_app) # เรียกใช้ฟังก์ชันที่เปลี่ยนชื่อแล้ว
    web_thread.start()
    
    # รัน Discord bot (นี่คือ Blocking Call)
    # bot.run() จะสร้างและรัน asyncio event loop ของตัวเอง
    bot.run(DISCORD_TOKEN)
