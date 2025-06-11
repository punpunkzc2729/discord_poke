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
import yt_dlp # Import yt-dlp for YouTube playback

# Suppress yt_dlp console output - REMOVED/COMMENTED OUT THIS LINE TO FIX THE TypeError
# yt_dlp.utils.bug_reports_message = lambda: ''

# --- Load environment variables ---
load_dotenv()

# --- Spotify API Credentials ---
SPOTIPY_CLIENT_ID = os.getenv("SPOTIPY_CLIENT_ID")
SPOTIPY_CLIENT_SECRET = os.getenv("SPOTIPY_CLIENT_SECRET")
SPOTIPY_REDIRECT_URI = os.getenv("SPOTIPY_REDIRECT_URI")
SPOTIPY_SCOPES = "user-read-playback-state user-modify-playback-state user-read-currently-playing playlist-read-private playlist-read-collaborative user-library-read"

# --- Discord Bot Credentials ---
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
YOUR_GUILD_ID = int(os.environ["GUILD_ID"]) # Ensure GUILD_ID is set in your .env
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
DISCORD_REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI")
DISCORD_OAUTH_SCOPES = "identify guilds"

# --- Global Variables ---
# Key: Discord User ID, Value: Spotify client
spotify_users = {}  
# Key: Flask Session ID, Value: Discord User ID
web_logged_in_users = {}  
voice_client = None
# Initialize queue and volume for web-based music controls
queue = []  
volume = 1.0

# --- Configure Logging ---
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
intents.voice_states = True # Required for bot to join/leave voice channels

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree
bot_ready = asyncio.Event() # Event to signal when bot is fully ready

# --- Flask App Setup ---
app = Flask(__name__, static_folder="static", template_folder="templates")
# Generate a secure secret key if not provided in environment
app.secret_key = os.getenv("FLASK_SECRET_KEY") or os.urandom(24) 

# --- Helper Functions ---
def get_user_spotify_client(discord_user_id: int):
    """
    Get Spotify client for a Discord user. 
    Checks for token expiration and removes if invalid.
    """
    sp_client = spotify_users.get(discord_user_id)
    if sp_client:
        try:
            # Test if the token is still valid
            sp_client.current_user() 
            return sp_client
        except spotipy.exceptions.SpotifyException as e:
            logging.warning(f"Spotify token expired for user {discord_user_id}: {e}")
            # If token expired, remove it from our cache
            if discord_user_id in spotify_users:
                del spotify_users[discord_user_id]
                save_spotify_tokens() # Save updated tokens to file
            return None
    return None

def save_spotify_tokens():
    """Save Spotify tokens to file (spotify_tokens.json)"""
    try:
        tokens_data = {}
        for user_id, sp_client in spotify_users.items():
            # Get cached token info directly from auth_manager
            token_info = sp_client.auth_manager.get_cached_token()
            if token_info:
                tokens_data[str(user_id)] = token_info
        
        with open("spotify_tokens.json", "w") as f:
            json.dump(tokens_data, f, indent=4)
        logging.info("Saved Spotify tokens to file.")
    except Exception as e:
        logging.error(f"Error saving Spotify tokens: {e}", exc_info=True)

def load_spotify_tokens():
    """Load Spotify tokens from file (spotify_tokens.json)"""
    try:
        if os.path.exists("spotify_tokens.json"):
            with open("spotify_tokens.json", "r") as f:
                tokens_data = json.load(f)
            # Iterate through loaded tokens and initialize Spotify clients
            # Use list() to allow deletion during iteration if a token is invalid
            for user_id_str, token_info in list(tokens_data.items()): 
                user_id = int(user_id_str)
                auth_manager = SpotifyOAuth(
                    client_id=SPOTIPY_CLIENT_ID,
                    client_secret=SPOTIPY_CLIENT_SECRET,
                    redirect_uri=SPOTIPY_REDIRECT_URI,
                    scope=SPOTIPY_SCOPES,
                    # token_info is no longer passed here directly
                )
                # Set the cached token after initializing SpotifyOAuth
                auth_manager.set_cached_token(token_info)

                sp_user = spotipy.Spotify(auth_manager=auth_manager)
                try:
                    # Validate token by making a simple API call
                    sp_user.current_user() 
                    spotify_users[user_id] = sp_user
                    logging.info(f"Loaded valid Spotify token for user ID: {user_id}")
                except spotipy.exceptions.SpotifyException:
                    logging.warning(f"Spotify token for user {user_id} expired on startup, removing.")
                    del tokens_data[user_id_str] # Remove expired token from data
            
            # Save cleaned tokens back to file (only valid ones)
            with open("spotify_tokens.json", "w") as f:
                json.dump(tokens_data, f, indent=4)
    except Exception as e:
        logging.error(f"Error loading Spotify tokens: {e}", exc_info=True)

# Callback function for after audio playback finishes
async def _after_playback_cleanup(error, channel_id):
    if error:
        logging.error(f"Audio playback error: {error}")
        # Optionally send error message to Discord channel
        channel = bot.get_channel(channel_id)
        if channel:
            await channel.send(f"❌ Error during playback: {error}")
    
    # Try to play the next song in the queue
    if queue and voice_client and voice_client.is_connected() and not voice_client.is_playing():
        channel = bot.get_channel(channel_id)
        if channel:
            await _play_next_in_queue(channel)
    elif not queue and voice_client and voice_client.is_connected() and not voice_client.is_playing():
        logging.info("Queue finished.")
        channel = bot.get_channel(channel_id)
        if channel:
            await channel.send("✅ Queue finished!")


async def _play_next_in_queue(channel: discord.VoiceChannel):
    """Plays the next song in the queue, supporting YouTube URLs."""
    global voice_client, queue, volume

    if not voice_client or not voice_client.is_connected():
        logging.warning("Bot not in a voice channel to play queue.")
        return

    if voice_client.is_playing():
        voice_client.stop()

    if not queue:
        logging.info("Queue is empty.")
        await channel.send("✅ Queue finished!")
        return

    url = queue.pop(0) # Get the next URL from the queue
    logging.info(f"Attempting to play from queue: {url}")
    
    # Check if the URL is a YouTube link
    if "youtube.com/" in url or "youtu.be/" in url:
        ydl_opts = {
            'format': 'bestaudio/best', # Select best audio format
            'noplaylist': True,        # Don't download entire playlists
            'default_search': 'ytsearch', # If just a name, search YouTube
            'source_address': '0.0.0.0', # Resolve issues on some Linux systems
            'verbose': False, # Set to True for debugging yt-dlp output
            'extract_flat': 'in_playlist', # For playlists, extract info without fetching all
        }

        try:
            # Use asyncio.to_thread for blocking yt_dlp operations
            # This ensures the Discord bot's event loop isn't blocked
            loop = asyncio.get_event_loop()
            # The lambda function should just return the result of extract_info
            info = await loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(ydl_opts).extract_info(url, download=False))
            
            # If the query was a search term or a playlist, info might contain 'entries'
            if 'entries' in info and info['entries']:
                # Pick the first entry for simplicity or iterate for playlist support
                selected_info = info['entries'][0]
            else:
                selected_info = info

            audio_url = selected_info['url']
            title = selected_info.get('title', 'Unknown Title')
            
            # Prepare FFmpeg audio source
            source = discord.FFmpegPCMAudio(audio_url, executable="ffmpeg")
            
            # Play the audio and schedule cleanup/next song
            voice_client.play(source, after=lambda e: asyncio.run_coroutine_threadsafe(
                _after_playback_cleanup(e, channel.id), bot.loop))
            
            await channel.send(f"🎶 Playing YouTube: **{title}**")

        except Exception as e:
            logging.error(f"Error playing YouTube item {url}: {e}", exc_info=True)
            await channel.send(f"❌ Could not play YouTube video: {url}. Error: {e}")
            await asyncio.sleep(1) # Small delay before trying next
            # Automatically try to play the next song in the queue if current failed
            if queue and voice_client and voice_client.is_connected():
                await _play_next_in_queue(channel)
            elif not queue:
                await channel.send("✅ Queue finished!")
    else:
        # Handle non-YouTube URLs or other types of media if needed
        # For now, just log and skip
        logging.info(f"Playback for non-YouTube URL {url} is not fully implemented yet. Skipping.")
        await channel.send(f"⚠️ Playback for non-YouTube URL {url} is not fully implemented yet. Skipping.")
        await asyncio.sleep(1)
        # Automatically try to play the next song in the queue
        if queue and voice_client and voice_client.is_connected():
            await _play_next_in_queue(channel)
        elif not queue:
            await channel.send("✅ Queue finished!")


# --- Discord Bot Events ---
@bot.event
async def on_ready():
    # Load Opus for voice functionality
    if not discord.opus.is_loaded():
        try:
            # Attempt to load Opus library (adjust 'libopus.so' for your OS if needed)
            discord.opus.load_opus('libopus.so') # For Linux
            # discord.opus.load_opus('opus.dll') # For Windows
            # discord.opus.load_opus('libopus.dylib') # For macOS
            logging.info("Opus loaded successfully.")
        except Exception as e:
            logging.error(f"Failed to load opus: {e}. Voice commands may not work.")
            print(f"Failed to load opus: {e}. Please ensure it's installed and accessible.")

    print(f"✅ Bot logged in as {bot.user}")
    logging.info(f"Bot logged in as {bot.user}")

    # Sync commands globally and to specific guild for faster updates during development
    try:
        # Sync global commands
        await tree.sync() 
        logging.info("Global commands synced.")
        
        # Sync commands to a specific guild
        guild_obj = discord.Object(id=YOUR_GUILD_ID)
        await tree.sync(guild=guild_obj)
        logging.info(f"Commands synced to guild: {YOUR_GUILD_ID}")
    except Exception as e:
        logging.error(f"Failed to sync commands: {e}", exc_info=True)

    # Load saved Spotify tokens on bot startup
    load_spotify_tokens()
    
    # Signal that bot is ready
    bot_ready.set()
    logging.info("Bot is fully ready.")

# --- Discord Slash Commands ---

@tree.command(name="join", description="Join your voice channel")
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
                logging.error(f"Failed to connect to voice channel: {e}")
                await interaction.response.send_message(f"❌ Could not connect to voice channel: {e}", ephemeral=True)
            except Exception as e:
                logging.error(f"An unexpected error occurred while joining voice: {e}")
                await interaction.response.send_message(f"❌ An unexpected error occurred: {e}", ephemeral=True)
    else:
        await interaction.response.send_message("❌ You're not in a voice channel", ephemeral=True)

@tree.command(name="leave", description="Leave voice channel")
async def leave(interaction: discord.Interaction):
    global voice_client
    if voice_client and voice_client.is_connected():
        if voice_client.is_playing():
            voice_client.stop()
        await voice_client.disconnect()
        voice_client = None
        await interaction.response.send_message("✅ Left voice channel", ephemeral=True)
    else:
        await interaction.response.send_message("❌ Not in a voice channel", ephemeral=True)

@tree.command(name="link_spotify", description="Link your Spotify account")
async def link_spotify(interaction: discord.Interaction):
    await interaction.response.send_message(
        f"🔗 To link your Spotify account, please visit:\n"
        f"**{url_for('index', _external=True)}**\n"
        f"Login with Discord first, then connect Spotify", 
        ephemeral=True
    )

@tree.command(name="play", description="Play music from Spotify")
@app_commands.describe(query="Song name, artist, or Spotify link")
async def play(interaction: discord.Interaction, query: str):
    sp_user = get_user_spotify_client(interaction.user.id)
    if not sp_user:
        await interaction.response.send_message(
            "❌ Please link your Spotify account first using /link_spotify", 
            ephemeral=True
        )
        return

    await interaction.response.defer() # Defer the response as Spotify API calls can take time

    try:
        track_uris = []
        context_uri = None
        response_msg = "🎶"

        # Check if it's a Spotify link (track, playlist, or album)
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
        else:  # Search by name if not a direct link
            results = await asyncio.to_thread(sp_user.search, q=query, type='track', limit=1)
            if not results['tracks']['items']:
                await interaction.followup.send("❌ Song not found on Spotify")
                return
            track = results['tracks']['items'][0]
            track_uris.append(track['uri'])
            response_msg += f" Playing: **{track['name']}** by **{track['artists'][0]['name']}**"

        # Get active device to play music on
        devices = await asyncio.to_thread(sp_user.devices)
        active_device_id = None
        for device in devices['devices']:
            if device['is_active']:
                active_device_id = device['id']
                break
        
        if not active_device_id:
            await interaction.followup.send("❌ No active Spotify client found. Please open your Spotify app and play something there first, or select a device to play on in Spotify.")
            return

        # Start playback on the active device
        if context_uri: # For playlists and albums
            await asyncio.to_thread(sp_user.start_playback, device_id=active_device_id, context_uri=context_uri)
        else: # For individual tracks
            await asyncio.to_thread(sp_user.start_playback, device_id=active_device_id, uris=track_uris)
        
        await interaction.followup.send(response_msg)

    except spotipy.exceptions.SpotifyException as e:
        if e.http_status == 401:
            await interaction.followup.send("❌ Spotify token expired. Please relink your account using /link_spotify.")
            if interaction.user.id in spotify_users:
                del spotify_users[interaction.user.id]
                save_spotify_tokens()
        elif e.http_status == 404 and "Device not found" in str(e):
            await interaction.followup.send("❌ No active Spotify client found. Please open your Spotify app.")
        elif e.http_status == 403: # Forbidden error, often related to premium or playback restrictions
            await interaction.followup.send("❌ Spotify playback error: You might need a Spotify Premium account or there's a restriction with playback.")
        else:
            await interaction.followup.send(f"❌ Spotify error: {e}. Please try again.")
        logging.error(f"Spotify error for user {interaction.user.id}: {e}", exc_info=True)
    except Exception as e:
        await interaction.followup.send(f"❌ An unexpected error occurred: {e}")
        logging.error(f"An unexpected error in play command: {e}", exc_info=True)


@tree.command(name="pause", description="Pause Spotify playback")
async def pause_spotify(interaction: discord.Interaction):
    sp_user = get_user_spotify_client(interaction.user.id)
    if not sp_user:
        await interaction.response.send_message("❌ Please link your Spotify account first using /link_spotify", ephemeral=True)
        return
    
    try:
        await asyncio.to_thread(sp_user.pause_playback)
        await interaction.response.send_message("⏸️ Paused Spotify playback", ephemeral=True)
    except spotipy.exceptions.SpotifyException as e:
        await interaction.response.send_message(f"❌ Error pausing Spotify: {e}", ephemeral=True)
        logging.error(f"Error pausing Spotify for user {interaction.user.id}: {e}", exc_info=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ An unexpected error occurred: {e}", ephemeral=True)
        logging.error(f"An unexpected error in pause command: {e}", exc_info=True)

@tree.command(name="resume", description="Resume Spotify playback")
async def resume_spotify(interaction: discord.Interaction):
    sp_user = get_user_spotify_client(interaction.user.id)
    if not sp_user:
        await interaction.response.send_message("❌ Please link your Spotify account first using /link_spotify", ephemeral=True)
        return
    
    try:
        await asyncio.to_thread(sp_user.start_playback)
        await interaction.response.send_message("▶️ Resumed Spotify playback", ephemeral=True)
    except spotipy.exceptions.SpotifyException as e:
        await interaction.response.send_message(f"❌ Error resuming Spotify: {e}", ephemeral=True)
        logging.error(f"Error resuming Spotify for user {interaction.user.id}: {e}", exc_info=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ An unexpected error occurred: {e}", ephemeral=True)
        logging.error(f"An unexpected error in resume command: {e}", exc_info=True)

@tree.command(name="skip", description="Skip current track")
async def skip_spotify(interaction: discord.Interaction):
    sp_user = get_user_spotify_client(interaction.user.id)
    if not sp_user:
        await interaction.response.send_message("❌ Please link your Spotify account first using /link_spotify", ephemeral=True)
        return
    
    try:
        await asyncio.to_thread(sp_user.next_track)
        await interaction.response.send_message("⏭️ Skipped track", ephemeral=True)
    except spotipy.exceptions.SpotifyException as e:
        await interaction.response.send_message(f"❌ Error skipping Spotify: {e}", ephemeral=True)
        logging.error(f"Error skipping Spotify for user {interaction.user.id}: {e}", exc_info=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ An unexpected error occurred: {e}", ephemeral=True)
        logging.error(f"An unexpected error in skip command: {e}", exc_info=True)

@tree.command(name="speak", description="Make bot speak in voice channel / ให้บอทพูดในช่องเสียง")
@app_commands.describe(message="Message to speak / ข้อความที่จะให้บอทพูด")
@app_commands.describe(lang="Language (e.g., 'en', 'th') / ภาษา (เช่น 'en', 'th')")
async def speak(interaction: discord.Interaction, message: str, lang: str = 'en'):
    global voice_client
    if not voice_client or not voice_client.is_connected():
        await interaction.response.send_message("❌ Bot not in voice channel. Use /join first / บอทยังไม่ได้เข้าช่องเสียง ใช้ /join ก่อน", ephemeral=True)
        return
    
    await interaction.response.defer() # Defer the response as TTS generation can take time
    try:
        # Generate unique filename for TTS audio
        tts_filename = f"tts_discord_{interaction.id}.mp3"
        await asyncio.to_thread(gTTS(message, lang=lang).save, tts_filename) # Use asyncio.to_thread for blocking TTS operation
        
        # Play the generated audio file
        source = discord.FFmpegPCMAudio(tts_filename, executable="ffmpeg")
        voice_client.play(source, after=lambda e: asyncio.create_task(cleanup_audio(e, tts_filename))) # Cleanup after playback
        
        await interaction.followup.send(f"🗣️ Speaking: **{message}** (Lang: {lang})")

    except Exception as e:
        await interaction.followup.send(f"❌ Error speaking: {e}")
        logging.error(f"TTS error: {e}", exc_info=True)

async def cleanup_audio(error, filename):
    """Clean up TTS audio file after playback"""
    if error:
        logging.error(f"TTS playback error: {error}")
    # Ensure file exists before attempting to remove
    if os.path.exists(filename):
        os.remove(filename)
        logging.info(f"Cleaned up audio file: {filename}")

@tree.command(name="wake", description="ปลุกผู้ใช้ด้วย DM", guild=discord.Object(id=YOUR_GUILD_ID))
@app_commands.describe(user="เลือกผู้ใช้")
async def wake(interaction: discord.Interaction, user: discord.User):
    try:
        await user.send(f"⏰ คุณถูก {interaction.user.display_name} ปลุก! ตื่นนน!")
        await interaction.response.send_message(f"✅ ปลุก {user.name} แล้ว", ephemeral=True)
        logging.info(f"{interaction.user.display_name} woke up {user.name}.")
    except discord.Forbidden:
        await interaction.response.send_message(f"❌ ไม่สามารถส่ง DM ถึง {user.name} ได้ (อาจจะปิด DM หรือเป็นบอท)", ephemeral=True)
        logging.warning(f"Failed to send DM to {user.name} (Forbidden).")
    except Exception as e:
        await interaction.response.send_message(f"❌ เกิดข้อผิดพลาดในการส่ง DM: {e}", ephemeral=True)
        logging.error(f"Failed to wake user: {e}", exc_info=True)


# --- Flask Routes ---
# Removed duplicate index, add, play, pause, resume, stop, skip, volume_up, volume_down routes here
# The existing ones below are more complete with Discord/Spotify linking logic.

@app.route("/")
def index():
    # Get or create a unique session ID for the user
    current_session_id = session.get('session_id')
    if not current_session_id:
        current_session_id = os.urandom(16).hex()
        session['session_id'] = current_session_id

    # Get Discord user ID from session
    discord_user_id = web_logged_in_users.get(current_session_id)
    is_discord_linked = bool(discord_user_id)
    is_spotify_linked = False

    # Check Spotify link status if Discord is linked
    if discord_user_id:
        try:
            # Run the async check in the bot's event loop
            future = asyncio.run_coroutine_threadsafe(
                _check_spotify_link_status(discord_user_id),
                bot.loop
            )
            is_spotify_linked = future.result(timeout=5) # Wait for result with timeout
        except Exception as e:
            logging.error(f"Error checking Spotify link status for web user {discord_user_id}: {e}")
            is_spotify_linked = False # Assume not linked on error

    return render_template(
        "index.html",
        is_discord_linked=is_discord_linked,
        discord_user_id=discord_user_id,
        is_spotify_linked=is_spotify_linked
    )

async def _check_spotify_link_status(discord_user_id):
    """Internal async function to check if user has linked Spotify"""
    # Wait until bot is ready and loop is running
    await bot_ready.wait() 
    return get_user_spotify_client(discord_user_id) is not None

@app.route("/login/discord")
def login_discord():
    # Construct Discord OAuth URL
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

    if error:
        flash(f"❌ Discord OAuth error: {error}", "error")
        return redirect(url_for("index"))

    if not code:
        flash("❌ No authorization code received", "error")
        return redirect(url_for("index"))

    try:
        # Run the async token and user fetch in the bot's event loop
        future = asyncio.run_coroutine_threadsafe(
            _fetch_discord_token_and_user(code),
            bot.loop
        )
        token_info, user_data = future.result(timeout=10) # Wait for result
        
        discord_user_id = int(user_data["id"])
        discord_username = user_data["username"]

        current_session_id = session.get('session_id')
        if not current_session_id: # Should ideally be set by index, but a fallback
            current_session_id = os.urandom(16).hex()
            session['session_id'] = current_session_id

        web_logged_in_users[current_session_id] = discord_user_id
        # Also store in Flask session for easier direct access within Flask routes
        session['discord_user_id_for_web'] = discord_user_id 
        
        flash(f"✅ Discord login successful: {discord_username}", "success")

    except Exception as e:
        flash(f"❌ Discord login error: {e}", "error")
        logging.error(f"Discord OAuth error: {e}", exc_info=True)
    
    return redirect(url_for("index"))

async def _fetch_discord_token_and_user(code):
    """Fetch Discord token and user data using httpx (async HTTP client)"""
    data = {
        "client_id": DISCORD_CLIENT_ID,
        "client_secret": DISCORD_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": DISCORD_REDIRECT_URI,
        "scope": DISCORD_OAUTH_SCOPES,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    
    async with httpx.AsyncClient() as client:
        # Fetch token
        token_response = await client.post("https://discord.com/api/oauth2/token", data=data, headers=headers)
        token_response.raise_for_status() # Raise HTTPStatusError for bad responses (4xx or 5xx)
        token_info = token_response.json()

        # Fetch user data using the access token
        access_token = token_info["access_token"]
        user_headers = {"Authorization": f"Bearer {access_token}"}
        user_response = await client.get("https://discord.com/api/v10/users/@me", headers=user_headers)
        user_response.raise_for_status()
        user_data = user_response.json()
        
        return token_info, user_data

@app.route("/login/spotify/<int:discord_user_id_param>")
def login_spotify_web(discord_user_id_param: int):
    current_session_id = session.get('session_id')
    logged_in_discord_user_id = web_logged_in_users.get(current_session_id)

    # Security check: Ensure the Discord user ID in the URL matches the logged-in user
    if logged_in_discord_user_id != discord_user_id_param:
        flash("❌ Discord User ID mismatch. Please login with Discord again.", "error")
        return redirect(url_for("index"))

    # Initialize Spotify OAuth manager without token_info
    auth_manager = SpotifyOAuth(
        client_id=SPOTIPY_CLIENT_ID,
        client_secret=SPOTIPY_CLIENT_SECRET,
        redirect_uri=SPOTIPY_REDIRECT_URI,
        scope=SPOTIPY_SCOPES,
        show_dialog=True # Force user to re-authorize, good for testing
    )
    auth_url = auth_manager.get_authorize_url()
    
    # Store Discord user ID in session to retrieve after Spotify callback
    session['spotify_auth_discord_user_id'] = discord_user_id_param
    return redirect(auth_url)

@app.route("/callback/spotify")
def spotify_callback():
    code = request.args.get("code")
    error = request.args.get("error")
    # Retrieve Discord user ID from session
    discord_user_id = session.pop('spotify_auth_discord_user_id', None) 
    
    if error:
        flash(f"❌ Spotify OAuth error: {error}", "error")
        return redirect(url_for("index"))

    if not code or not discord_user_id:
        flash("❌ Missing authorization code or Discord user ID for Spotify linking. Please try again.", "error")
        return redirect(url_for("index"))

    try:
        # Initialize Spotify OAuth manager
        auth_manager = SpotifyOAuth(
            client_id=SPOTIPY_CLIENT_ID,
            client_secret=SPOTIPY_CLIENT_SECRET,
            redirect_uri=SPOTIPY_REDIRECT_URI,
            scope=SPOTIPY_SCOPES,
        )

        # Exchange authorization code for access token in bot's event loop
        # This call will also set the cached token within the auth_manager instance
        future = asyncio.run_coroutine_threadsafe(
            asyncio.to_thread(auth_manager.get_access_token, code),
            bot.loop
        )
        token_info = future.result(timeout=10) # Get token_info, but it's already cached in auth_manager

        # Create a new Spotify client using the auth_manager that now has the token
        sp_user = spotipy.Spotify(auth_manager=auth_manager)
        spotify_users[discord_user_id] = sp_user # Store the Spotify client
        
        save_spotify_tokens() # Save tokens to file for persistence
        flash("✅ Spotify linked successfully!", "success")
        
    except Exception as e:
        flash(f"❌ Spotify linking error: {e}. Please ensure your redirect URI is correct in Spotify Developer Dashboard.", "error")
        logging.error(f"Spotify callback error for user {discord_user_id}: {e}", exc_info=True)
    
    return redirect(url_for("index"))

# --- Flask routes for controlling bot from web ---
@app.route("/web_control/add", methods=["POST"])
def add_web_queue():
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
    # Ensure bot_ready event is set before accessing bot.loop
    if not bot_ready.is_set():
        flash("Bot is not ready yet. Please wait a moment.", "warning")
        return redirect("/")

    if voice_client and not voice_client.is_playing():
        # Schedule the async function to run in the bot's event loop
        # Ensure voice_client.channel exists before trying to access its ID
        if voice_client.channel:
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
    if voice_client and voice_client.is_playing():
        voice_client.pause()
        flash("Playback paused.", "info")
        logging.info("Paused via web.")
    else:
        flash("Nothing to pause.", "warning")
    return redirect("/")

@app.route("/web_control/resume")
def resume_web_control():
    if voice_client and voice_client.is_paused():
        voice_client.resume()
        flash("Playback resumed.", "info")
        logging.info("Resumed via web.")
    else:
        flash("Nothing to resume.", "warning")
    return redirect("/")

@app.route("/web_control/stop")
def stop_web_control():
    global queue
    queue.clear() # Clear the queue
    if voice_client and (voice_client.is_playing() or voice_client.is_paused()):
        voice_client.stop() # Stop current playback
        flash("Playback stopped and queue cleared.", "info")
        logging.info("Stopped via web and cleared queue.")
    else:
        flash("Nothing to stop.", "warning")
    return redirect("/")

@app.route("/web_control/skip")
def skip_web_control():
    if voice_client and voice_client.is_playing():
        voice_client.stop() # Stopping effectively skips by triggering the 'after' callback if used for queue
        flash("Track skipped.", "info")
        logging.info("Skipped via web.")
    else:
        flash("Nothing to skip.", "warning")
    return redirect("/")

@app.route("/web_control/volume_up")
def volume_up_web_control():
    global volume
    # Cap volume at 2.0 (200%)
    volume = min(volume + 0.1, 2.0)
    if voice_client and voice_client.source: # Ensure there's a source to adjust volume
        voice_client.source.volume = volume
    flash(f"Volume increased to {volume*100:.0f}%", "info")
    logging.info(f"Volume up: {volume}")
    return redirect("/")

@app.route("/web_control/volume_down")
def volume_down_web_control():
    global volume
    # Minimum volume 0.1 (10%) to avoid complete silence without stop
    volume = max(volume - 0.1, 0.1) 
    if voice_client and voice_client.source:
        voice_client.source.volume = volume
    flash(f"Volume decreased to {volume*100:.0f}%", "info")
    logging.info(f"Volume down: {volume}")
    return redirect("/")

# --- Run Flask + Discord bot ---
def run_web():
    # Flask app should run in its own thread
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)

if __name__ == "__main__":
    print("\n--- Initializing Bot & Web Server ---")
    print("Ensure FFmpeg and Opus are installed for voice functionality")
    print("---------------------------------------\n")

    # Start Flask web server in a separate thread
    web_thread = threading.Thread(target=run_web)
    web_thread.start()
    
    # Run Discord bot (this is a blocking call)
    bot.run(DISCORD_TOKEN)
