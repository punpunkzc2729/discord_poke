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

# --- Global Variables ---
spotify_users = {}  # Key: Discord User ID, Value: Spotify client object
web_logged_in_users = {}  # Key: Flask Session ID, Value: Discord User ID
voice_client = None
queue = []  # Music queue for playback (supports YouTube/SoundCloud URLs)
current_playing_youtube_info = {} # Stores {'title', 'duration', 'thumbnail'} for YouTube/SoundCloud playback
volume = 1.0 # Initial volume level

# --- Global variables for Poll System (as per PRD) ---
active_polls = {}

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s:%(levelname)s:%(message)s',
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)

# --- Discord Bot Setup ---
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.members = True # Requires Privileged Intent in Discord Developer Portal!

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree
bot_ready = asyncio.Event()

# --- Flask App Setup ---
# Set template_folder for Jinja2 templates and static_folder for static files (JS, CSS)
app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = os.getenv("FLASK_SECRET_KEY") or os.urandom(24) 

# --- Helper functions for Firestore Persistence (as per PRD) ---
async def update_user_data_in_firestore(discord_user_id: int, spotify_token_info: dict = None, flask_session_to_add: str = None, flask_session_to_remove: str = None):
    """
    Updates user data in Firestore, including Spotify tokens and Flask sessions.
    """
    if db is None:
        logging.error("Firestore DB not initialized. Cannot update user data.")
        return

    user_ref = db.collection('users').document(str(discord_user_id))
    user_data_to_update = {}

    try:
        # Use run_in_executor for potentially blocking Firestore API calls
        doc = await asyncio.to_thread(user_ref.get)
        current_data = doc.to_dict() if doc.exists else {}
    except firebase_exceptions.FirebaseError as e:
        logging.error(f"Error fetching user {discord_user_id} data from Firestore: {e}", exc_info=True)
        return

    if spotify_token_info:
        # If spotify_token_info is firestore.DELETE_FIELD, delete the field
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
            logging.info(f"User data {discord_user_id} updated in Firestore.")
        except firebase_exceptions.FirebaseError as e:
            logging.error(f"Error updating user {discord_user_id} data in Firestore: {e}", exc_info=True)

async def load_all_user_data_from_firestore():
    """
    Loads all user data (Spotify tokens, Flask sessions) from Firestore into global variables.
    """
    global spotify_users, web_logged_in_users

    if db is None:
        logging.warning("Firestore DB not initialized. Cannot load user data.")
        return

    try:
        users_ref = db.collection('users')
        docs = await asyncio.to_thread(users_ref.get) # Use asyncio.to_thread

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
                    logging.info(f"Loaded valid Spotify token for user ID: {user_id} from Firestore.")
                except spotipy.exceptions.SpotifyException as e:
                    logging.warning(f"Spotify token for user {user_id} expired on startup (Firestore): {e}. Removing from local cache and Firestore.")
                    # Remove expired Spotify token data from Firestore
                    await update_user_data_in_firestore(user_id, spotify_token_info=firestore.DELETE_FIELD)
                    if user_id in spotify_users:
                        del spotify_users[user_id]
                except Exception as e:
                    logging.error(f"Error validating loaded Spotify token for user {user_id}: {e}", exc_info=True)

            flask_sessions_list = data.get('flask_sessions', [])
            for session_id in flask_sessions_list:
                web_logged_in_users[session_id] = user_id
            
        logging.info("All user data (Spotify tokens and Flask sessions) loaded from Firestore.")
    except firebase_exceptions.FirebaseError as e:
        logging.error(f"Error loading all user data from Firestore: {e}", exc_info=True)
    except Exception as e:
        logging.error(f"Unexpected error loading user data from Firestore: {e}", exc_info=True)


def get_user_spotify_client(discord_user_id: int):
    """
    Retrieves the Spotify client for a Discord user.
    This function only retrieves from cache, it does not validate the token immediately.
    Token validation should be done in an async context using _check_spotify_link_status.
    """
    return spotify_users.get(discord_user_id)

async def _check_spotify_link_status(discord_user_id: int) -> bool:
    """
    Checks the user's Spotify linking status by validating the token.
    """
    await bot_ready.wait() # Wait until the bot loop is running
    sp_client = spotify_users.get(discord_user_id)
    if sp_client:
        try:
            await asyncio.to_thread(sp_client.current_user) # Validate Token asynchronously
            return True
        except spotipy.exceptions.SpotifyException as e:
            logging.warning(f"Spotify token expired for user {discord_user_id}: {e}")
            if discord_user_id in spotify_users:
                del spotify_users[discord_user_id]
                await update_user_data_in_firestore(discord_user_id, spotify_token_info=firestore.DELETE_FIELD)
            return False
        except Exception as e:
            logging.error(f"Unexpected error during Spotify token check for user {discord_user_id}: {e}", exc_info=True)
            return False
    return False

async def _fetch_discord_token_and_user(code: str):
    """
    Exchanges the Discord authorization code for tokens and user data.
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
        logging.error(f"Error during audio playback: {error}")
        channel = bot.get_channel(channel_id)
        if channel:
            await channel.send(f"❌ An error occurred during playback: {error}")
    
    # Clear now playing info
    current_playing_youtube_info = {}

    # Check if the bot is still in the voice channel and not playing before calling _play_next_in_queue
    if voice_client and voice_client.is_connected() and not voice_client.is_playing() and queue:
        channel = bot.get_channel(channel_id)
        if channel:
            await _play_next_in_queue(channel)
    elif not queue and voice_client and voice_client.is_connected() and not voice_client.is_playing():
        logging.info("Music queue finished.")
        channel = bot.get_channel(channel_id)
        if channel:
            await channel.send("✅ Finished playing all songs in the queue!")


async def _play_next_in_queue(channel: discord.VoiceChannel):
    """Plays the next song in the queue, supporting YouTube/SoundCloud URLs."""
    global voice_client, queue, volume, current_playing_youtube_info

    if not voice_client or not voice_client.is_connected():
        logging.warning("Bot is not in a voice channel to play the queue.")
        current_playing_youtube_info = {} # Clear info if bot is not in voice channel
        return

    if voice_client.is_playing():
        voice_client.stop()

    if not queue:
        logging.info("Music queue is empty.")
        await channel.send("✅ Finished playing all songs in the queue!")
        current_playing_youtube_info = {} # Clear info when queue is empty
        return

    url_to_play = queue.pop(0)
    logging.info(f"Attempting to play from queue: {url_to_play}")
    
    ydl_opts = {
        'format': 'bestaudio/best',
        'default_search': 'ytsearch', # For cases where user just gives a song title
        'source_address': '0.0.0.0',
        'verbose': False,
        'extract_flat': True # To quickly extract playlist info
    }

    try:
        loop = asyncio.get_event_loop() # Use the calling thread's event loop
        info = await loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(ydl_opts).extract_info(url_to_play, download=False))
        
        audio_url = None
        title = 'Unknown Title'
        duration = 0
        thumbnail = "https://placehold.co/400x400/FF0000/FFFFFF?text=YouTube"

        if info.get('_type') == 'playlist':
            playlist_title = info.get('title', 'Unknown Playlist')
            await channel.send(f"🎶 Adding playlist: **{playlist_title}** to the queue...")
            
            # Add all videos in the playlist to the queue
            # Since extract_flat=True, info.get('entries') will only have id and url of each video
            for entry in info.get('entries', []):
                if entry and entry.get('url'):
                    queue.append(entry['url'])
            
            # Play the very first video of the playlist (which was added as the first item in queue)
            if queue:
                # Pop the next video URL from the queue (which is now the first video of the playlist)
                first_video_url = queue.pop(0)
                # Fetch actual info for the first video again without extract_flat
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
                raise Exception("Could not extract videos from playlist.")
            
        elif info.get('url'): # Single video
            audio_url = info['url']
            title = info.get('title', 'Unknown Title')
            duration = info.get('duration', 0)
            thumbnail = info.get('thumbnail', thumbnail)
        else:
            raise Exception("No playable audio URL found.")
        
        current_playing_youtube_info = {
            'title': title,
            'duration': duration, # in seconds
            'thumbnail': thumbnail
        }
        
        source = discord.FFmpegPCMAudio(audio_url, executable="ffmpeg")
        
        # Use bot.loop to run the callback in the bot's main thread
        voice_client.play(source, after=lambda e: asyncio.run_coroutine_threadsafe(
            _after_playback_cleanup(e, channel.id), bot.loop))
        
        await channel.send(f"🎶 Now playing YouTube: **{title}**")

    except yt_dlp.utils.ExtractorError as e:
        error_message = str(e)
        if "Sign in to confirm you’re not a bot" in error_message or "requires login" in error_message or "age-restricted" in error_message or "unavailable in your country" in error_message:
            await channel.send(
                f"❌ Cannot play **{url_to_play}**: This video may require login, be age-restricted, or unavailable in your region. Please try a different public video."
            )
            logging.warning(f"Restricted YouTube video: {url_to_play}")
        else:
            await channel.send(f"❌ An unexpected error occurred playing YouTube for {url_to_play}: {e}")
            logging.error(f"Error playing YouTube item {url_to_play}: {e}", exc_info=True)
        
        current_playing_youtube_info = {} # Clear info on error
        # Attempt to play next song after an error
        if queue and voice_client and voice_client.is_connected():
            await _play_next_in_queue(channel)
        elif not queue:
            await channel.send("✅ Finished playing all songs in the queue!")
    except Exception as e:
        logging.error(f"Error playing YouTube item {url_to_play}: {e}", exc_info=True)
        await channel.send(f"❌ Could not play YouTube video: {url_to_play}. An error occurred: {e}")
        
        current_playing_youtube_info = {} # Clear info on error
        # Attempt to play next song after an error
        if queue and voice_client and voice_client.is_connected():
            await _play_next_in_queue(channel)
        elif not queue:
            await channel.send("✅ Finished playing all songs in the queue!")


# --- Discord Bot Events ---
@bot.event
async def on_ready():
    # Load Opus for voice functionality
    if not discord.opus.is_loaded():
        try:
            discord.opus.load_opus('libopus.so')
            logging.info("Opus loaded successfully.")
        except Exception as e:
            logging.error(f"Could not load opus: {e}. Voice commands may not work.")
            print(f"Could not load opus: {e}. Please ensure it is installed and accessible.")

    print(f"✅ Bot logged in as {bot.user}")
    logging.info(f"Bot logged in as {bot.user}")

    try:
        # Sync Global commands
        await tree.sync() 
        logging.info("Global commands synced.")
        
        # Sync commands to a specific Guild (if GUILD_ID is defined)
        if YOUR_GUILD_ID:
            guild_obj = discord.Object(id=YOUR_GUILD_ID)
            await tree.sync(guild=guild_obj)
            logging.info(f"Commands synced to guild: {YOUR_GUILD_ID}")
        else:
            logging.warning("GUILD_ID not defined. Not syncing commands to specific Guild.")
    except Exception as e:
        logging.error(f"Could not sync commands: {e}", exc_info=True)

    # Load Spotify tokens and Flask sessions from Firestore
    # As on_ready is already an async context, we can call await directly
    await load_all_user_data_from_firestore()
    
    bot_ready.set() # Set Event to let Flask know the bot is ready
    logging.info("Bot is fully ready.")

# --- Discord Slash Commands (as per PRD) ---

@tree.command(name="join", description="Joins your voice channel")
async def join(interaction: discord.Interaction):
    global voice_client
    if interaction.user.voice:
        channel = interaction.user.voice.channel
        if voice_client and voice_client.is_connected():
            if voice_client.channel.id == channel.id:
                await interaction.response.send_message(f"✅ Already in **{channel.name}**", ephemeral=True)
            else:
                await voice_client.move_to(channel)
                await interaction.response.send_message(f"✅ Moved to **{channel.name}**", ephemeral=True)
        else:
            try:
                voice_client = await channel.connect()
                await interaction.response.send_message(f"✅ Joined **{channel.name}**", ephemeral=True)
            except discord.ClientException as e:
                logging.error(f"Could not connect to voice channel: {e}")
                await interaction.response.send_message(f"❌ Could not connect to voice channel: {e}", ephemeral=True)
            except Exception as e:
                logging.error(f"An unexpected error occurred while joining voice channel: {e}")
                await interaction.response.send_message(f"❌ An unexpected error occurred: {e}", ephemeral=True)
    else:
        await interaction.response.send_message("❌ You are not in a voice channel", ephemeral=True)

@tree.command(name="leave", description="Leaves the voice channel")
async def leave(interaction: discord.Interaction):
    global voice_client, queue
    if voice_client and voice_client.is_connected():
        if voice_client.is_playing():
            voice_client.stop()
        queue.clear() # Clear queue when leaving voice channel
        await voice_client.disconnect()
        voice_client = None
        await interaction.response.send_message("✅ Left voice channel and cleared music queue", ephemeral=True)
    else:
        await interaction.response.send_message("❌ Not in a voice channel", ephemeral=True)

@tree.command(name="link_spotify", description="Links your Spotify account")
async def link_spotify(interaction: discord.Interaction):
    # Pass base_url to user so the link works in any environment
    base_url = request.url_root if request else "YOUR_APP_BASE_URL_HERE" 
    await interaction.response.send_message(
        f"🔗 To link your Spotify account, please visit:\n"
        f"**{base_url}**\n"
        f"Log in with Discord first, then connect Spotify.", 
        ephemeral=True
    )

@tree.command(name="play", description="Plays music from Spotify")
@app_commands.describe(query="Song name, artist, or Spotify link")
async def play(interaction: discord.Interaction, query: str):
    sp_user = get_user_spotify_client(interaction.user.id)
    if not sp_user:
        await interaction.response.send_message(
            "❌ Please link your Spotify account first using /link_spotify", 
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
            response_msg += f" Playing: **{track['name']}** by **{track['artists'][0]['name']}**"
        elif "spotify.com/playlist/" in query:
            playlist_id = query.split('/')[-1].split('?')[0]
            context_uri = f"spotify:playlist:{playlist_id}"
            playlist = await asyncio.to_thread(sp_user.playlist, playlist_id)
            response_msg += f" Playing playlist: **{playlist['name']}**"
        elif "spotify.com/album/" in query:
            album_id = query.split('/')[-1].split('?')[0]
            context_uri = f"spotify:album:{album_id}"
            album = await asyncio.to_thread(sp_user.album, album_id)
            response_msg += f" Playing album: **{album['name']}**"
        else:
            results = await asyncio.to_thread(sp_user.search, q=query, type='track', limit=1)
            if not results['tracks']['items']:
                await interaction.followup.send("❌ No song found on Spotify")
                return
            track = results['tracks']['items'][0]
            track_uris.append(track['uri'])
            response_msg += f" Playing: **{track['name']}** by **{track['artists'][0]['name']}**"

        devices = await asyncio.to_thread(sp_user.devices)
        active_device_id = None
        for device in devices['devices']:
            if device['is_active']:
                active_device_id = device['id']
                break
        
        if not active_device_id:
            await interaction.followup.send("❌ No active Spotify client found. Please open your Spotify app and play something there first, or select a playback device in Spotify.")
            return

        if context_uri:
            await asyncio.to_thread(sp_user.start_playback, device_id=active_device_id, context_uri=context_uri)
        else:
            await asyncio.to_thread(sp_user.start_playback, device_id=active_device_id, uris=track_uris)
        
        await interaction.followup.send(response_msg)

    except spotipy.exceptions.SpotifyException as e:
        if e.http_status == 401:
            await interaction.followup.send("❌ Spotify token expired. Please link your account again using /link_spotify.")
            if interaction.user.id in spotify_users:
                del spotify_users[interaction.user.id]
                await update_user_data_in_firestore(interaction.user.id, spotify_token_info=firestore.DELETE_FIELD)
        elif e.http_status == 404 and "Device not found" in str(e):
            await interaction.followup.send("❌ No active Spotify client found. Please open your Spotify app.")
        elif e.http_status == 403:
            await interaction.followup.send("❌ Spotify playback error: You might need a Spotify Premium account or have playback restrictions.")
        else:
            await interaction.followup.send(f"❌ Spotify error: {e}. Please try again.")
        logging.error(f"Spotify error for user {interaction.user.id}: {e}", exc_info=True)
    except Exception as e:
        await interaction.followup.send(f"❌ An unexpected error occurred: {e}")
        logging.error(f"Unexpected error in play command: {e}", exc_info=True)

@tree.command(name="pause", description="Pauses Spotify playback")
async def pause_spotify(interaction: discord.Interaction):
    sp_user = get_user_spotify_client(interaction.user.id)
    if not sp_user:
        await interaction.response.send_message("❌ Please link your Spotify account first using /link_spotify", ephemeral=True)
        return
    
    try:
        await asyncio.to_thread(sp_user.pause_playback)
        await interaction.response.send_message("⏸️ Spotify playback paused", ephemeral=True)
    except spotipy.exceptions.SpotifyException as e:
        await interaction.response.send_message(f"❌ Error pausing Spotify playback: {e}", ephemeral=True)
        logging.error(f"Error pausing Spotify playback for user {interaction.user.id}: {e}", exc_info=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ An unexpected error occurred: {e}", ephemeral=True)
        logging.error(f"Unexpected error in pause command: {e}", exc_info=True)

@tree.command(name="resume", description="Resumes Spotify playback")
async def resume_spotify(interaction: discord.Interaction):
    sp_user = get_user_spotify_client(interaction.user.id)
    if not sp_user:
        await interaction.response.send_message("❌ Please link your Spotify account first using /link_spotify", ephemeral=True)
        return
    
    try:
        await asyncio.to_thread(sp_user.start_playback)
        await interaction.response.send_message("▶️ Spotify playback resumed", ephemeral=True)
    except spotipy.exceptions.SpotifyException as e:
        await interaction.response.send_message(f"❌ Error resuming Spotify playback: {e}", ephemeral=True)
        logging.error(f"Error resuming Spotify playback for user {interaction.user.id}: {e}", exc_info=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ An unexpected error occurred: {e}", ephemeral=True)
        logging.error(f"Unexpected error in resume command: {e}", exc_info=True)

@tree.command(name="skip", description="Skips the current song")
async def skip_spotify(interaction: discord.Interaction):
    sp_user = get_user_spotify_client(interaction.user.id)
    if not sp_user:
        await interaction.response.send_message("❌ Please link your Spotify account first using /link_spotify", ephemeral=True)
        return
    
    try:
        await asyncio.to_thread(sp_user.next_track)
        await interaction.response.send_message("⏭️ Song skipped", ephemeral=True)
    except spotipy.exceptions.SpotifyException as e:
        await interaction.response.send_message(f"❌ Error skipping Spotify song: {e}", ephemeral=True)
        logging.error(f"Error skipping Spotify song for user {interaction.user.id}: {e}", exc_info=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ An unexpected error occurred: {e}", ephemeral=True)
        logging.error(f"Unexpected error in skip command: {e}", exc_info=True)

@tree.command(name="speak", description="Makes the bot speak in the voice channel")
@app_commands.describe(message="The message for the bot to speak")
@app_commands.describe(lang="Language (e.g., 'en', 'th')")
async def speak(interaction: discord.Interaction, message: str, lang: str = 'en'):
    global voice_client
    if not voice_client or not voice_client.is_connected():
        await interaction.response.send_message("❌ Bot is not in a voice channel. Use /join first", ephemeral=True)
        return
    
    await interaction.response.defer()
    try:
        tts_filename = f"tts_discord_{interaction.id}.mp3"
        await asyncio.to_thread(gTTS(message, lang=lang).save, tts_filename)
        
        source = discord.FFmpegPCMAudio(tts_filename, executable="ffmpeg")
        
        # Use bot.loop to run the callback in the bot's main thread
        voice_client.play(source, after=lambda e: asyncio.run_coroutine_threadsafe(
            cleanup_audio(e, tts_filename), bot.loop))
        
        await interaction.followup.send(f"🗣️ Speaking: **{message}** (Language: {lang})")

    except Exception as e:
        await interaction.followup.send(f"❌ Error speaking: {e}")
        logging.error(f"TTS error: {e}", exc_info=True)

async def cleanup_audio(error, filename):
    """Cleans up the TTS audio file after playback"""
    if error:
        logging.error(f"Error playing TTS: {error}")
    if os.path.exists(filename):
        os.remove(filename)
        logging.info(f"Cleaned up audio file: {filename}")

@tree.command(name="random_name", description="Randomly picks a name from a given list")
@app_commands.describe(names="Names or items separated by commas (e.g., John, Doe, Alice)")
async def random_name(interaction: discord.Interaction, names: str):
    try:
        name_list = [name.strip() for name in names.split(',') if name.strip()]
        
        if not name_list:
            await interaction.response.send_message("❌ Please provide at least one name separated by commas", ephemeral=True)
            return

        selected_name = random.choice(name_list)
        await interaction.response.send_message(f"✨ The randomly selected name is: **{selected_name}**")
        logging.info(f"{interaction.user.display_name} randomized names: '{names}' and got '{selected_name}'")

    except Exception as e:
        logging.error(f"Error in random_name command: {e}", exc_info=True)
        await interaction.response.send_message(f"❌ An error occurred while picking a random name: {e}", ephemeral=True)

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

        show_results_button_item = discord.ui.Button(label="Show Results", style=discord.ButtonStyle.secondary, custom_id=f"poll_show_results_{poll_id}")
        show_results_button_item.callback = self.show_results_button
        self.add_item(show_results_button_item)

    async def on_timeout(self):
        logging.info(f"Poll {self.poll_id} timed out.")

    @discord.ui.button(label="Show Results", style=discord.ButtonStyle.secondary, custom_id="show_results_placeholder")
    async def show_results_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        expected_custom_id = f"poll_show_results_{self.poll_id}"
        if button.custom_id != expected_custom_id:
            await interaction.response.send_message("❌ This 'Show Results' button is not linked to an active poll.", ephemeral=True)
            return

        await self.update_poll_message(interaction.message)
        await interaction.response.defer()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return True 

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item):
        logging.error(f"Error in poll interaction: {error}", exc_info=True)
        try:
            await interaction.followup.send(f"❌ An error occurred while processing your vote: {error}", ephemeral=True)
        except discord.errors.NotFound:
            logging.warning(f"Could not send error message to webhook (Unknown Webhook) for interaction {interaction.id}. Original error: {error}")
        except Exception as e:
            logging.error(f"Could not send error message in on_error. Secondary error: {e}", exc_info=True)

    async def update_poll_message(self, message: discord.Message):
        """Updates the poll message with current vote counts"""
        poll_data = active_polls.get(message.id)
        if not poll_data:
            logging.warning(f"Attempted to update a non-existent poll: {message.id}")
            return

        embed = discord.Embed(
            title=f"📊 Poll: {poll_data['question']}",
            color=discord.Color.purple()
        )

        results_text = ""
        for option, voters in poll_data['votes'].items():
            results_text += f"**{option}**: {len(voters)} votes\n"
        
        embed.description = results_text if results_text else "No votes yet."
        embed.set_footer(text=f"Poll ID: {message.id}")
        
        await message.edit(embed=embed, view=self)

    async def _button_callback(self, interaction: discord.Interaction):
        custom_id = interaction.data['custom_id']
        parts = custom_id.split('_')
        if len(parts) != 3 or parts[0] != "poll":
            await interaction.response.send_message("❌ Error with this poll button", ephemeral=True)
            return

        poll_id = int(parts[1])
        option_index = int(parts[2])
        user_id = interaction.user.id
        
        poll_data = active_polls.get(poll_id)
        if not poll_data:
            await interaction.response.send_message("❌ This poll is no longer active.", ephemeral=True)
            return
        
        selected_option = poll_data['options'][option_index]

        user_changed_vote = False
        for option_key, voters_set in poll_data['votes'].items():
            if user_id in voters_set and option_key != selected_option:
                voters_set.remove(user_id)
                user_changed_vote = True
                logging.info(f"User {user_id} removed vote from {option_key} in poll {poll_id}")
                break
        
        if user_id not in poll_data['votes'][selected_option]:
            poll_data['votes'][selected_option].add(user_id)
            logging.info(f"User {user_id} voted for {selected_option} in poll {poll_id}")
            status_message = f"✅ You have voted for: **{selected_option}**"
        else:
            status_message = f"✅ You are still voting for: **{selected_option}**"
            logging.info(f"User {user_id} confirmed vote for {selected_option} in poll {poll_id}")

        await self.update_poll_message(interaction.message)
        await interaction.response.send_message(status_message, ephemeral=True)


@tree.command(name="poll", description="Creates a poll with options")
@app_commands.describe(question="The question for the poll")
@app_commands.describe(options="Options for the poll (comma-separated, e.g., Option A, Option B)")
async def create_poll(interaction: discord.Interaction, question: str, options: str):
    option_list = [opt.strip() for opt in options.split(',') if opt.strip()]

    if not option_list:
        await interaction.response.send_message("❌ Please provide at least one option for the poll", ephemeral=True)
        return
    
    if len(option_list) > 25:
        await interaction.response.send_message("❌ Only up to 25 options are supported for a poll", ephemeral=True)
        return

    embed = discord.Embed(
        title=f"📊 Poll: {question}",
        description="Click the buttons below to vote!",
        color=discord.Color.blue()
    )
    embed.set_footer(text=f"Poll created by: {interaction.user.display_name}")

    initial_results_text = ""
    for option in option_list:
        initial_results_text += f"**{option}**: 0 votes\n"
    embed.add_field(name="Initial Results", value=initial_results_text, inline=False)


    await interaction.response.defer(ephemeral=False)

    message = await interaction.followup.send(embed=embed)
    
    active_polls[message.id] = {
        "question": question,
        "options": option_list,
        "votes": {option: set() for option in option_list}
    }

    poll_view = PollView(message.id, question, option_list)
    
    await message.edit(embed=embed, view=poll_view) # Corrected to use the updated embed
    logging.info(f"Poll created by {interaction.user.display_name}: ID {message.id}, Question: {question}, Options: {options}")


@tree.command(name="wake", description="Wakes up a user with a DM")
@app_commands.describe(user="Select a user")
async def wake(interaction: discord.Interaction, user: discord.User):
    try:
        await user.send(f"⏰ You've been poked by {interaction.user.display_name}! Wakey wakey!")
        await interaction.response.send_message(f"✅ Poked {user.name}", ephemeral=True)
        logging.info(f"{interaction.user.display_name} poked {user.name}.")
    except discord.Forbidden:
        await interaction.response.send_message(f"❌ Could not send DM to {user.name} (they might have DMs closed or are a bot)", ephemeral=True)
        logging.warning(f"Could not send DM to {user.name} (Forbidden).")
    except Exception as e:
        await interaction.response.send_message(f"❌ An error occurred sending DM: {e}", ephemeral=True)
        logging.error(f"Could not poke user: {e}", exc_info=True)


# --- Flask Routes for serving HTML templates and static files ---
@app.route("/")
async def index():
    # Pass necessary data to the template
    current_session_id = session.get('session_id')
    discord_user_id = web_logged_in_users.get(current_session_id)
    is_discord_linked = bool(discord_user_id)
    is_spotify_linked = False
    discord_username = None

    if is_discord_linked:
        try:
            user_obj = bot.get_user(discord_user_id)
            if user_obj:
                discord_username = user_obj.name
            else:
                # Fallback: fetch user if not in cache (may require privileged intents)
                try:
                    user_obj = await bot.fetch_user(discord_user_id)
                    discord_username = user_obj.name
                except Exception as e:
                    logging.warning(f"Could not fetch Discord user {discord_user_id} for API: {e}")
                    discord_username = str(discord_user_id) # Fallback to ID

            is_spotify_linked = await _check_spotify_link_status(discord_user_id)
        except Exception as e:
            logging.error(f"Error checking Spotify link status for web user {discord_user_id}: {e}")
            is_spotify_linked = False

    # Get environment variables for Firebase, APP_ID, and Auth Token
    # Use .get() with a default value to prevent errors if variables are not set
    # firebaseConfig should be JSON parsed from the string provided by the environment
    firebase_config_str = os.getenv("FIREBASE_CONFIG")
    firebase_config = {}
    if firebase_config_str:
        try:
            firebase_config = json.loads(firebase_config_str)
        except json.JSONDecodeError as e:
            logging.error(f"Error decoding FIREBASE_CONFIG: {e}", exc_info=True)
            firebase_config = {} # Fallback to empty dict

    app_id_env = os.getenv("APP_ID", "default-app-id-from-env") # Fallback for APP_ID
    initial_auth_token = os.getenv("INITIAL_AUTH_TOKEN", None) # Fallback for token

    return render_template(
        "index.html",
        is_discord_linked=is_discord_linked,
        discord_user_id=discord_user_id,
        discord_username=discord_username,
        is_spotify_linked=is_spotify_linked,
        base_url=request.url_root, # Pass base_url to the template
        firebase_config=firebase_config, # Pass Firebase config
        app_id=app_id_env, # Pass app ID
        initial_auth_token=initial_auth_token # Pass initial auth token
    )


# --- Flask Routes (API Endpoints for JS) ---
# These endpoints will be called by index.js to get/send data
@app.route("/api/auth_status")
async def get_auth_status_api():
    current_session_id = session.get('session_id')
    discord_user_id = web_logged_in_users.get(current_session_id)
    is_discord_linked = bool(discord_user_id)
    is_spotify_linked = False
    discord_username = None

    if discord_user_id:
        try:
            user_obj = bot.get_user(discord_user_id)
            if user_obj:
                discord_username = user_obj.name
            else:
                try:
                    user_obj = await bot.fetch_user(discord_user_id)
                    discord_username = user_obj.name
                except Exception as e:
                    logging.warning(f"Could not fetch Discord user {discord_user_id} for API: {e}")
                    discord_username = str(discord_user_id) # Fallback to ID

            is_spotify_linked = await _check_spotify_link_status(discord_user_id)
        except Exception as e:
            logging.error(f"Error checking Spotify link status for web user {discord_user_id}: {e}")
            is_spotify_linked = False

    return jsonify({
        "is_discord_linked": is_discord_linked,
        "is_spotify_linked": is_spotify_linked,
        "discord_user_id": discord_user_id,
        "discord_username": discord_username
    })

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
async def discord_callback(): # Changed to async def
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
        # Call _fetch_discord_token_and_user directly in Flask's async context
        token_info, user_data = await _fetch_discord_token_and_user(code)
        
        discord_user_id = int(user_data["id"])
        
        if not current_session_id:
            current_session_id = os.urandom(16).hex()
            session['session_id'] = current_session_id

        # Call update_user_data_in_firestore in the bot's async context
        await update_user_data_in_firestore(discord_user_id, flask_session_to_add=current_session_id)

        web_logged_in_users[current_session_id] = discord_user_id
        session['discord_user_id_for_web'] = discord_user_id

        flash(f"✅ Successfully logged in with Discord!", "success")

    except Exception as e:
        flash(f"❌ Error logging in with Discord: {e}", "error")
        logging.error(f"Discord OAuth error: {e}", exc_info=True)
    
    return redirect(url_for("index")) 

@app.route("/login/spotify/<int:discord_user_id_param>")
async def login_spotify_web(discord_user_id_param: int): # Changed to async def
    current_session_id = session.get('session_id')
    logged_in_discord_user_id = web_logged_in_users.get(current_session_id)

    if logged_in_discord_user_id != discord_user_id_param:
        flash("❌ Discord User ID mismatch. Please log in with Discord again.", "error")
        return redirect(url_for("index"))

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
async def spotify_callback(): # Changed to async def
    code = request.args.get("code")
    error = request.args.get("error")
    discord_user_id = session.pop('spotify_auth_discord_user_id', None) 
    
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

        # Use asyncio.to_thread for the blocking call (get_access_token)
        token_info = await asyncio.to_thread(auth_manager.get_access_token, code)

        sp_user = spotipy.Spotify(auth_manager=auth_manager)
        spotify_users[discord_user_id] = sp_user
        
        # Use await directly as spotify_callback is already async
        await update_user_data_in_firestore(discord_user_id, spotify_token_info=token_info)

        flash("✅ Spotify linked successfully!", "success")
        
    except Exception as e:
        flash(f"❌ Error linking Spotify: {e}. Please ensure your redirect URI is correct in Spotify Developer Dashboard.", "error")
        logging.error(f"Spotify callback error for user {discord_user_id}: {e}", exc_info=True)
    
    return redirect(url_for("index"))

# --- Flask routes for controlling bot from web ---
@app.route("/web_control/add", methods=["POST"])
async def add_web_queue(): # Changed to async def
    global voice_client, queue
    url = request.form.get("url") # Receive data as form data
    if not url:
        return jsonify({"status": "error", "message": "No URL provided to add to queue."}), 400

    current_session_id = session.get('session_id')
    discord_user_id = web_logged_in_users.get(current_session_id)

    if not discord_user_id:
        return jsonify({"status": "error", "message": "Please log in with Discord first to use music control."}), 401

    target_channel = None
    if voice_client and voice_client.is_connected():
        target_channel = voice_client.channel
    else:
        try:
            # Wait until the bot is ready
            await bot_ready.wait() 
            user = bot.get_user(discord_user_id)
            if user and user.voice and user.voice.channel:
                target_channel = user.voice.channel
                if not voice_client or not voice_client.is_connected():
                    try:
                        voice_client = await target_channel.connect()
                        logging.info(f"Bot automatically joined {target_channel.name} for web playback.")
                    except discord.ClientException as e:
                        logging.error(f"Could not automatically join voice channel: {e}")
                        return jsonify({"status": "error", "message": f"❌ Could not automatically join voice channel: {e}"}), 500
            else:
                return jsonify({"status": "error", "message": "❌ You are not in a Discord voice channel or the bot cannot access it"}), 400
        except Exception as e:
            logging.error(f"Error finding user's voice channel for web queue add: {e}", exc_info=True)
            return jsonify({"status": "error", "message": f"❌ Error: {e}"}), 500

    if not target_channel:
        return jsonify({"status": "error", "message": "❌ Bot is not in a voice channel. Please use the `/join` command in Discord first."}), 400

    queue.append(url)
    logging.info(f"Added to queue from web: {url}")

    if not voice_client.is_playing() and not voice_client.is_paused():
        await _play_next_in_queue(target_channel) # Call directly in async context

    return jsonify({"status": "success", "message": f"Added '{url}' to the queue!"})

@app.route("/web_control/play_spotify_search", methods=["POST"])
async def web_control_play_spotify_search():
    query = request.form.get("query") # Receive as form data
    if not query:
        return jsonify({"status": "error", "message": "No search query provided."}), 400
    
    current_session_id = session.get('session_id')
    discord_user_id = web_logged_in_users.get(current_session_id)
    if not discord_user_id:
        return jsonify({"status": "error", "message": "Please log in with Discord first to use Spotify music control."}), 401

    sp_user = get_user_spotify_client(discord_user_id)
    if not sp_user:
        return jsonify({"status": "error", "message": "Please link your Spotify account first."}), 403

    try:
        track_uris = []
        context_uri = None
        response_msg_title = ""

        if "spotify.com/track/" in query:
            track_id = query.split('/')[-1].split('?')[0]
            track_uri = f"spotify:track:{track_id}"
            track = await asyncio.to_thread(sp_user.track, track_uri)
            track_uris.append(track_uri)
            response_msg_title = f"**{track['name']}** by **{track['artists'][0]['name']}**"
        elif "spotify.com/playlist/" in query:
            playlist_id = query.split('/')[-1].split('?')[0]
            context_uri = f"spotify:playlist:{playlist_id}"
            playlist = await asyncio.to_thread(sp_user.playlist, playlist_id)
            response_msg_title = f"Playlist: **{playlist['name']}**"
        elif "spotify.com/album/" in query:
            album_id = query.split('/')[-1].split('?')[0]
            context_uri = f"spotify:album:{album_id}"
            album = await asyncio.to_thread(sp_user.album, album_id)
            response_msg_title = f"Album: **{album['name']}**"
        else:
            results = await asyncio.to_thread(sp_user.search, q=query, type='track', limit=1)
            if not results['tracks']['items']:
                return jsonify({"status": "error", "message": "No song found on Spotify."}), 404
            track = results['tracks']['items'][0]
            track_uris.append(track['uri'])
            response_msg_title = f"**{track['name']}** by **{track['artists'][0]['name']}**"

        devices = await asyncio.to_thread(sp_user.devices)
        active_device_id = None
        for device in devices['devices']:
            if device['is_active']:
                active_device_id = device['id']
                break
        
        if not active_device_id:
            return jsonify({"status": "error", "message": "No active Spotify client found. Please open your Spotify app and play something there first."}), 404

        if context_uri:
            await asyncio.to_thread(sp_user.start_playback, device_id=active_device_id, context_uri=context_uri)
        else:
            await asyncio.to_thread(sp_user.start_playback, device_id=active_device_id, uris=track_uris)
        
        return jsonify({"status": "success", "message": f"Playing Spotify: {response_msg_title}"})

    except spotipy.exceptions.SpotifyException as e:
        error_message = f"Spotify error: {e.msg}"
        if e.http_status == 401:
            error_message = "Spotify token expired. Please relink your account."
            # Clear token in Firestore
            await update_user_data_in_firestore(discord_user_id, spotify_token_info=firestore.DELETE_FIELD)
            if discord_user_id in spotify_users:
                del spotify_users[discord_user_id]
        elif e.http_status == 404 and "Device not found" in str(e):
            error_message = "No active Spotify client found. Please open your Spotify app."
        elif e.http_status == 403:
            error_message = "Spotify playback error: You might need a Spotify Premium account or have playback restrictions."
        
        logging.error(f"Spotify error for user {discord_user_id}: {e}", exc_info=True)
        return jsonify({"status": "error", "message": error_message}), e.http_status or 500
    except Exception as e:
        logging.error(f"Unexpected error in web_control_play_spotify_search: {e}", exc_info=True)
        return jsonify({"status": "error", "message": f"An unexpected error occurred: {e}"}), 500


@app.route("/web_control/pause")
async def pause_web_control(): # Changed to async def
    current_session_id = session.get('session_id')
    discord_user_id = web_logged_in_users.get(current_session_id)
    if not discord_user_id:
        return jsonify({"status": "error", "message": "Please log in with Discord first."}), 401
    
    # Check if bot is playing from its queue first
    if voice_client and voice_client.is_playing():
        voice_client.pause()
        return jsonify({"status": "success", "message": "Bot queue playback paused."})
    
    # If not, try to pause Spotify playback
    sp_user = get_user_spotify_client(discord_user_id)
    if sp_user:
        try:
            await asyncio.to_thread(sp_user.pause_playback)
            return jsonify({"status": "success", "message": "Spotify playback paused."})
        except spotipy.exceptions.SpotifyException as e:
            logging.error(f"Error pausing Spotify from web: {e}", exc_info=True)
            return jsonify({"status": "error", "message": f"Spotify error: {e.msg}"}), e.http_status or 500
    
    return jsonify({"status": "warning", "message": "Nothing to pause."})


@app.route("/web_control/resume")
async def resume_web_control(): # Changed to async def
    current_session_id = session.get('session_id')
    discord_user_id = web_logged_in_users.get(current_session_id)
    if not discord_user_id:
        return jsonify({"status": "error", "message": "Please log in with Discord first."}), 401

    # Check if bot has a paused queue
    if voice_client and voice_client.is_paused():
        voice_client.resume()
        return jsonify({"status": "success", "message": "Bot queue playback resumed."})

    # If not, try to resume Spotify playback
    sp_user = get_user_spotify_client(discord_user_id)
    if sp_user:
        try:
            await asyncio.to_thread(sp_user.start_playback)
            return jsonify({"status": "success", "message": "Spotify playback resumed."})
        except spotipy.exceptions.SpotifyException as e:
            logging.error(f"Error resuming Spotify from web: {e}", exc_info=True)
            return jsonify({"status": "error", "message": f"Spotify error: {e.msg}"}), e.http_status or 500

    return jsonify({"status": "warning", "message": "Nothing to resume."})


@app.route("/web_control/stop")
async def stop_web_control(): # Changed to async def
    global queue, voice_client, current_playing_youtube_info
    
    current_session_id = session.get('session_id')
    discord_user_id = web_logged_in_users.get(current_session_id)
    if not discord_user_id:
        return jsonify({"status": "error", "message": "Please log in with Discord first."}), 401

    queue.clear()
    current_playing_youtube_info = {} # Clear current playing info
    if voice_client and (voice_client.is_playing() or voice_client.is_paused()):
        voice_client.stop()
        await asyncio.to_thread(voice_client.disconnect) # Disconnect from voice channel too
        voice_client = None
        return jsonify({"status": "success", "message": "Playback stopped and queue cleared."})
    
    # If no bot queue, try to stop Spotify playback
    sp_user = get_user_spotify_client(discord_user_id)
    if sp_user:
        try:
            await asyncio.to_thread(sp_user.pause_playback) # Spotify doesn't have a direct 'stop', pause is closest
            return jsonify({"status": "success", "message": "Spotify playback stopped."})
        except spotipy.exceptions.SpotifyException as e:
            logging.error(f"Error stopping Spotify from web: {e}", exc_info=True)
            return jsonify({"status": "error", "message": f"Spotify error: {e.msg}"}), e.http_status or 500

    return jsonify({"status": "warning", "message": "Nothing to stop."})

@app.route("/web_control/skip")
async def skip_web_control(): # Changed to async def
    global voice_client
    current_session_id = session.get('session_id')
    discord_user_id = web_logged_in_users.get(current_session_id)
    if not discord_user_id:
        return jsonify({"status": "error", "message": "Please log in with Discord first."}), 401

    # Prioritize skipping bot's internal queue
    if voice_client and voice_client.is_playing():
        voice_client.stop() # Stopping effectively skips by triggering the 'after' callback
        return jsonify({"status": "success", "message": "Bot queue song skipped."})

    # If not playing bot's queue, try to skip Spotify
    sp_user = get_user_spotify_client(discord_user_id)
    if sp_user:
        try:
            await asyncio.to_thread(sp_user.next_track)
            return jsonify({"status": "success", "message": "Spotify song skipped."})
        except spotipy.exceptions.SpotifyException as e:
            logging.error(f"Error skipping Spotify from web: {e}", exc_info=True)
            return jsonify({"status": "error", "message": f"Spotify error: {e.msg}"}), e.http_status or 500
    
    return jsonify({"status": "warning", "message": "Nothing to skip."})

@app.route("/web_control/skip_previous")
async def skip_previous_web_control(): # Added this route for previous track
    current_session_id = session.get('session_id')
    discord_user_id = web_logged_in_users.get(current_session_id)
    if not discord_user_id:
        return jsonify({"status": "error", "message": "Please log in with Discord first."}), 401
    
    sp_user = get_user_spotify_client(discord_user_id)
    if sp_user:
        try:
            await asyncio.to_thread(sp_user.previous_track)
            return jsonify({"status": "success", "message": "Previous song played on Spotify."})
        except spotipy.exceptions.SpotifyException as e:
            logging.error(f"Error playing previous Spotify song from web: {e}", exc_info=True)
            return jsonify({"status": "error", "message": f"Spotify error: {e.msg}"}), e.http_status or 500
    
    return jsonify({"status": "warning", "message": "Previous song function only works for Spotify."})


@app.route("/web_control/set_volume", methods=["GET"]) # Changed to GET to easily pass volume in URL
async def set_volume_web_control(): # Changed to async def
    global volume, voice_client
    vol_str = request.args.get("vol")
    if not vol_str:
        return jsonify({"status": "error", "message": "No volume level provided."}), 400
    
    try:
        new_volume = float(vol_str)
        if not (0.0 <= new_volume <= 2.0):
            return jsonify({"status": "error", "message": "Volume must be between 0.0 and 2.0"}), 400
        
        volume = new_volume
        if voice_client and voice_client.source:
            voice_client.source.volume = volume
        
        return jsonify({"status": "success", "message": f"Volume adjusted to {volume*100:.0f}%"})
    except ValueError:
        return jsonify({"status": "error", "message": "Invalid volume value."}), 400
    except Exception as e:
        logging.error(f"Error adjusting volume from web: {e}", exc_info=True)
        return jsonify({"status": "error", "message": f"An unexpected error occurred: {e}"}), 500


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
async def get_queue_data():
    # Return the global queue list (which stores URLs)
    # For simplicity, we return URLs only as PRD doesn't specify complex queue display
    return jsonify({"queue": queue}) 


# --- Run Flask + Discord bot ---
def run_web_app(): # Renamed function to clarify it's for Flask
    # Load Discord web login sessions when Flask app starts
    # Load Firebase data in the Flask thread using asyncio.run
    # to give it its own Event Loop
    logging.info("Loading user data from Firestore in web thread...")
    asyncio.run(load_all_user_data_from_firestore()) 
    logging.info("User data from Firestore loaded successfully in web thread.")

    # Flask app should run in its own thread
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)

if __name__ == "__main__":
    print("\n--- Starting Bot and Web Server ---")
    print("Ensure FFmpeg and Opus are installed for voice functionality")
    print("-----------------------------------\n")

    # Start Flask web server in a separate thread
    web_thread = threading.Thread(target=run_web_app) # Call the renamed function
    web_thread.start()
    
    # Run Discord bot (This is a Blocking Call)
    # bot.run() will create and run its own asyncio event loop
    bot.run(DISCORD_TOKEN)
