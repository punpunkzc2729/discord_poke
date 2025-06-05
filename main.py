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

# --- Load environment variables ---
load_dotenv()

# --- Spotify API Credentials ---
SPOTIPY_CLIENT_ID = os.getenv("SPOTIPY_CLIENT_ID")
SPOTIPY_CLIENT_SECRET = os.getenv("SPOTIPY_CLIENT_SECRET")
SPOTIPY_REDIRECT_URI = os.getenv("SPOTIPY_REDIRECT_URI")
SPOTIPY_SCOPES = "user-read-playback-state user-modify-playback-state user-read-currently-playing playlist-read-private playlist-read-collaborative user-library-read"

# --- Discord Bot Credentials ---
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
YOUR_GUILD_ID = int(os.environ["GUILD_ID"])
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
DISCORD_REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI")
DISCORD_OAUTH_SCOPES = "identify guilds"

# --- Global Variables ---
spotify_users = {}  # Key: Discord User ID, Value: Spotify client
web_logged_in_users = {}  # Key: Flask Session ID, Value: Discord User ID
voice_client = None

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
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree
bot_ready = asyncio.Event()

# --- Flask App Setup ---
app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = os.getenv("FLASK_SECRET_KEY") or os.urandom(24)

# --- Helper Functions ---
def get_user_spotify_client(discord_user_id: int):
    """Get Spotify client for a Discord user"""
    sp_client = spotify_users.get(discord_user_id)
    if sp_client:
        try:
            sp_client.current_user()
            return sp_client
        except spotipy.exceptions.SpotifyException as e:
            logging.warning(f"Spotify token expired for user {discord_user_id}: {e}")
            if discord_user_id in spotify_users:
                del spotify_users[discord_user_id]
                save_spotify_tokens()
            return None
    return None

def save_spotify_tokens():
    """Save Spotify tokens to file"""
    try:
        tokens_data = {}
        for user_id, sp_client in spotify_users.items():
            token_info = sp_client.auth_manager.get_cached_token()
            if token_info:
                tokens_data[str(user_id)] = token_info
        
        with open("spotify_tokens.json", "w") as f:
            json.dump(tokens_data, f, indent=4)
        logging.info("Saved Spotify tokens to file.")
    except Exception as e:
        logging.error(f"Error saving Spotify tokens: {e}", exc_info=True)

# --- Discord Bot Events ---
@bot.event
async def on_ready():
    # Load Opus for voice functionality
    if not discord.opus.is_loaded():
        try:
            discord.opus.load_opus('libopus.so')
            logging.info("Opus loaded successfully.")
        except Exception as e:
            logging.error(f"Failed to load opus: {e}")
            print(f"Failed to load opus: {e}")

    print(f"✅ Bot logged in as {bot.user}")
    logging.info(f"Bot logged in as {bot.user}")

    # Sync commands
    await tree.sync()
    guild_obj = discord.Object(id=YOUR_GUILD_ID)
    await tree.sync(guild=guild_obj)
    logging.info(f"Commands synced to guild: {YOUR_GUILD_ID}")

    # Load saved Spotify tokens
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
                    token_info=token_info
                )
                sp_user = spotipy.Spotify(auth_manager=auth_manager)
                try:
                    sp_user.current_user()
                    spotify_users[user_id] = sp_user
                    logging.info(f"Loaded Spotify token for user ID: {user_id}")
                except spotipy.exceptions.SpotifyException:
                    logging.warning(f"Spotify token for user {user_id} expired on startup")
                    tokens_data.pop(user_id_str, None)
            
            # Save cleaned tokens
            with open("spotify_tokens.json", "w") as f:
                json.dump(tokens_data, f, indent=4)
    except Exception as e:
        logging.error(f"Error loading Spotify tokens: {e}", exc_info=True)
    
    bot_ready.set()

# --- Discord Slash Commands ---
@tree.command(name="join", description="Join your voice channel")
async def join(interaction: discord.Interaction):
    global voice_client
    if interaction.user.voice:
        channel = interaction.user.voice.channel
        if voice_client and voice_client.is_connected():
            if voice_client.channel.id == channel.id:
                await interaction.response.send_message(f"✅ Already in **{channel.name}**")
            else:
                await voice_client.move_to(channel)
                await interaction.response.send_message(f"✅ Moved to **{channel.name}**")
        else:
            voice_client = await channel.connect()
            await interaction.response.send_message(f"✅ Joined **{channel.name}**")
    else:
        await interaction.response.send_message("❌ You're not in a voice channel")

@tree.command(name="leave", description="Leave voice channel")
async def leave(interaction: discord.Interaction):
    global voice_client
    if voice_client and voice_client.is_connected():
        if voice_client.is_playing():
            voice_client.stop()
        await voice_client.disconnect()
        voice_client = None
        await interaction.response.send_message("✅ Left voice channel")
    else:
        await interaction.response.send_message("❌ Not in a voice channel")

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
            "❌ Please link your Spotify account first using `/link_spotify`", 
            ephemeral=True
        )
        return

    await interaction.response.defer()

    try:
        track_uris = []
        context_uri = None
        response_msg = "🎶"

        # Check if it's a Spotify link
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
        else:  # Search by name
            results = await asyncio.to_thread(sp_user.search, q=query, type='track', limit=1)
            if not results['tracks']['items']:
                await interaction.followup.send("❌ Song not found on Spotify")
                return
            track = results['tracks']['items'][0]
            track_uris.append(track['uri'])
            response_msg += f" Playing: **{track['name']}** by **{track['artists'][0]['name']}**"

        # Get active device
        devices = await asyncio.to_thread(sp_user.devices)
        active_device_id = None
        for device in devices['devices']:
            if device['is_active']:
                active_device_id = device['id']
                break
        
        if not active_device_id:
            await interaction.followup.send("❌ No active Spotify client found. Please open your Spotify app")
            return

        # Start playback
        if context_uri:
            await asyncio.to_thread(sp_user.start_playback, device_id=active_device_id, context_uri=context_uri)
        else:
            await asyncio.to_thread(sp_user.start_playback, device_id=active_device_id, uris=track_uris)
        
        await interaction.followup.send(response_msg)

    except spotipy.exceptions.SpotifyException as e:
        if e.http_status == 401:
            await interaction.followup.send("❌ Spotify token expired. Please relink your account")
            if interaction.user.id in spotify_users:
                del spotify_users[interaction.user.id]
                save_spotify_tokens()
        elif e.http_status == 404 and "Device not found" in str(e):
            await interaction.followup.send("❌ No active Spotify client found")
        else:
            await interaction.followup.send(f"❌ Spotify error: {e}")
        logging.error(f"Spotify error for user {interaction.user.id}: {e}")

@tree.command(name="pause", description="Pause Spotify playback")
async def pause_spotify(interaction: discord.Interaction):
    sp_user = get_user_spotify_client(interaction.user.id)
    if not sp_user:
        await interaction.response.send_message("❌ Please link your Spotify account first", ephemeral=True)
        return
    
    try:
        await asyncio.to_thread(sp_user.pause_playback)
        await interaction.response.send_message("⏸️ Paused Spotify playback")
    except spotipy.exceptions.SpotifyException as e:
        await interaction.response.send_message(f"❌ Error pausing: {e}")

@tree.command(name="resume", description="Resume Spotify playback")
async def resume_spotify(interaction: discord.Interaction):
    sp_user = get_user_spotify_client(interaction.user.id)
    if not sp_user:
        await interaction.response.send_message("❌ Please link your Spotify account first", ephemeral=True)
        return
    
    try:
        await asyncio.to_thread(sp_user.start_playback)
        await interaction.response.send_message("▶️ Resumed Spotify playback")
    except spotipy.exceptions.SpotifyException as e:
        await interaction.response.send_message(f"❌ Error resuming: {e}")

@tree.command(name="skip", description="Skip current track")
async def skip_spotify(interaction: discord.Interaction):
    sp_user = get_user_spotify_client(interaction.user.id)
    if not sp_user:
        await interaction.response.send_message("❌ Please link your Spotify account first", ephemeral=True)
        return
    
    try:
        await asyncio.to_thread(sp_user.next_track)
        await interaction.response.send_message("⏭️ Skipped track")
    except spotipy.exceptions.SpotifyException as e:
        await interaction.response.send_message(f"❌ Error skipping: {e}")

@tree.command(name="speak", description="Make bot speak in voice channel")
@app_commands.describe(message="Message to speak")
async def speak(interaction: discord.Interaction, message: str):
    if not voice_client or not voice_client.is_connected():
        await interaction.response.send_message("❌ Bot not in voice channel. Use `/join` first")
        return
    
    await interaction.response.defer()
    try:
        tts_filename = f"tts_discord_{interaction.id}.mp3"
        await asyncio.to_thread(gTTS(message, lang='en').save, tts_filename)
        
        source = discord.FFmpegPCMAudio(tts_filename, executable="ffmpeg")
        voice_client.play(source, after=lambda e: asyncio.create_task(cleanup_audio(e, tts_filename)))
        
        await interaction.followup.send(f"🗣️ Speaking: **{message}**")

    except Exception as e:
        await interaction.followup.send(f"❌ Error speaking: {e}")
        logging.error(f"TTS error: {e}", exc_info=True)

async def cleanup_audio(error, filename):
    """Clean up TTS audio file"""
    if error:
        logging.error(f"TTS playback error: {error}")
    if os.path.exists(filename):
        os.remove(filename)

# --- Flask Routes ---
@app.route("/")
def index():
    current_session_id = session.get('session_id')
    if not current_session_id:
        current_session_id = os.urandom(16).hex()
        session['session_id'] = current_session_id

    discord_user_id = web_logged_in_users.get(current_session_id)
    is_discord_linked = bool(discord_user_id)
    is_spotify_linked = False

    if discord_user_id:
        try:
            future = asyncio.run_coroutine_threadsafe(
                _check_spotify_link_status(discord_user_id),
                bot.loop
            )
            is_spotify_linked = future.result(timeout=5)
        except:
            pass

    return render_template(
        "index.html",
        is_discord_linked=is_discord_linked,
        discord_user_id=discord_user_id,
        is_spotify_linked=is_spotify_linked
    )

async def _check_spotify_link_status(discord_user_id):
    """Check if user has linked Spotify"""
    return get_user_spotify_client(discord_user_id) is not None

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
        future = asyncio.run_coroutine_threadsafe(
            _fetch_discord_token_and_user(code),
            bot.loop
        )
        token_info, user_data = future.result(timeout=10)
        
        discord_user_id = int(user_data["id"])
        discord_username = user_data["username"]

        current_session_id = session.get('session_id')
        if not current_session_id:
            current_session_id = os.urandom(16).hex()
            session['session_id'] = current_session_id

        web_logged_in_users[current_session_id] = discord_user_id
        session['discord_user_id_for_web'] = discord_user_id
        
        flash(f"✅ Discord login successful: {discord_username}", "success")

    except Exception as e:
        flash(f"❌ Discord login error: {e}", "error")
        logging.error(f"Discord OAuth error: {e}", exc_info=True)
    
    return redirect(url_for("index"))

async def _fetch_discord_token_and_user(code):
    """Fetch Discord token and user data"""
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
        token_response = await client.post("https://discord.com/api/oauth2/token", data=data, headers=headers)
        token_response.raise_for_status()
        token_info = token_response.json()

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

    if logged_in_discord_user_id != discord_user_id_param:
        flash("❌ Discord User ID mismatch. Please login again", "error")
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
        flash(f"❌ Spotify OAuth error: {error}", "error")
        return redirect(url_for("index"))

    if not code or not discord_user_id:
        flash("❌ Missing authorization code or user ID", "error")
        return redirect(url_for("index"))

    auth_manager = SpotifyOAuth(
        client_id=SPOTIPY_CLIENT_ID,
        client_secret=SPOTIPY_CLIENT_SECRET,
        redirect_uri=SPOTIPY_REDIRECT_URI,
        scope=SPOTIPY_SCOPES,
    )

    try:
        future = asyncio.run_coroutine_threadsafe(
            asyncio.to_thread(auth_manager.get_access_token, code),
            bot.loop
        )
        token_info = future.result(timeout=10)
        
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
        flash("✅ Spotify linked successfully!", "success")
        
    except Exception as e:
        flash(f"❌ Spotify linking error: {e}", "error")
        logging.error(f"Spotify callback error: {e}", exc_info=True)
    
    return redirect(url_for("index"))

# --- Run Flask + Discord bot ---
def run_web():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)

if __name__ == "__main__":
    print("\n--- Initializing Bot & Web Server ---")
    print("Ensure FFmpeg and Opus are installed for voice functionality")
    print("---------------------------------------\n")

    web_thread = threading.Thread(target=run_web)
    web_thread.start()
    bot.run(DISCORD_TOKEN)