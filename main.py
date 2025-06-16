import discord
from discord.ext import commands
from discord import app_commands
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from gtts import gTTS # ยังคง import ไว้หากต้องการใช้ในอนาคต แต่ไม่ได้ใช้ในการพูดโดยตรงในระบบเสียงปัจจุบัน
import os
import threading
import logging
import json
import base64 # สำหรับการถอดรหัสข้อมูลรับรอง
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
from firebase_admin import exceptions as firebase_exceptions # สำหรับดักจับข้อผิดพลาด Firebase

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
# ตรวจสอบให้แน่ใจว่า GUILD_ID ในไฟล์ .env เป็นตัวเลขล้วนๆ เพื่อหลีกเลี่ยง ValueError.
# เพิ่ม .strip().split('#')[0] เพื่อจัดการคอมเมนต์หรือช่องว่างในไฟล์ .env
YOUR_GUILD_ID = int(os.environ["GUILD_ID"].strip().split('#')[0]) 
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
DISCORD_REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI")
DISCORD_OAUTH_SCOPES = "identify guilds"

# --- Firebase Setup ---
firebase_credentials_base64 = os.getenv("FIREBASE_CREDENTIALS_BASE64")
db = None # กำหนด db เป็น None เริ่มต้น
if not firebase_credentials_base64:
    logging.error("FIREBASE_CREDENTIALS_BASE64 environment variable not set. Firestore will not work.")
    # ไม่ exit(1) ที่นี่ เพื่อให้บอทยังคงทำงานได้แม้ไม่มี Firestore
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
        # ไม่ exit(1) ที่นี่เช่นกัน
        db = None # ตั้งค่า db เป็น None หากเริ่มต้นล้มเหลว

# --- ตัวแปร Global ---
spotify_users = {}  # Key: Discord User ID, Value: Spotify client
web_logged_in_users = {}  # Key: Flask Session ID, Value: Discord User ID
voice_client = None # Object สำหรับการจัดการการเชื่อมต่อช่องเสียงของ Discord
queue = []  # คิวเพลงสำหรับเล่น (รองรับ YouTube/SoundCloud URL)
volume = 1.0 # ระดับเสียงเริ่มต้น

# --- ตัวแปร Global สำหรับระบบโพลล์ ---
# Key: poll_message_id, Value: {"question": str, "options": list[str], "votes": {option_str: set[user_id]}}
active_polls = {}

# --- ตั้งค่าการบันทึก Log ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s:%(levelname)s:%(message)s',
    handlers=[
        logging.FileHandler("bot.log"), # บันทึก Log ลงไฟล์
        logging.StreamHandler() # แสดง Log ในคอนโซล
    ]
)

# --- ตั้งค่า Discord Bot ---
intents = discord.Intents.default() # ดึงค่า Intent เริ่มต้นทั้งหมด
intents.message_content = True # จำเป็นสำหรับการอ่านเนื้อหาข้อความ
intents.voice_states = True # จำเป็นเพื่อให้บอทเข้าร่วม/ออกจากช่องเสียงได้
intents.members = True # จำเป็นสำหรับการดึงข้อมูลสมาชิกสำหรับโพลล์ (ถ้าจำเป็น)

bot = commands.Bot(command_prefix="!", intents=intents) # สร้าง Instance ของบอท
tree = bot.tree # สำหรับการจัดการ Slash Commands
bot_ready = asyncio.Event() # Event สำหรับส่งสัญญาณเมื่อบอทพร้อมใช้งานเต็มที่

# --- ตั้งค่า Flask App ---
app = Flask(__name__, static_folder="static", template_folder="templates") # สร้าง Instance ของ Flask App
# สร้างคีย์ลับที่ปลอดภัยหากไม่ได้ระบุในตัวแปรสภาพแวดล้อม
app.secret_key = os.getenv("FLASK_SECRET_KEY") or os.urandom(24) 

# --- ฟังก์ชันช่วย ---
def get_user_spotify_client(discord_user_id: int):
    """
    ดึง Spotify client สำหรับผู้ใช้ Discord
    ตรวจสอบการหมดอายุของโทเค็นและลบออกหากไม่ถูกต้อง
    """
    sp_client = spotify_users.get(discord_user_id)
    if sp_client:
        try:
            # ทดสอบว่าโทเค็นยังใช้ได้หรือไม่
            sp_client.current_user() 
            return sp_client
        except spotipy.exceptions.SpotifyException as e:
            logging.warning(f"Spotify token expired for user {discord_user_id}: {e}")
            # ถ้าโทเค็นหมดอายุ ให้ลบออกจากแคชของเรา
            if discord_user_id in spotify_users:
                del spotify_users[discord_user_id]
                # ไม่เรียก save_spotify_tokens() ที่นี่ เพราะจะทำผ่าน Firestore แล้ว
            return None
    return None

async def update_user_data_in_firestore(discord_user_id: int, spotify_token_info: dict = None, flask_session_to_add: str = None, flask_session_to_remove: str = None):
    """
    อัปเดตข้อมูลผู้ใช้ใน Firestore รวมถึงโทเค็น Spotify และเซสชัน Flask
    :param discord_user_id: ID ผู้ใช้ Discord (ใช้เป็น document ID)
    :param spotify_token_info: dict ข้อมูลโทเค็นจาก Spotipy (เป็นทางเลือก)
    :param flask_session_to_add: Flask session ID ที่จะเพิ่ม (เป็นทางเลือก)
    :param flask_session_to_remove: Flask session ID ที่จะลบ (เป็นทางเลือก)
    """
    if db is None:
        logging.error("Firestore DB is not initialized. Cannot update user data.")
        return

    user_ref = db.collection('users').document(str(discord_user_id))
    user_data_to_update = {}

    # ดึงข้อมูลผู้ใช้ปัจจุบันเพื่อจัดการ array
    try:
        doc = await asyncio.to_thread(user_ref.get)
        current_data = doc.to_dict() if doc.exists else {}
    except firebase_exceptions.FirebaseError as e:
        logging.error(f"Error fetching user {discord_user_id} data from Firestore: {e}", exc_info=True)
        return

    if spotify_token_info:
        user_data_to_update['spotify_token_info'] = spotify_token_info
    
    # จัดการ Flask sessions
    flask_sessions = set(current_data.get('flask_sessions', [])) # ใช้ set เพื่อจัดการค่าซ้ำ

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
    โหลดข้อมูลผู้ใช้ทั้งหมด (โทเค็น Spotify, เซสชัน Flask) จาก Firestore เข้าสู่ตัวแปร global.
    """
    global spotify_users, web_logged_in_users # จำเป็นต้องแก้ไขตัวแปร global เหล่านี้

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
                    # ตัวเลือก: คุณสามารถลบออกจาก Firestore ที่นี่ด้วยก็ได้หากไม่ถูกต้องอย่างสม่ำเสมอ
                    # await asyncio.to_thread(db.collection('users').document(str(user_id)).update({'spotify_token_info': firestore.DELETE_FIELD}))
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
    if error:
        logging.error(f"ข้อผิดพลาดในการเล่นเสียง: {error}")
        # สามารถส่งข้อความแสดงข้อผิดพลาดไปยังช่อง Discord ได้
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
    """เล่นเพลงถัดไปในคิว รองรับ URL ของ YouTube"""
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
    
    # ตรวจสอบว่า URL เป็นลิงก์ YouTube หรือไม่
    if "youtube.com/" in url_to_play or "youtu.be/" in url_to_play:
        ydl_opts = {
            'format': 'bestaudio/best', # เลือกรูปแบบเสียงที่ดีที่สุด
            'default_search': 'ytsearch', # หากเป็นแค่ชื่อ ให้ค้นหาใน YouTube
            'source_address': '0.0.0.0', # แก้ปัญหาบนบางระบบ Linux
            'verbose': False, # ตั้งค่าเป็น True สำหรับการดีบักผลลัพธ์ของ yt-dlp
        }

        try:
            loop = asyncio.get_event_loop()
            info = await loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(ydl_opts).extract_info(url_to_play, download=False))
            
            audio_url = None
            title = 'Unknown Title'

            # ตรวจสอบว่าเป็นเพลย์ลิสต์หรือไม่
            if info.get('_type') == 'playlist':
                playlist_title = info.get('title', 'Unknown Playlist')
                await channel.send(f"🎶 เพิ่มเพลย์ลิสต์: **{playlist_title}** ลงในคิว...")
                
                # เพิ่มวิดีโอทั้งหมดในเพลย์ลิสต์ลงในคิว
                # วิดีโอแรกจะถูกเล่นทันที วิดีโอที่เหลือจะถูกต่อคิว
                for entry in info.get('entries', []):
                    if entry and entry.get('url'):
                        # ตรวจสอบไม่ให้เพิ่มวิดีโอปัจจุบันซ้ำ
                        if entry['url'] != url_to_play: # Avoid adding the currently playing URL again if it was the first playlist item
                            queue.append(entry['url'])
                
                # ดึงข้อมูลของวิดีโอแรกสุดจากเพลย์ลิสต์เพื่อเล่น
                selected_info = info.get('entries')[0] if info.get('entries') else None
                if not selected_info or not selected_info.get('url'):
                    raise Exception("ไม่สามารถดึงวิดีโอแรกจากเพลย์ลิสต์ได้.")
                
                audio_url = selected_info['url']
                title = selected_info.get('title', 'Unknown Title')
            elif info.get('url'): # วิดีโอเดี่ยว
                audio_url = info['url']
                title = info.get('title', 'Unknown Title')
            else:
                raise Exception("ไม่พบ URL เสียงที่สามารถเล่นได้.")
            
            # เตรียมแหล่งเสียง FFmpeg
            source = discord.FFmpegPCMAudio(audio_url, executable="ffmpeg")
            
            # เล่นเสียงและกำหนดเวลาล้างข้อมูล/เพลงถัดไป
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
            await asyncio.sleep(1) # หน่วงเวลาเล็กน้อยก่อนลองเล่นเพลงถัดไป
            # พยายามเล่นเพลงถัดไปในคิวโดยอัตโนมัติหากปัจจุบันล้มเหลว
            if queue and voice_client and voice_client.is_connected():
                await _play_next_in_queue(channel)
            elif not queue:
                await channel.send("✅ เล่นเพลงในคิวทั้งหมดแล้ว!")
        except Exception as e: # ดักจับข้อผิดพลาดทั่วไปอื่นๆ
            logging.error(f"ข้อผิดพลาดในการเล่นรายการ YouTube {url_to_play}: {e}", exc_info=True)
            await channel.send(f"❌ ไม่สามารถเล่นวิดีโอ YouTube: {url_to_play} ได้ เกิดข้อผิดพลาด: {e}")
            await asyncio.sleep(1)
            # พยายามเล่นเพลงถัดไปในคิวโดยอัตโนมัติ
            if queue and voice_client and voice_client.is_connected():
                await _play_next_in_queue(channel)
            elif not queue:
                await channel.send("✅ เล่นเพลงในคิวทั้งหมดแล้ว!")
    else:
        # จัดการ URL ที่ไม่ใช่ YouTube หรือสื่อประเภทอื่น ๆ หากจำเป็น
        # สำหรับตอนนี้ แค่บันทึก Log และข้ามไป
        logging.info(f"การเล่น URL ที่ไม่ใช่ YouTube {url_to_play} ยังไม่ได้ถูกนำมาใช้ทั้งหมด ขอข้ามไป.")
        await channel.send(f"⚠️ การเล่น URL ที่ไม่ใช่ YouTube {url_to_play} ยังไม่ได้ถูกนำมาใช้ทั้งหมด ขอข้ามไป.")
        await asyncio.sleep(1)
        # พยายามเล่นเพลงถัดไปในคิวโดยอัตโนมัติ
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
            # พยายามโหลดไลบรารี Opus (ปรับ 'libopus.so' สำหรับ OS ของคุณหากจำเป็น)
            discord.opus.load_opus('libopus.so') # สำหรับ Linux
            # discord.opus.load_opus('opus.dll') # สำหรับ Windows
            # discord.opus.load_opus('libopus.dylib') # สำหรับ macOS
            logging.info("Opus โหลดสำเร็จ.")
        except Exception as e:
            logging.error(f"ไม่สามารถโหลด opus: {e} ได้ คำสั่งเสียงอาจไม่ทำงาน.")
            print(f"ไม่สามารถโหลด opus: {e} ได้ โปรดตรวจสอบให้แน่ใจว่าติดตั้งและเข้าถึงได้.")

    print(f"✅ บอทเข้าสู่ระบบในฐานะ {bot.user}")
    logging.info(f"บอทเข้าสู่ระบบในฐานะ {bot.user}")

    # ซิงค์คำสั่งทั่วโลกและไปยัง Guild เฉพาะสำหรับการอัปเดตที่รวดเร็วระหว่างการพัฒนา
    try:
        # ซิงค์คำสั่งทั่วโลก
        await tree.sync() 
        logging.info("คำสั่งทั่วโลกซิงค์แล้ว.")
        
        # ซิงค์คำสั่งไปยัง Guild เฉพาะ
        guild_obj = discord.Object(id=YOUR_GUILD_ID)
        await tree.sync(guild=guild_obj)
        logging.info(f"คำสั่งซิงค์กับ Guild: {YOUR_GUILD_ID}")
    except Exception as e:
        logging.error(f"ไม่สามารถซิงค์คำสั่ง: {e} ได้", exc_info=True)

    # โหลดข้อมูลผู้ใช้ทั้งหมดจาก Firestore เมื่อบอทเริ่มต้น (รวมถึงโทเค็น Spotify และเซสชัน Discord web)
    await load_all_user_data_from_firestore()
    
    bot_ready.set()
    logging.info("บอทพร้อมใช้งานเต็มที่แล้ว.")

# --- Discord Slash Commands ---

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
    await interaction.response.send_message(
        f"🔗 ในการเชื่อมโยงบัญชี Spotify ของคุณ โปรดไปที่:\n"
        f"**{url_for('index', _external=True)}**\n"
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
                await update_user_data_in_firestore(interaction.user.id, spotify_token_info=firestore.DELETE_FIELD) # ลบจาก Firestore ด้วย
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
    try:
        # แยกสตริงที่ป้อนด้วยเครื่องหมายจุลภาค, ตัดช่องว่าง, และกรองสตริงที่ว่างเปล่าออก
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

# --- คลาสระบบโพลล์ ---
class PollView(discord.ui.View):
    def __init__(self, poll_id, question, options):
        super().__init__(timeout=None) # คงโพลล์ให้ทำงานไปเรื่อยๆ
        self.poll_id = poll_id
        self.question = question
        self.options = options
        
        # เริ่มต้นโครงสร้างข้อมูลโหวตสำหรับโพลล์นี้
        # สิ่งนี้ควรเกิดขึ้นเพียงครั้งเดียวเมื่อสร้างโพลล์
        if poll_id not in active_polls:
            active_polls[poll_id] = {
                "question": question,
                "options": options,
                "votes": {option: set() for option in options} # ใช้ set เพื่อเก็บ user ID ป้องกันการโหวตซ้ำ
            }
        
        # เพิ่มปุ่มสำหรับแต่ละตัวเลือกแบบไดนามิกภายในเมธอด __init__
        for i, option in enumerate(options):
            button = discord.ui.Button(label=option, custom_id=f"poll_{poll_id}_{i}", style=discord.ButtonStyle.primary)
            button.callback = self._button_callback # กำหนด callback เฉพาะสำหรับปุ่มนี้
            self.add_item(button)

        # เพิ่มปุ่ม "แสดงผลลัพธ์"
        show_results_button_item = discord.ui.Button(label="แสดงผลลัพธ์", style=discord.ButtonStyle.secondary, custom_id=f"poll_show_results_{poll_id}")
        show_results_button_item.callback = self.show_results_button # กำหนด callback เฉพาะ
        self.add_item(show_results_button_item)


    async def on_timeout(self):
        # สิ่งนี้จะถูกเรียกเมื่อ View หมดเวลา
        # สำหรับโพลล์ที่ไม่สิ้นสุด อาจไม่ถึงตรงนี้เว้นแต่จะหยุดอย่างชัดเจน
        logging.info(f"Poll {self.poll_id} หมดเวลา.")
        # คุณอาจต้องการปิดใช้งานปุ่มหรือลบข้อมูลโพลล์ที่นี่
        # self.clear_items()
        # await self.message.edit(view=self) # ปิดใช้งานการโต้ตอบ
        # if self.poll_id in active_polls:
        #     del active_polls[self.poll_id]

    @discord.ui.button(label="แสดงผลลัพธ์", style=discord.ButtonStyle.secondary, custom_id="show_results_placeholder")
    async def show_results_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        expected_custom_id = f"poll_show_results_{self.poll_id}"
        if button.custom_id != expected_custom_id:
            await interaction.response.send_message("❌ ปุ่ม 'แสดงผลลัพธ์' นี้ไม่ได้เชื่อมโยงกับโพลล์ที่ใช้งานอยู่.", ephemeral=True)
            return

        await self.update_poll_message(interaction.message)
        await interaction.response.defer() # ยืนยันการกดปุ่มโดยไม่ต้องส่งข้อความใหม่


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

    # Callback สำหรับปุ่มตัวเลือก
    async def _button_callback(self, interaction: discord.Interaction): # ลบอาร์กิวเมนต์ 'button'
        # แยก poll_id และ option_index จาก custom_id (เช่น "poll_MESSAGEID_INDEX")
        custom_id = interaction.data['custom_id'] # ดึง custom_id จากข้อมูลการโต้ตอบ
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
        # ตรรกะนี้ช่วยให้ผู้ใช้สามารถเปลี่ยนการโหวตได้โดยการคลิกตัวเลือกอื่น
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
            # หากผู้ใช้คลิกตัวเลือกเดิมที่โหวตไปแล้ว จะถือว่าเป็นการยืนยัน
            # หรือหากต้องการอนุญาตให้ยกเลิกการโหวต:
            # poll_data['votes'][selected_option].remove(user_id)
            # status_message = f"✅ คุณยกเลิกการโหวต: **{selected_option}**"
            status_message = f"✅ คุณยังคงโหวตให้: **{selected_option}**"
            logging.info(f"ผู้ใช้ {user_id} ยืนยันการโหวตสำหรับ {selected_option} ในโพลล์ {poll_id}")

        await self.update_poll_message(interaction.message)
        await interaction.response.send_message(status_message, ephemeral=True)


@tree.command(name="poll", description="สร้างโพลล์ด้วยตัวเลือก")
@app_commands.describe(question="คำถามสำหรับโพลล์")
@app_commands.describe(options="ตัวเลือกสำหรับโพลล์ (คั่นด้วยจุลภาค เช่น ตัวเลือก A, ตัวเลือก B)")
async def create_poll(interaction: discord.Interaction, question: str, options: str):
    # แยกตัวเลือกด้วยเครื่องหมายจุลภาคและทำความสะอาดช่องว่าง
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


    # หน่วงการตอบสนองก่อนที่จะส่งข้อความพร้อม View
    await interaction.response.defer(ephemeral=False)

    # ส่งข้อความโดยไม่มี View ก่อนเพื่อดึง Message ID
    # จากนั้นจึงสร้าง View ด้วย Message ID และแก้ไขข้อความ
    message = await interaction.followup.send(embed=embed)
    
    # เก็บข้อมูลโพลล์ทันทีที่ Message ID พร้อมใช้งาน
    # PollView.__init__ จะตรวจสอบ active_polls ดังนั้นตรวจสอบให้แน่ใจว่าได้ตั้งค่านี้ก่อนสร้าง PollView
    active_polls[message.id] = {
        "question": question,
        "options": option_list,
        "votes": {option: set() for option in option_list}
    }

    # สร้าง Instance ของ PollView, ตอนนี้ปุ่มทั้งหมดถูกเพิ่มภายใน __init__ แล้ว
    poll_view = PollView(message.id, question, option_list)
    
    await message.edit(view=poll_view) # แก้ไขข้อความเพื่อแนบ View (ปุ่ม)
    logging.info(f"โพลล์สร้างโดย {interaction.user.display_name}: ID {message.id}, คำถาม: {question}, ตัวเลือก: {options}")


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
    
    await interaction.response.defer() # หน่วงการตอบสนองเนื่องจากการสร้าง TTS อาจใช้เวลา
    try:
        # สร้างชื่อไฟล์เฉพาะสำหรับเสียง TTS
        tts_filename = f"tts_discord_{interaction.id}.mp3"
        await asyncio.to_thread(gTTS(message, lang=lang).save, tts_filename) # ใช้ asyncio.to_thread สำหรับการดำเนินการ TTS ที่บล็อก
        
        # เล่นไฟล์เสียงที่สร้างขึ้น
        source = discord.FFmpegPCMAudio(tts_filename, executable="ffmpeg")
        voice_client.play(source, after=lambda e: asyncio.create_task(cleanup_audio(e, tts_filename))) # ล้างข้อมูลหลังเล่น
        
        await interaction.followup.send(f"🗣️ กำลังพูด: **{message}** (ภาษา: {lang})")

    except Exception as e:
        await interaction.followup.send(f"❌ เกิดข้อผิดพลาดในการพูด: {e}")
        logging.error(f"ข้อผิดพลาด TTS: {e}", exc_info=True)

async def cleanup_audio(error, filename):
    """ล้างไฟล์เสียง TTS หลังจากเล่น"""
    if error:
        logging.error(f"ข้อผิดพลาดในการเล่น TTS: {error}")
    # ตรวจสอบให้แน่ใจว่าไฟล์มีอยู่ก่อนที่จะพยายามลบ
    if os.path.exists(filename):
        os.remove(filename)
        logging.info(f"ล้างไฟล์เสียง: {filename}")

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


# --- Flask Routes ---
@app.route("/")
async def index(): # เปลี่ยนเป็น async เนื่องจากจะมีการ await _check_spotify_link_status
    # ดึงหรือสร้าง Session ID เฉพาะสำหรับผู้ใช้
    current_session_id = session.get('session_id')
    if not current_session_id:
        current_session_id = os.urandom(16).hex()
        session['session_id'] = current_session_id

    # ดึง Discord user ID จาก web_logged_in_users ที่โหลดมาจาก Firestore
    discord_user_id = web_logged_in_users.get(current_session_id)
    is_discord_linked = bool(discord_user_id) # ถือว่าเชื่อมโยง Discord หากมี user_id ใน web_logged_in_users

    is_spotify_linked = False

    # ตรวจสอบสถานะการเชื่อมโยง Spotify หากเชื่อมโยง Discord แล้ว
    if discord_user_id:
        try:
            # รันการตรวจสอบแบบ async ใน event loop ของบอท
            is_spotify_linked = await _check_spotify_link_status(discord_user_id)
        except Exception as e:
            logging.error(f"ข้อผิดพลาดในการตรวจสอบสถานะการเชื่อมโยง Spotify สำหรับผู้ใช้เว็บ {discord_user_id}: {e}")
            is_spotify_linked = False # ถือว่าไม่ได้เชื่อมโยงเมื่อเกิดข้อผิดพลาด

    # Render template ของ index.html ที่รวมกันแล้ว
    return render_template(
        "index.html",
        is_discord_linked=is_discord_linked,
        discord_user_id=discord_user_id,
        is_spotify_linked=is_spotify_linked
    )

@app.route("/api/auth_status")
async def get_auth_status():
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
    current_session_id = session.get('session_id')
    discord_user_id = web_logged_in_users.get(current_session_id)
    return jsonify({"discord_user_id": discord_user_id})


@app.route("/login/discord")
def login_discord():
    # สร้าง URL OAuth ของ Discord
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
    code = request.args.get("code")
    error = request.args.get("error")
    current_session_id = session.get('session_id') 

    if error:
        flash(f"❌ ข้อผิดพลาด Discord OAuth: {error}", "error")
        return redirect(url_for("index"))

    if not code:
        flash("❌ ไม่ได้รับรหัสอนุญาต", "error")
        return redirect(url_for("index"))

    try:
        future = asyncio.run_coroutine_threadsafe(
            _fetch_discord_token_and_user(code),
            bot.loop
        )
        token_info, user_data = future.result(timeout=10)
        
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

        flash(f"✅ เข้าสู่ระบบ Discord สำเร็จ: {discord_username}", "success")

    except Exception as e:
        flash(f"❌ ข้อผิดพลาดในการเข้าสู่ระบบ Discord: {e}", "error")
        logging.error(f"ข้อผิดพลาด Discord OAuth: {e}", exc_info=True)
    
    return redirect(url_for("index")) 

@app.route("/login/spotify/<int:discord_user_id_param>")
def login_spotify_web(discord_user_id_param: int):
    current_session_id = session.get('session_id')
    logged_in_discord_user_id = web_logged_in_users.get(current_session_id)

    if logged_in_discord_user_id != discord_user_id_param:
        flash("❌ Discord User ID ไม่ตรงกัน กรุณาเข้าสู่ระบบด้วย Discord อีกครั้ง.", "error")
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
    return redirect(auth_url)

@app.route("/callback/spotify")
def spotify_callback():
    code = request.args.get("code")
    error = request.args.get("error")
    discord_user_id = session.pop('spotify_auth_discord_user_id', None) 
    
    if error:
        flash(f"❌ ข้อผิดพลาด Spotify OAuth: {error}", "error")
        return redirect(url_for("index"))

    if not code or not discord_user_id:
        flash("❌ รหัสอนุญาตหรือ Discord user ID สำหรับการเชื่อมโยง Spotify หายไป โปรดลองอีกครั้ง.", "error")
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
        spotify_users[discord_user_id] = sp_user
        
        # บันทึก Spotify token info ลง Firestore
        asyncio.run_coroutine_threadsafe(
            update_user_data_in_firestore(discord_user_id, spotify_token_info=token_info),
            bot.loop
        ).result() # รอให้การอัปเดต Firestore เสร็จสมบูรณ์

        flash("✅ เชื่อมโยง Spotify สำเร็จ!", "success")
        
    except Exception as e:
        flash(f"❌ ข้อผิดพลาดในการเชื่อมโยง Spotify: {e}. โปรดตรวจสอบให้แน่ใจว่า redirect URI ของคุณถูกต้องใน Spotify Developer Dashboard.", "error")
        logging.error(f"ข้อผิดพลาด Spotify callback สำหรับผู้ใช้ {discord_user_id}: {e}", exc_info=True)
    
    return redirect(url_for("index"))

# --- Run Flask + Discord bot ---
def run_web():
    # Flask app should run in its own thread
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)

if __name__ == "__main__":
    print("\n--- กำลังเริ่มต้นบอทและเว็บเซิร์ฟเวอร์ ---")
    print("ตรวจสอบให้แน่ใจว่าติดตั้ง FFmpeg และ Opus แล้วสำหรับฟังก์ชันเสียง")
    print("---------------------------------------\n")

    # เริ่ม Flask web server ในเธรดแยก
    web_thread = threading.Thread(target=run_web)
    web_thread.start()
    
    # รัน Discord bot (นี่เป็นการเรียกแบบบล็อก)
    bot.run(DISCORD_TOKEN)
