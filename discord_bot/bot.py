# bot.py

import discord
from discord.ext import commands
from discord import app_commands
from gtts import gTTS
import os
import logging
import json
from dotenv import load_dotenv
import spotipy
from spotipy.oauth2 import SpotifyOAuth
import asyncio

# --- โหลดตัวแปรสภาพแวดล้อมจากไฟล์ .env ---
load_dotenv()

# --- ตั้งค่า Spotify API Credentials ---
SPOTIPY_CLIENT_ID = os.getenv("SPOTIPY_CLIENT_ID")
SPOTIPY_CLIENT_SECRET = os.getenv("SPOTIPY_CLIENT_SECRET")
SPOTIPY_REDIRECT_URI = os.getenv("SPOTIPY_REDIRECT_URI") # อาจจะไม่จำเป็นใน bot.py ถ้า web app จัดการ OAuth
SPOTIPY_SCOPES = "user-read-playback-state user-modify-playback-state user-read-currently-playing playlist-read-private playlist-read-collaborative user-library-read"

# --- ตั้งค่า Discord Bot Credentials ---
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
YOUR_GUILD_ID = int(os.environ["GUILD_ID"]) # ใช้สำหรับ sync command กับ Guild โดยตรง

# --- Global Dictionaries / Variables ---
spotify_users = {}
# web_logged_in_users จะอยู่ใน web.py แทน
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

# เพิ่ม flag เพื่อตรวจสอบว่าบอทพร้อมใช้งานหรือไม่ (อาจจะไม่จำเป็นตรงนี้ถ้า Flask ไม่ได้เรียกตรงๆ)
# bot_ready = asyncio.Event() # ไม่ต้องใช้แล้วเพราะแยก process

# --- Helper Function: Get Spotify Client for User ---
def get_user_spotify_client(discord_user_id: int):
    sp_client = spotify_users.get(discord_user_id)
    if sp_client:
        try:
            sp_client.current_user() # ตรวจสอบ token
            return sp_client
        except spotipy.exceptions.SpotifyException as e:
            logging.warning(f"Spotify token expired or invalid for user {discord_user_id}: {e}")
            if discord_user_id in spotify_users:
                del spotify_users[discord_user_id]
            # ไม่ต้อง save_spotify_tokens() ตรงนี้ ถ้า web.py เป็นตัวจัดการหลัก
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

# --- Discord Bot Events ---
@bot.event
async def on_ready():
    # global bot_ready # ไม่ต้องใช้แล้ว
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

    guild_obj = discord.Object(id=YOUR_GUILD_ID)
    await tree.sync(guild=guild_obj)
    logging.info(f"Commands synced to guild: {YOUR_GUILD_ID}")

    # โหลด Spotify tokens เมื่อบอทพร้อม
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
                # ถ้า Token เสีย ก็ลบออกจากไฟล์ทันที
                if int(user_id) in spotify_users:
                    del spotify_users[int(user_id)]
                save_spotify_tokens() # Save เพื่อลบ token ที่เสียออก
    except FileNotFoundError:
        logging.info("No saved Spotify tokens found.")
    except json.JSONDecodeError:
        logging.error("Error decoding spotify_tokens.json. File might be corrupted.")
    except Exception as e:
        logging.error(f"Error loading Spotify tokens: {e}")
    
    # bot_ready.set() # ไม่ต้องใช้แล้ว

# --- Discord Slash Commands ---

# คำสั่ง link_spotify จะถูกเรียกใช้บน Discord โดยตรง
# แต่การยืนยันตัวตนผ่านเว็บยังต้องทำผ่าน Flask App
# ดังนั้นในบอทจะแค่ส่งลิงก์ไปให้ user ไปดำเนินการต่อบนเว็บ
@tree.command(name="link_spotify", description="เชื่อมต่อบัญชี Spotify ของคุณ")
async def link_spotify(interaction: discord.Interaction):
    # ไม่ต้องตรวจสอบ web_logged_in_users.values() ตรงนี้แล้ว เพราะ Flask ดูแลเรื่อง session
    # แต่แนะนำให้ user ไปที่หน้าเว็บเพื่อเริ่มกระบวนการ
    await interaction.response.send_message(
        f"🔗 กรุณาไปที่หน้าเว็บของเราเพื่อเชื่อมต่อ Spotify ของคุณ: \n"
        f"**{os.getenv('FLASK_BASE_URL')}** (ล็อกอิน Discord บนเว็บก่อน)\n"
        f"จากนั้นคลิกที่ 'เชื่อมต่อ Spotify' บนหน้าเว็บ", 
        ephemeral=True
    )
    logging.info(f"Sent Spotify auth link instruction to user {interaction.user.id}")


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

        # การเล่นเพลง/Playlist/Album
        if "playlist" in query or "album" in query:
            if "playlist" in query:
                sp_user.start_playback(device_id=active_device_id, context_uri=f"spotify:playlist:{playlist_id}")
            elif "album" in query:
                sp_user.start_playback(device_id=active_device_id, context_uri=f"spotify:album:{album_id}")
        else:
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

# --- Running the Bot ---
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)