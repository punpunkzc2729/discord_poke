import discord
from discord.ext import commands
from discord import app_commands
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
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

# Firestore imports
import firebase_admin
from firebase_admin import credentials, firestore
from firebase_admin import exceptions as firebase_exceptions 

# ปิดข้อความรายงานบั๊กของ yt_dlp เพื่อไม่ให้แสดงในคอนโซล
yt_dlp.utils.bug_reports_message = lambda: ''

# --- โหลดตัวแปรสภาพแวดล้อม ---
load_dotenv()

# --- ข้อมูลประจำตัว Spotify API ---
# ควรตั้งค่าในไฟล์ .env
SPOTIPY_CLIENT_ID = os.getenv("SPOTIPY_CLIENT_ID")
SPOTIPY_CLIENT_SECRET = os.getenv("SPOTIPY_CLIENT_SECRET")
SPOTIPY_REDIRECT_URI = os.getenv("SPOTIPY_REDIRECT_URI")
# Scopes ที่จำเป็นสำหรับ Spotify API
SPOTIPY_SCOPES = "user-read-playback-state user-modify-playback-state user-read-currently-playing playlist-read-private playlist-read-collaborative user-library-read"

# --- ข้อมูลประจำตัว Discord Bot ---
# ควรตั้งค่าในไฟล์ .env
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
# ตรวจสอบให้แน่ใจว่า GUILD_ID ในไฟล์ .env เป็นตัวเลขล้วนๆ เพื่อหลีกเลี่ยง ValueError
# เพิ่ม .strip().split('#')[0] เพื่อจัดการคอมเมนต์หรือช่องว่างในไฟล์ .env
YOUR_GUILD_ID = int(os.environ["GUILD_ID"].strip().split('#')[0]) 
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
DISCORD_REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI")
# Scopes ที่จำเป็นสำหรับ Discord OAuth2
DISCORD_OAUTH_SCOPES = "identify guilds"

# --- Firebase Setup ---
# ข้อมูลรับรอง Firebase ที่เข้ารหัส Base64 ควรอยู่ในตัวแปรสภาพแวดล้อม
firebase_credentials_base64 = os.getenv("FIREBASE_CREDENTIALS_BASE64")
db = None # กำหนด db เป็น None เริ่มต้น
if not firebase_credentials_base64:
    logging.error("FIREBASE_CREDENTIALS_BASE64 environment variable not set. Firestore will not work.")
else:
    try:
        # ถอดรหัสข้อมูลรับรอง base64 และแยกวิเคราะห์ JSON
        decoded_credentials = base64.b64decode(firebase_credentials_base64).decode('utf-8')
        cred = credentials.Certificate(json.loads(decoded_credentials))
        firebase_admin.initialize_app(cred)
        db = firestore.client()
        logging.info("Firebase Admin SDK initialized successfully.")
    except Exception as e:
        logging.error(f"ข้อผิดพลาดในการเริ่มต้น Firebase Admin SDK: {e}", exc_info=True)
        db = None # ตั้งค่า db เป็น None หากเริ่มต้นล้มเหลว

# --- ตัวแปร Global ---
# เก็บ Spotify client object สำหรับแต่ละ Discord user ID
spotify_users = {}  # Key: Discord User ID, Value: Spotify client
# เก็บการเชื่อมโยง Flask session ID กับ Discord user ID สำหรับการควบคุมผ่านเว็บ
web_logged_in_users = {}  # Key: Flask Session ID, Value: Discord User ID
voice_client = None # Object สำหรับการจัดการการเชื่อมต่อช่องเสียงของ Discord
queue = []  # คิวเพลงสำหรับเล่น (รองรับ YouTube/SoundCloud URL)
volume = 1.0 # ระดับเสียงเริ่มต้น (0.0 ถึง 2.0)

# --- ตัวแปร Global สำหรับระบบโพลล์ ---
# Key: poll_message_id, Value: {"question": str, "options": list[str], "votes": {option_str: set[user_id]}}
active_polls = {}

# --- ตั้งค่าการบันทึก Log ---
logging.basicConfig(
    level=logging.INFO, # ระดับ Log ขั้นต่ำที่แสดง
    format='%(asctime)s:%(levelname)s:%(message)s', # รูปแบบของข้อความ Log
    handlers=[
        logging.FileHandler("bot.log"), # บันทึก Log ลงไฟล์
        logging.StreamHandler() # แสดง Log ในคอนโซล
    ]
)

# --- ตั้งค่า Discord Bot ---
# Intents ที่จำเป็นสำหรับบอท Discord
intents = discord.Intents.default() 
intents.message_content = True 
intents.voice_states = True 
intents.members = True 

bot = commands.Bot(command_prefix="!", intents=intents) # สร้าง Instance ของบอท
tree = bot.tree # สำหรับการจัดการ Slash Commands
bot_ready = asyncio.Event() # Event สำหรับส่งสัญญาณเมื่อบอทพร้อมใช้งานเต็มที่

# --- ตั้งค่า Flask App ---
app = Flask(__name__, static_folder="static", template_folder="templates") # สร้าง Instance ของ Flask App
# คีย์ลับสำหรับ Flask session ควรตั้งค่าในไฟล์ .env เพื่อความปลอดภัย
app.secret_key = os.getenv("FLASK_SECRET_KEY") or os.urandom(24) 

# --- ฟังก์ชันช่วย (Helper Functions) ---

def get_user_spotify_client(discord_user_id: int):
    """
    ดึง Spotify client สำหรับผู้ใช้ Discord
    ตรวจสอบการหมดอายุของโทเค็นโดยพยายามเรียก API ง่ายๆ และลบออกหากไม่ถูกต้อง
    """
    sp_client = spotify_users.get(discord_user_id)
    if sp_client:
        try:
            # ทดสอบว่าโทเค็นยังใช้ได้หรือไม่
            sp_client.current_user() 
            return sp_client
        except spotipy.exceptions.SpotifyException as e:
            logging.warning(f"Spotify token expired for user {discord_user_id}: {e}")
            # ถ้าโทเค็นหมดอายุ ให้ลบออกจากแคชของเราและ Firestore
            if discord_user_id in spotify_users:
                del spotify_users[discord_user_id]
                # ลบฟิลด์ spotify_token_info จาก Firestore
                asyncio.run_coroutine_threadsafe(
                    update_user_data_in_firestore(discord_user_id, spotify_token_info=firestore.DELETE_FIELD),
                    bot.loop
                ).result()
            return None
    return None

async def update_user_data_in_firestore(discord_user_id: int, spotify_token_info: dict = None, flask_session_to_add: str = None, flask_session_to_remove: str = None):
    """
    อัปเดตข้อมูลผู้ใช้ใน Firestore รวมถึงโทเค็น Spotify และเซสชัน Flask
    :param discord_user_id: ID ผู้ใช้ Discord (ใช้เป็น document ID)
    :param spotify_token_info: dict ข้อมูลโทเค็นจาก Spotipy หรือ firestore.DELETE_FIELD (เป็นทางเลือก)
    :param flask_session_to_add: Flask session ID ที่จะเพิ่ม (เป็นทางเลือก)
    :param flask_session_to_remove: Flask session ID ที่จะลบ (เป็นทางเลือก)
    """
    if db is None:
        logging.error("Firestore DB is not initialized. Cannot update user data.")
        return

    user_ref = db.collection('users').document(str(discord_user_id))
    user_data_to_update = {}

    try:
        doc = await asyncio.to_thread(user_ref.get)
        current_data = doc.to_dict() if doc.exists else {}
    except firebase_exceptions.FirebaseError as e:
        logging.error(f"Error fetching user {discord_user_id} data from Firestore: {e}", exc_info=True)
        return

    if spotify_token_info is not None: # ตรวจสอบว่าเป็น None จริงๆ (ไม่รวม firestore.DELETE_FIELD)
        user_data_to_update['spotify_token_info'] = spotify_token_info
    
    # จัดการ Flask sessions เป็น set เพื่อหลีกเลี่ยงค่าซ้ำ
    flask_sessions = set(current_data.get('flask_sessions', [])) 

    if flask_session_to_add:
        flask_sessions.add(flask_session_to_add)
    if flask_session_to_remove and flask_session_to_remove in flask_sessions:
        flask_sessions.remove(flask_session_to_remove)

    # แปลง set กลับเป็น list สำหรับ Firestore
    user_data_to_update['flask_sessions'] = list(flask_sessions)

    if user_data_to_update: # อัปเดตเฉพาะเมื่อมีข้อมูลที่จะตั้งค่า
        try:
            # ใช้ set with merge=True เพื่ออัปเดตฟิลด์เฉพาะโดยไม่เขียนทับเอกสารทั้งหมด
            await asyncio.to_thread(user_ref.set, user_data_to_update, merge=True)
            logging.info(f"ข้อมูลผู้ใช้ {discord_user_id} อัปเดตใน Firestore แล้ว.")
        except firebase_exceptions.FirebaseError as e:
            logging.error(f"ข้อผิดพลาดในการอัปเดตข้อมูลผู้ใช้ {discord_user_id} ใน Firestore: {e}", exc_info=True)

async def load_all_user_data_from_firestore():
    """
    โหลดข้อมูลผู้ใช้ทั้งหมด (โทเค็น Spotify, เซสชัน Flask) จาก Firestore เข้าสู่ตัวแปร global
    เพื่อฟื้นฟูสถานะเมื่อบอทเริ่มต้น
    """
    global spotify_users, web_logged_in_users 

    if db is None:
        logging.warning("Firestore DB is not initialized. Cannot load user data.")
        return

    try:
        users_ref = db.collection('users')
        docs = await asyncio.to_thread(users_ref.get) # ดึงเอกสารผู้ใช้ทั้งหมด

        for doc in docs:
            user_id = int(doc.id)
            data = doc.to_dict()
            
            # โหลดข้อมูลโทเค็น Spotify
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
                    # ตรวจสอบโทเค็นโดยการเรียกใช้ API ง่ายๆ
                    await asyncio.to_thread(sp_user.current_user)
                    spotify_users[user_id] = sp_user
                    logging.info(f"โหลดโทเค็น Spotify ที่ถูกต้องสำหรับผู้ใช้ ID: {user_id} จาก Firestore แล้ว")
                except spotipy.exceptions.SpotifyException:
                    logging.warning(f"โทเค็น Spotify สำหรับผู้ใช้ {user_id} หมดอายุเมื่อเริ่มต้น (Firestore) ลบออกจากแคชในเครื่อง.")
                    # คุณอาจต้องการลบออกจาก Firestore ที่นี่ด้วยหากไม่ถูกต้องอย่างสม่ำเสมอ
                    # await update_user_data_in_firestore(user_id, spotify_token_info=firestore.DELETE_FIELD)
                except Exception as e:
                    logging.error(f"ข้อผิดพลาดในการตรวจสอบโทเค็น Spotify ที่โหลดสำหรับผู้ใช้ {user_id}: {e}", exc_info=True)

            # โหลด Flask sessions
            flask_sessions_list = data.get('flask_sessions', [])
            for session_id in flask_sessions_list:
                web_logged_in_users[session_id] = user_id
            
        logging.info("โหลดข้อมูลผู้ใช้ทั้งหมด (โทเค็น Spotify และเซสชัน Flask) จาก Firestore แล้ว.")
    except firebase_exceptions.FirebaseError as e:
        logging.error(f"ข้อผิดพลาดในการโหลดข้อมูลผู้ใช้ทั้งหมดจาก Firestore: {e}", exc_info=True)
    except Exception as e:
        logging.error(f"ข้อผิดพลาดที่ไม่คาดคิดในการโหลดข้อมูลผู้ใช้จาก Firestore: {e}", exc_info=True)


async def _check_spotify_link_status(discord_user_id: int) -> bool:
    """
    ตรวจสอบสถานะการเชื่อมโยง Spotify ของผู้ใช้
    """
    sp_client = spotify_users.get(discord_user_id)
    if sp_client:
        try:
            # ทำการเรียก Spotify API เล็กน้อยเพื่อตรวจสอบโทเค็น
            await asyncio.to_thread(sp_client.current_user)
            return True
        except spotipy.exceptions.SpotifyException as e:
            logging.warning(f"ตรวจสอบโทเค็น Spotify ล้มเหลวสำหรับผู้ใช้ {discord_user_id}: {e}")
            # หากโทเค็นหมดอายุ ให้ลบออกจากแคชและ Firestore
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
        # แลกเปลี่ยนรหัสอนุญาตสำหรับโทเค็น
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
        token_response.raise_for_status() # Raise an exception for HTTP errors
        token_info = token_response.json()

        # ใช้ access token เพื่อดึงข้อมูลผู้ใช้
        user_response = await client.get(
            "https://discord.com/api/users/@me",
            headers={
                "Authorization": f"Bearer {token_info['access_token']}"
            }
        )
        user_response.raise_for_status() # Raise an exception for HTTP errors
        user_data = user_response.json()
        return token_info, user_data

# ฟังก์ชัน Callback สำหรับหลังจากเล่นเสียงเสร็จสิ้น
async def _after_playback_cleanup(error, channel_id):
    """
    จัดการหลังจากเล่นเสียงเสร็จสิ้น, รวมถึงการจัดการข้อผิดพลาดและการเล่นเพลงถัดไปในคิว
    """
    if error:
        logging.error(f"ข้อผิดพลาดในการเล่นเสียง: {error}")
        channel = bot.get_channel(channel_id)
        if channel:
            await channel.send(f"❌ เกิดข้อผิดพลาดระหว่างเล่น: {error}")
    
    # พยายามเล่นเพลงถัดไปในคิว
    if queue and voice_client and voice_client.is_connected() and not voice_client.is_playing():
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
    global voice_client, queue, volume

    if not voice_client or not voice_client.is_connected():
        logging.warning("บอทไม่ได้อยู่ในช่องเสียงเพื่อเล่นเพลงในคิว.")
        return

    if voice_client.is_playing():
        voice_client.stop()

    if not queue:
        logging.info("คิวเพลงว่างเปล่า.")
        await channel.send("✅ เล่นเพลงในคิวทั้งหมดแล้ว!")
        return

    url_to_play = queue.pop(0) # ดึง URL ถัดไปจากคิว
    logging.info(f"พยายามเล่นจากคิว: {url_to_play}")
    
    # ตรวจสอบว่าเป็นลิงก์ YouTube/SoundCloud หรือไม่
    # yt-dlp รองรับหลายแพลตฟอร์มรวมถึง YouTube และ SoundCloud
    ydl_opts = {
        'format': 'bestaudio/best', 
        'default_search': 'ytsearch', 
        'source_address': '0.0.0.0', 
        'verbose': False, 
        'noplaylist': True # ไม่ดึงเพลย์ลิสต์ทั้งหมดโดยอัตโนมัติหากไม่ได้ระบุอย่างชัดเจน
    }

    try:
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(ydl_opts).extract_info(url_to_play, download=False))
        
        audio_url = None
        title = 'Unknown Title'

        if info.get('_type') == 'playlist':
            playlist_title = info.get('title', 'Unknown Playlist')
            await channel.send(f"🎶 เพิ่มเพลย์ลิสต์: **{playlist_title}** ลงในคิว...")
            
            # เพิ่มวิดีโอทั้งหมดในเพลย์ลิสต์ลงในคิว
            for entry in info.get('entries', []):
                if entry and entry.get('url'):
                    queue.append(entry['url'])
            
            # ดึงข้อมูลของวิดีโอแรกสุดจากเพลย์ลิสต์เพื่อเล่น
            selected_info = info.get('entries')[0] if info.get('entries') else None
            if not selected_info or not selected_info.get('url'):
                raise Exception("ไม่สามารถดึงวิดีโอแรกจากเพลย์ลิสต์ได้.")
            
            audio_url = selected_info['url']
            title = selected_info.get('title', 'Unknown Title')
        elif info.get('url'): 
            audio_url = info['url']
            title = info.get('title', 'Unknown Title')
        else:
            raise Exception("ไม่พบ URL เสียงที่สามารถเล่นได้.")
        
        # เตรียมแหล่งเสียง FFmpeg
        # ต้องแน่ใจว่า ffmpeg สามารถเข้าถึงได้ใน PATH หรือระบุ path เต็ม
        source = discord.FFmpegPCMAudio(audio_url, executable="ffmpeg")
        voice_client.play(source, after=lambda e: asyncio.run_coroutine_threadsafe(
            _after_playback_cleanup(e, channel.id), bot.loop))
        
        await channel.send(f"🎶 กำลังเล่น: **{title}**")

    except yt_dlp.utils.ExtractorError as e:
        error_message = str(e)
        if "Sign in to confirm you’re not a bot" in error_message or "requires login" in error_message or "age-restricted" in error_message or "unavailable in your country" in error_message:
            await channel.send(
                f"❌ ไม่สามารถเล่น **{url_to_play}** ได้: วิดีโอนี้อาจต้องเข้าสู่ระบบ, ถูกจำกัดอายุ, หรือไม่พร้อมใช้งานในภูมิภาคของคุณ โปรดลองใช้วิดีโอสาธารณะอื่น."
            )
            logging.warning(f"วิดีโอ YouTube/SoundCloud ถูกจำกัด: {url_to_play}")
        else:
            await channel.send(f"❌ เกิดข้อผิดพลาดที่ไม่คาดคิดในการเล่นสำหรับ {url_to_play}: {e}")
            logging.error(f"ข้อผิดพลาดในการเล่นรายการ {url_to_play}: {e}", exc_info=True)
        # พยายามเล่นเพลงถัดไปในคิวโดยอัตโนมัติหากปัจจุบันล้มเหลว
        if queue and voice_client and voice_client.is_connected():
            await asyncio.sleep(1) # หน่วงเวลาเล็กน้อยก่อนลองเล่นเพลงถัดไป
            await _play_next_in_queue(channel)
        elif not queue:
            await channel.send("✅ เล่นเพลงในคิวทั้งหมดแล้ว!")
    except Exception as e: 
        logging.error(f"ข้อผิดพลาดในการเล่นรายการ {url_to_play}: {e}", exc_info=True)
        await channel.send(f"❌ ไม่สามารถเล่น: {url_to_play} ได้ เกิดข้อผิดพลาด: {e}")
        if queue and voice_client and voice_client.is_connected():
            await asyncio.sleep(1)
            await _play_next_in_queue(channel)
        elif not queue:
            await channel.send("✅ เล่นเพลงในคิวทั้งหมดแล้ว!")

async def cleanup_audio(error, filename):
    """ล้างไฟล์เสียง TTS หลังจากเล่น"""
    if error:
        logging.error(f"ข้อผิดพลาดในการเล่น TTS: {error}")
    if os.path.exists(filename):
        os.remove(filename)
        logging.info(f"ล้างไฟล์เสียง: {filename}")


# --- Discord Bot Events ---
@bot.event
async def on_ready():
    """
    เหตุการณ์นี้จะถูกเรียกเมื่อบอทเชื่อมต่อกับ Discord API สำเร็จ
    """
    # โหลด Opus สำหรับฟังก์ชันเสียง
    if not discord.opus.is_loaded():
        try:
            # พยายามโหลดไลบรารี Opus
            # ตัวอย่าง: discord.opus.load_opus('libopus.so') # สำหรับ Linux
            # discord.opus.load_opus('opus.dll') # สำหรับ Windows
            # discord.opus.load_opus('libopus.dylib') # สำหรับ macOS
            # หากไม่ระบุ path และไฟล์อยู่ใน PATH ของระบบ จะหาเจอเอง
            discord.opus.load_opus() 
            logging.info("Opus โหลดสำเร็จ.")
        except Exception as e:
            logging.error(f"ไม่สามารถโหลด opus: {e} ได้ คำสั่งเสียงอาจไม่ทำงาน.")
            print(f"ไม่สามารถโหลด opus: {e} ได้ โปรดตรวจสอบให้แน่ใจว่าติดตั้งและเข้าถึงได้.")

    print(f"✅ บอทเข้าสู่ระบบในฐานะ {bot.user}")
    logging.info(f"บอทเข้าสู่ระบบในฐานะ {bot.user}")

    # ซิงค์คำสั่งทั่วโลกและไปยัง Guild เฉพาะสำหรับการอัปเดตที่รวดเร็วระหว่างการพัฒนา
    try:
        # ซิงค์คำสั่งทั่วโลก (อาจใช้เวลานานในการอัปเดตสำหรับผู้ใช้)
        await tree.sync() 
        logging.info("คำสั่งทั่วโลกซิงค์แล้ว.")
        
        # ซิงค์คำสั่งไปยัง Guild เฉพาะ (อัปเดตทันทีสำหรับ Guild ที่ระบุ)
        guild_obj = discord.Object(id=YOUR_GUILD_ID)
        await tree.sync(guild=guild_obj)
        logging.info(f"คำสั่งซิงค์กับ Guild: {YOUR_GUILD_ID}")
    except Exception as e:
        logging.error(f"ไม่สามารถซิงค์คำสั่ง: {e} ได้", exc_info=True)

    # โหลดข้อมูลผู้ใช้ทั้งหมดจาก Firestore เมื่อบอทเริ่มต้น
    await load_all_user_data_from_firestore()
    
    bot_ready.set() # ตั้งค่า Event เพื่อส่งสัญญาณว่าบอทพร้อมใช้งาน
    logging.info("บอทพร้อมใช้งานเต็มที่แล้ว.")

# --- Discord Slash Commands ---

@tree.command(name="join", description="เข้าร่วมช่องเสียงของคุณ")
async def join(interaction: discord.Interaction):
    """คำสั่งสำหรับบอทเข้าร่วมช่องเสียงที่ผู้ใช้กำลังอยู่"""
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
    """คำสั่งสำหรับบอทออกจากช่องเสียง"""
    global voice_client
    if voice_client and voice_client.is_connected():
        if voice_client.is_playing():
            voice_client.stop()
        await voice_client.disconnect()
        voice_client = None
        await interaction.response.send_message("✅ ออกจากช่องเสียงแล้ว", ephemeral=True)
    else:
        await interaction.response.send_message("❌ ไม่ได้อยู่ในช่องเสียง", ephemeral=True)

@tree.command(name="link_spotify", description="เชื่อมโยงบัญชี Spotify ของคุณ")
async def link_spotify(interaction: discord.Interaction):
    """คำสั่งสำหรับส่งลิงก์หน้าเว็บให้ผู้ใช้เชื่อมโยง Spotify"""
    await interaction.response.send_message(
        f"🔗 ในการเชื่อมโยงบัญชี Spotify ของคุณ โปรดไปที่:\n"
        f"**{url_for('index', _external=True)}**\n"
        f"เข้าสู่ระบบด้วย Discord ก่อน จากนั้นจึงเชื่อมต่อ Spotify", 
        ephemeral=True
    )

@tree.command(name="play", description="เล่นเพลงจาก Spotify")
@app_commands.describe(query="ชื่อเพลง, ศิลปิน, หรือลิงก์ Spotify (เพลง, เพลย์ลิสต์, อัลบั้ม)")
async def play(interaction: discord.Interaction, query: str):
    """
    คำสั่งสำหรับเล่นเพลง, เพลย์ลิสต์ หรืออัลบั้มจาก Spotify
    รองรับการค้นหาด้วยชื่อหรือลิงก์ Spotify โดยตรง
    """
    sp_user = get_user_spotify_client(interaction.user.id)
    if not sp_user:
        await interaction.response.send_message(
            "❌ กรุณาเชื่อมโยงบัญชี Spotify ของคุณก่อนโดยใช้ /link_spotify", 
            ephemeral=True
        )
        return

    await interaction.response.defer() # หน่วงการตอบสนองเนื่องจากการเรียก Spotify API อาจใช้เวลา

    try:
        track_uris = []
        context_uri = None
        response_msg = "🎶"

        # ตรวจสอบว่าเป็นลิงก์ Spotify (เพลง, เพลย์ลิสต์, หรืออัลบั้ม) หรือไม่
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
        else:  # ค้นหาด้วยชื่อถ้าไม่ใช่ลิงก์โดยตรง
            results = await asyncio.to_thread(sp_user.search, q=query, type='track', limit=1)
            if not results['tracks']['items']:
                await interaction.followup.send("❌ ไม่พบเพลงบน Spotify")
                return
            track = results['tracks']['items'][0]
            track_uris.append(track['uri'])
            response_msg += f" กำลังเล่น: **{track['name']}** โดย **{track['artists'][0]['name']}**"

        # ดึงอุปกรณ์ที่ใช้งานอยู่เพื่อเล่นเพลง
        devices = await asyncio.to_thread(sp_user.devices)
        active_device_id = None
        for device in devices['devices']:
            if device['is_active']:
                active_device_id = device['id']
                break
        
        if not active_device_id:
            await interaction.followup.send("❌ ไม่พบ Spotify client ที่ใช้งานอยู่ กรุณาเปิดแอป Spotify ของคุณและเล่นเพลงอะไรก็ได้ที่นั่นก่อน หรือเลือกอุปกรณ์สำหรับเล่นใน Spotify.")
            return

        # เริ่มเล่นเพลงบนอุปกรณ์ที่ใช้งานอยู่
        if context_uri: # สำหรับเพลย์ลิสต์และอัลบั้ม
            await asyncio.to_thread(sp_user.start_playback, device_id=active_device_id, context_uri=context_uri)
        else: # สำหรับเพลงเดี่ยว
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
        elif e.http_status == 403: # ข้อผิดพลาด Forbidden มักเกี่ยวข้องกับ Premium หรือข้อจำกัดการเล่น
            await interaction.followup.send("❌ ข้อผิดพลาดในการเล่น Spotify: คุณอาจต้องมีบัญชี Spotify Premium หรือมีข้อจำกัดในการเล่น.")
        else:
            await interaction.followup.send(f"❌ ข้อผิดพลาด Spotify: {e}. โปรดลองอีกครั้ง.")
        logging.error(f"ข้อผิดพลาด Spotify สำหรับผู้ใช้ {interaction.user.id}: {e}", exc_info=True)
    except Exception as e:
        await interaction.followup.send(f"❌ เกิดข้อผิดพลาดที่ไม่คาดคิด: {e}")
        logging.error(f"ข้อผิดพลาดที่ไม่คาดคิดในคำสั่ง play: {e}", exc_info=True)

@tree.command(name="random_name", description="สุ่มเลือกชื่อจากรายการที่ให้มา")
@app_commands.describe(names="ชื่อหรือรายการที่คั่นด้วยเครื่องหมายจุลภาค (เช่น John, Doe, Alice)")
async def random_name(interaction: discord.Interaction, names: str):
    """คำสั่งสำหรับสุ่มเลือกชื่อจากรายการที่ผู้ใช้ป้อน"""
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

# --- คลาสระบบโพลล์ (Poll System Class) ---
class PollView(discord.ui.View):
    """
    View สำหรับจัดการการโต้ตอบปุ่มของระบบโพลล์
    """
    def __init__(self, poll_id, question, options):
        super().__init__(timeout=None) # คงโพลล์ให้ทำงานไปเรื่อยๆ จนกว่าจะถูกลบหรือหมดอายุโดย Discord
        self.poll_id = poll_id
        self.question = question
        self.options = options
        
        # เริ่มต้นโครงสร้างข้อมูลโหวตสำหรับโพลล์นี้ หากยังไม่มี
        if poll_id not in active_polls:
            active_polls[poll_id] = {
                "question": question,
                "options": options,
                "votes": {option: set() for option in options} # ใช้ set เพื่อเก็บ user ID ป้องกันการโหวตซ้ำ
            }
        
        # เพิ่มปุ่มสำหรับแต่ละตัวเลือกแบบไดนามิก
        for i, option in enumerate(options):
            button = discord.ui.Button(label=option, custom_id=f"poll_{poll_id}_{i}", style=discord.ButtonStyle.primary)
            button.callback = self._button_callback # กำหนด callback เฉพาะสำหรับปุ่มนี้
            self.add_item(button)

        # เพิ่มปุ่ม "แสดงผลลัพธ์"
        show_results_button_item = discord.ui.Button(label="แสดงผลลัพธ์", style=discord.ButtonStyle.secondary, custom_id=f"poll_show_results_{poll_id}")
        show_results_button_item.callback = self.show_results_button 
        self.add_item(show_results_button_item)

    async def on_timeout(self):
        """เหตุการณ์นี้จะถูกเรียกเมื่อ View หมดเวลา (ถ้ามีการตั้ง timeout)"""
        logging.info(f"Poll {self.poll_id} หมดเวลา.")
        # สำหรับโพลล์ที่ไม่สิ้นสุด อาจไม่ถึงตรงนี้เว้นแต่จะหยุดอย่างชัดเจน
        # คุณอาจต้องการปิดใช้งานปุ่มหรือลบข้อมูลโพลล์ที่นี่
        # self.clear_items()
        # await self.message.edit(view=self) 
        # if self.poll_id in active_polls:
        #     del active_polls[self.poll_id]

    # Callback สำหรับปุ่ม "แสดงผลลัพธ์"
    @discord.ui.button(label="แสดงผลลัพธ์", style=discord.ButtonStyle.secondary, custom_id="show_results_placeholder") # custom_id ที่นี่เป็นเพียง placeholder
    async def show_results_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """จัดการการคลิกปุ่ม 'แสดงผลลัพธ์'"""
        expected_custom_id = f"poll_show_results_{self.poll_id}"
        if button.custom_id != expected_custom_id:
            await interaction.response.send_message("❌ ปุ่ม 'แสดงผลลัพธ์' นี้ไม่ได้เชื่อมโยงกับโพลล์ที่ใช้งานอยู่.", ephemeral=True)
            return

        # อัปเดตข้อความโพลล์เพื่อแสดงผลลัพธ์ล่าสุด
        await self.update_poll_message(interaction.message)
        await interaction.response.defer() # ยืนยันการกดปุ่มโดยไม่ต้องส่งข้อความใหม่


    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """ตรวจสอบสิทธิ์ในการโต้ตอบกับปุ่ม (ทุกคนสามารถโต้ตอบได้)"""
        return True 

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item):
        """จัดการข้อผิดพลาดที่เกิดขึ้นระหว่างการโต้ตอบปุ่ม"""
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
        """Callback สำหรับปุ่มตัวเลือกโพลล์"""
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

        # ลบการโหวตก่อนหน้าของผู้ใช้จากตัวเลือกใดๆ (หากผู้ใช้สามารถโหวตได้เพียงตัวเลือกเดียว)
        user_changed_vote = False
        for option_key, voters_set in poll_data['votes'].items():
            if user_id in voters_set and option_key != selected_option:
                voters_set.remove(user_id)
                user_changed_vote = True
                logging.info(f"ผู้ใช้ {user_id} ลบการโหวตจาก {option_key} ในโพลล์ {poll_id}")
                break
        
        # เพิ่มการโหวตใหม่
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
    """คำสั่งสำหรับสร้างโพลล์ใหม่พร้อมปุ่มโหวต"""
    option_list = [opt.strip() for opt in options.split(',') if opt.strip()]

    if not option_list:
        await interaction.response.send_message("❌ โปรดระบุตัวเลือกอย่างน้อยหนึ่งตัวเลือกสำหรับโพลล์", ephemeral=True)
        return
    
    if len(option_list) > 25: # Discord จำกัดปุ่มไว้ที่ 5 แถว แถวละ 5 ปุ่ม (รวม 25 ปุ่ม)
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

    # ส่งข้อความโดยไม่มี View ก่อนเพื่อดึง Message ID
    message = await interaction.followup.send(embed=embed)
    
    # เก็บข้อมูลโพลล์ทันทีที่ Message ID พร้อมใช้งาน
    active_polls[message.id] = {
        "question": question,
        "options": option_list,
        "votes": {option: set() for option in option_list}
    }

    # สร้าง Instance ของ PollView และแนบไปกับข้อความ
    poll_view = PollView(message.id, question, option_list)
    await message.edit(view=poll_view) 
    logging.info(f"โพลล์สร้างโดย {interaction.user.display_name}: ID {message.id}, คำถาม: {question}, ตัวเลือก: {options}")


@tree.command(name="pause", description="หยุดเล่น Spotify ชั่วคราว")
async def pause_spotify(interaction: discord.Interaction):
    """คำสั่งสำหรับหยุดเล่นเพลง Spotify ชั่วคราว"""
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
    """คำสั่งสำหรับเล่นเพลง Spotify ต่อจากที่หยุดไว้"""
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
    """คำสั่งสำหรับข้ามเพลง Spotify ปัจจุบัน"""
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

@tree.command(name="previous", description="เล่นเพลงก่อนหน้าบน Spotify")
async def previous_spotify(interaction: discord.Interaction):
    """คำสั่งสำหรับเล่นเพลงก่อนหน้าบน Spotify"""
    sp_user = get_user_spotify_client(interaction.user.id)
    if not sp_user:
        await interaction.response.send_message("❌ กรุณาเชื่อมโยงบัญชี Spotify ของคุณก่อนโดยใช้ /link_spotify", ephemeral=True)
        return
    
    try:
        await asyncio.to_thread(sp_user.previous_track)
        await interaction.response.send_message("⏮️ เล่นเพลงก่อนหน้าแล้ว", ephemeral=True)
    except spotipy.exceptions.SpotifyException as e:
        await interaction.response.send_message(f"❌ ข้อผิดพลาดในการเล่นเพลงก่อนหน้าบน Spotify: {e}", ephemeral=True)
        logging.error(f"ข้อผิดพลาดในการเล่นเพลงก่อนหน้าบน Spotify สำหรับผู้ใช้ {interaction.user.id}: {e}", exc_info=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ เกิดข้อผิดพลาดที่ไม่คาดคิด: {e}", ephemeral=True)
        logging.error(f"ข้อผิดพลาดที่ไม่คาดคิดในคำสั่ง previous: {e}", exc_info=True)

@tree.command(name="speak", description="ให้บอทพูดในช่องเสียง")
@app_commands.describe(message="ข้อความที่จะให้บอทพูด")
@app_commands.describe(lang="ภาษา (เช่น 'en', 'th')")
async def speak(interaction: discord.Interaction, message: str, lang: str = 'en'):
    """คำสั่งสำหรับให้บอทพูดข้อความที่ระบุในช่องเสียง (TTS)"""
    global voice_client
    if not voice_client or not voice_client.is_connected():
        await interaction.response.send_message("❌ บอทไม่ได้อยู่ในช่องเสียง. ใช้ /join ก่อน", ephemeral=True)
        return
    
    await interaction.response.defer() 
    try:
        tts_filename = f"tts_discord_{interaction.id}.mp3"
        await asyncio.to_thread(gTTS(message, lang=lang).save, tts_filename) 
        
        source = discord.FFmpegPCMAudio(tts_filename, executable="ffmpeg")
        voice_client.play(source, after=lambda e: asyncio.create_task(cleanup_audio(e, tts_filename))) 
        
        await interaction.followup.send(f"🗣️ กำลังพูด: **{message}** (ภาษา: {lang})")

    except Exception as e:
        await interaction.followup.send(f"❌ เกิดข้อผิดพลาดในการพูด: {e}")
        logging.error(f"ข้อผิดพลาด TTS: {e}", exc_info=True)

@tree.command(name="wake", description="ปลุกผู้ใช้ด้วย DM")
@app_commands.describe(user="เลือกผู้ใช้")
async def wake(interaction: discord.Interaction, user: discord.User):
    """คำสั่งสำหรับส่งข้อความ DM ไปปลุกผู้ใช้"""
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


# --- Flask Routes (Web Interface) ---
@app.route("/")
async def index(): 
    """หน้าแรกของเว็บอินเตอร์เฟซ แสดงสถานะการเชื่อมต่อ Discord และ Spotify"""
    current_session_id = session.get('session_id')
    if not current_session_id:
        current_session_id = os.urandom(16).hex()
        session['session_id'] = current_session_id

    discord_user_id = web_logged_in_users.get(current_session_id)
    is_discord_linked = bool(discord_user_id) 

    is_spotify_linked = False

    # ตรวจสอบสถานะการเชื่อมโยง Spotify หากเชื่อมโยง Discord แล้ว
    if discord_user_id:
        try:
            is_spotify_linked = await _check_spotify_link_status(discord_user_id)
        except Exception as e:
            logging.error(f"ข้อผิดพลาดในการตรวจสอบสถานะการเชื่อมโยง Spotify สำหรับผู้ใช้เว็บ {discord_user_id}: {e}")
            is_spotify_linked = False 

    return render_template(
        "index.html",
        is_discord_linked=is_discord_linked,
        discord_user_id=discord_user_id,
        is_spotify_linked=is_spotify_linked
    )

@app.route("/api/auth_status")
async def get_auth_status():
    """API endpoint เพื่อดึงสถานะการเชื่อมโยง Discord และ Spotify"""
    current_session_id = session.get('session_id')
    discord_user_id = web_logged_in_users.get(current_session_id)
    is_discord_linked = bool(discord_user_id)
    is_spotify_linked = False

    if is_discord_linked:
        try:
            is_spotify_linked = await _check_spotify_link_status(discord_user_id)
        except Exception as e:
            logging.error(f"ข้อผิดพลาดในการตรวจสอบสถานะ Spotify สำหรับ API: {e}")
            is_spotify_linked = False

    return jsonify({
        "is_discord_linked": is_discord_linked,
        "is_spotify_linked": is_spotify_linked
    })

@app.route("/api/discord_user_id")
def get_discord_user_id_api():
    """API endpoint เพื่อดึง Discord User ID สำหรับเซสชันปัจจุบัน"""
    current_session_id = session.get('session_id')
    discord_user_id = web_logged_in_users.get(current_session_id)
    return jsonify({"discord_user_id": discord_user_id})


@app.route("/login/discord")
def login_discord():
    """Redirect ไปยังหน้า Discord OAuth เพื่อเข้าสู่ระบบ Discord"""
    discord_auth_url = (
        f"https://discord.com/api/oauth2/authorize"
        f"?client_id={DISCORD_CLIENT_ID}"
        f"&redirect_uri={DISCORD_REDIRECT_URI}"
        f"&response_type=code"
        f"&scope={'+'.join(DISCORD_OAUTH_SCOPES.split(' '))}"
    )
    return redirect(discord_auth_url)

@app.route("/callback/discord")
def discord_callback():
    """Callback URL สำหรับ Discord OAuth หลังจากผู้ใช้อนุญาตหรือปฏิเสธ"""
    code = request.args.get("code")
    error = request.args.get("error")
    current_session_id = session.get('session_id') 

    if error:
        flash(f"❌ Discord OAuth error: {error}", "error")
        return redirect(url_for("index"))

    if not code:
        flash("❌ No authorization code received", "error")
        return redirect(url_for("index"))

    try:
        # รันฟังก์ชัน async ในเธรดแยกโดยใช้ asyncio.run_coroutine_threadsafe
        future = asyncio.run_coroutine_threadsafe(
            _fetch_discord_token_and_user(code),
            bot.loop
        )
        token_info, user_data = future.result(timeout=10) # รอผลลัพธ์สูงสุด 10 วินาที
        
        discord_user_id = int(user_data["id"])
        discord_username = user_data["username"]

        if not current_session_id:
            current_session_id = os.urandom(16).hex()
            session['session_id'] = current_session_id

        # เพิ่ม Flask session ID เข้าไปใน Firestore ของผู้ใช้
        asyncio.run_coroutine_threadsafe(
            update_user_data_in_firestore(discord_user_id, flask_session_to_add=current_session_id),
            bot.loop
        ).result()

        # อัปเดต web_logged_in_users ในหน่วยความจำ
        web_logged_in_users[current_session_id] = discord_user_id
        session['discord_user_id_for_web'] = discord_user_id # ใช้เก็บใน Flask session ด้วย

        flash(f"✅ Discord login successful: {discord_username}", "success")

    except Exception as e:
        flash(f"❌ Error during Discord login: {e}", "error")
        logging.error(f"Discord OAuth error: {e}", exc_info=True)
    
    return redirect(url_for("index")) 

@app.route("/login/spotify/<int:discord_user_id_param>")
def login_spotify_web(discord_user_id_param: int):
    """
    Redirect ไปยังหน้า Spotify OAuth เพื่อเข้าสู่ระบบ Spotify
    มีการตรวจสอบว่า Discord User ID ที่ส่งมาตรงกับที่เข้าสู่ระบบปัจจุบันหรือไม่
    """
    current_session_id = session.get('session_id')
    logged_in_discord_user_id = web_logged_in_users.get(current_session_id)

    # ป้องกันการเชื่อมโยง Spotify ให้กับ Discord User ID ที่ไม่ตรงกับที่เข้าสู่ระบบ
    if logged_in_discord_user_id != discord_user_id_param:
        flash("❌ Discord User ID mismatch. Please login with Discord again.", "error")
        return redirect(url_for("index"))

    auth_manager = SpotifyOAuth(
        client_id=SPOTIPY_CLIENT_ID,
        client_secret=SPOTIPY_CLIENT_SECRET,
        redirect_uri=SPOTIPY_REDIRECT_URI,
        scope=SPOTIPY_SCOPES,
        show_dialog=True # บังคับให้ผู้ใช้อนุญาตเสมอ
    )
    auth_url = auth_manager.get_authorize_url()
    
    # เก็บ Discord User ID ไว้ใน session เพื่อใช้ใน callback
    session['spotify_auth_discord_user_id'] = discord_user_id_param
    return redirect(auth_url)

@app.route("/callback/spotify")
def spotify_callback():
    """Callback URL สำหรับ Spotify OAuth หลังจากผู้ใช้อนุญาตหรือปฏิเสธ"""
    code = request.args.get("code")
    error = request.args.get("error")
    discord_user_id = session.pop('spotify_auth_discord_user_id', None) # ดึง Discord User ID ออกจาก session
    
    if error:
        flash(f"❌ Spotify OAuth error: {error}", "error")
        return redirect(url_for("index"))

    if not code or not discord_user_id:
        flash("❌ Authorization code or Discord user ID for Spotify linking is missing. Please try again.", "error")
        return redirect(url_for("index"))

    try:
        auth_manager = SpotifyOAuth(
            client_id=SPOTIPY_CLIENT_ID,
            client_secret=SPOTIPY_CLIENT_SECRET,
            redirect_uri=SPOTIPY_REDIRECT_URI,
            scope=SPOTIPY_SCOPES,
        )

        future = asyncio.run_coroutine_threadsafe(
            asyncio.to_thread(auth_manager.get_access_token, code),
            bot.loop
        )
        token_info = future.result(timeout=10)

        sp_user = spotipy.Spotify(auth_manager=auth_manager)
        spotify_users[discord_user_id] = sp_user # เก็บ Spotify client ในแคช

        # บันทึก Spotify token info ลง Firestore
        asyncio.run_coroutine_threadsafe(
            update_user_data_in_firestore(discord_user_id, spotify_token_info=token_info),
            bot.loop
        ).result() # รอให้การอัปเดต Firestore เสร็จสมบูรณ์

        flash("✅ Spotify linked successfully!", "success")
        
    except Exception as e:
        flash(f"❌ Error linking Spotify: {e}. Please ensure your redirect URI is correct in Spotify Developer Dashboard.", "error")
        logging.error(f"Spotify callback error for user {discord_user_id}: {e}", exc_info=True)
    
    return redirect(url_for("index"))

# --- Flask routes for controlling bot from web ---
@app.route("/web_control/add", methods=["POST"])
def add_web_queue():
    """เพิ่ม URL เพลงลงในคิวของบอท Discord (สำหรับ YouTube/SoundCloud)"""
    url = request.form.get("url")
    if url:
        queue.append(url)
        flash(f"Added to queue: {url}", "info")
        logging.info(f"Added to queue from web: {url}")
    else:
        flash("No URL provided to add to queue.", "error")
    return redirect(url_for("index"))

@app.route("/web_control/play")
def play_web_control():
    """สั่งให้บอทเริ่มเล่นเพลงถัดไปในคิว (สำหรับ YouTube/SoundCloud)"""
    if not bot_ready.is_set():
        flash("Bot is not ready yet. Please wait a moment.", "warning")
        return redirect("/")

    # ตรวจสอบว่าบอทอยู่ในช่องเสียงและไม่ได้กำลังเล่นอยู่
    if voice_client and not voice_client.is_playing():
        if voice_client.channel: # ตรวจสอบว่า channel object มีอยู่จริง
            asyncio.run_coroutine_threadsafe(
                _play_next_in_queue(bot.get_channel(voice_client.channel.id)),
                bot.loop
            )
            flash("Attempting to play next in queue.", "info")
            logging.info("Triggered play via web.")
        else:
            flash("Bot is in a voice channel but channel object is unavailable.", "error")
    else:
        flash("Bot is not in a voice channel or is already playing.", "warning")
    return redirect("/")

@app.route("/web_control/pause")
def pause_web_control():
    """สั่งให้บอทหยุดเล่นเพลงชั่วคราว (สำหรับ YouTube/SoundCloud)"""
    if voice_client and voice_client.is_playing():
        voice_client.pause()
        flash("Playback paused.", "info")
        logging.info("Paused via web.")
    else:
        flash("Nothing to pause.", "warning")
    return redirect("/")

@app.route("/web_control/resume")
def resume_web_control():
    """สั่งให้บอทเล่นเพลงต่อจากที่หยุดไว้ (สำหรับ YouTube/SoundCloud)"""
    if voice_client and voice_client.is_paused():
        voice_client.resume()
        flash("Playback resumed.", "info")
        logging.info("Resumed via web.")
    else:
        flash("Nothing to resume.", "warning")
    return redirect("/")

@app.route("/web_control/stop")
def stop_web_control():
    """สั่งให้บอทหยุดเล่นเพลงและล้างคิวทั้งหมด (สำหรับ YouTube/SoundCloud)"""
    global queue
    queue.clear() 
    if voice_client and (voice_client.is_playing() or voice_client.is_paused()):
        voice_client.stop() 
        flash("Playback stopped and queue cleared.", "info")
        logging.info("Stopped via web and cleared queue.")
    else:
        flash("Nothing to stop.", "warning")
    return redirect("/")

@app.route("/web_control/skip")
def skip_web_control():
    """สั่งให้ Spotify ข้ามเพลงปัจจุบัน"""
    current_session_id = session.get('session_id')
    discord_user_id = web_logged_in_users.get(current_session_id)
    
    if not discord_user_id:
        flash("Please login with Discord first to control Spotify playback.", "error")
        return redirect("/")

    sp_user = get_user_spotify_client(discord_user_id)
    if not sp_user:
        flash("Your Spotify is not linked or token expired. Please re-link.", "error")
        return redirect("/")

    try:
        # ดำเนินการเรียก Spotify API ใน bot's event loop
        asyncio.run_coroutine_threadsafe(
            asyncio.to_thread(sp_user.next_track),
            bot.loop
        ).result()
        flash("Spotify track skipped.", "info")
        logging.info("Spotify track skipped via web.")
    except spotipy.exceptions.SpotifyException as e:
        flash(f"Error skipping Spotify track: {e}", "error")
        logging.error(f"Error skipping Spotify track via web for user {discord_user_id}: {e}", exc_info=True)
    except Exception as e:
        flash(f"An unexpected error occurred while skipping: {e}", "error")
        logging.error(f"Unexpected error skipping Spotify track via web for user {discord_user_id}: {e}", exc_info=True)
    return redirect("/")

@app.route("/web_control/previous")
def prev_spotify_web_control():
    """สั่งให้ Spotify เล่นเพลงก่อนหน้า"""
    current_session_id = session.get('session_id')
    discord_user_id = web_logged_in_users.get(current_session_id)
    
    if not discord_user_id:
        flash("Please login with Discord first to control Spotify playback.", "error")
        return redirect("/")

    sp_user = get_user_spotify_client(discord_user_id)
    if not sp_user:
        flash("Your Spotify is not linked or token expired. Please re-link.", "error")
        return redirect("/")

    try:
        # ดำเนินการเรียก Spotify API ใน bot's event loop
        asyncio.run_coroutine_threadsafe(
            asyncio.to_thread(sp_user.previous_track),
            bot.loop
        ).result()
        flash("Spotify track changed to previous.", "info")
        logging.info("Spotify track changed to previous via web.")
    except spotipy.exceptions.SpotifyException as e:
        flash(f"Error going to previous Spotify track: {e}", "error")
        logging.error(f"Error going to previous Spotify track via web for user {discord_user_id}: {e}", exc_info=True)
    except Exception as e:
        flash(f"An unexpected error occurred while going to previous track: {e}", "error")
        logging.error(f"Unexpected error going to previous Spotify track via web for user {discord_user_id}: {e}", exc_info=True)
    return redirect("/")

@app.route("/web_control/volume_up")
def volume_up_web_control():
    """เพิ่มระดับเสียงของบอท Discord (สำหรับ YouTube/SoundCloud)"""
    global volume
    volume = min(volume + 0.1, 2.0) # ระดับเสียงสูงสุด 2.0 (200%)
    if voice_client and voice_client.source: 
        voice_client.source.volume = volume
    flash(f"Volume increased to {volume*100:.0f}%", "info")
    logging.info(f"Volume up: {volume}")
    return redirect("/")

@app.route("/web_control/volume_down")
def volume_down_web_control():
    """ลดระดับเสียงของบอท Discord (สำหรับ YouTube/SoundCloud)"""
    global volume
    volume = max(volume - 0.1, 0.1) # ระดับเสียงต่ำสุด 0.1 (10%) เพื่อไม่ให้เงียบสนิท
    if voice_client and voice_client.source:
        voice_client.source.volume = volume
    flash(f"Volume decreased to {volume*100:.0f}%", "info")
    logging.info(f"Volume down: {volume}")
    return redirect("/")

# --- Run Flask + Discord bot ---
def run_web():
    """ฟังก์ชันสำหรับรัน Flask web server ในเธรดแยก"""
    # Flask app ควรจะรันในเธรดของตัวเอง
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)

if __name__ == "__main__":
    print("\n--- Initializing Bot and Web Server ---")
    print("Ensure FFmpeg and Opus are installed for voice functions.")
    print("---------------------------------------\n")

    # เริ่ม Flask web server ในเธรดแยก
    web_thread = threading.Thread(target=run_web)
    web_thread.start()
    
    # รัน Discord bot (นี่เป็นการเรียกแบบบล็อก)
    # bot.run() ควรเป็นคำสั่งสุดท้ายใน main thread
    bot.run(DISCORD_TOKEN)
