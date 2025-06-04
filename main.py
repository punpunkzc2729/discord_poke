import discord
from discord.ext import commands
from discord import app_commands
from flask import Flask, render_template, request, redirect, url_for, session
import threading
import logging
import os
from dotenv import load_dotenv
import spotipy
from spotipy.oauth2 import SpotifyOAuth

# โหลด config
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
YOUR_GUILD_ID = int(os.getenv("GUILD_ID"))

# Spotify OAuth
sp_oauth = SpotifyOAuth(
    client_id=os.getenv("SPOTIPY_CLIENT_ID"),
    client_secret=os.getenv("SPOTIPY_CLIENT_SECRET"),
    redirect_uri=os.getenv("SPOTIPY_REDIRECT_URI"),
    scope="user-read-playback-state,user-modify-playback-state"
)

# intents
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix="/", intents=intents)
tree = bot.tree
voice_client = None
queue = []
volume = 0.5

# Flask web
app = Flask(__name__)
app.secret_key = os.urandom(24)

# logging
logging.basicConfig(filename='bot.log', level=logging.INFO)

# === Opus check ===
if not discord.opus.is_loaded():
    try:
        discord.opus.load_opus('libopus.so')
    except Exception as e:
        logging.error(f"Failed to load opus: {e}")

@bot.event
async def on_ready():
    await tree.sync()
    await tree.sync(guild=discord.Object(id=YOUR_GUILD_ID))
    logging.info(f"Bot logged in as {bot.user}")

# === Spotify control ===
def get_spotify_token():
    token_info = sp_oauth.get_cached_token()
    if not token_info:
        return None
    return token_info['access_token']

def spotify_client():
    token = get_spotify_token()
    if not token:
        return None
    return spotipy.Spotify(auth=token)

@tree.command(name="join", description="เข้าห้อง Voice")
async def join(interaction: discord.Interaction):
    global voice_client
    if interaction.user.voice:
        channel = interaction.user.voice.channel
        voice_client = await channel.connect()
        await interaction.response.send_message(f"✅ เข้าห้อง {channel.name} แล้ว")
    else:
        await interaction.response.send_message("❌ ยังไม่อยู่ใน Voice")

@tree.command(name="leave", description="ออกจากห้อง Voice")
async def leave(interaction: discord.Interaction):
    global voice_client, queue
    if voice_client:
        await voice_client.disconnect()
        voice_client = None
        queue.clear()
        await interaction.response.send_message("✅ ออกจากห้อง Voice แล้ว")
    else:
        await interaction.response.send_message("❌ บอทยังไม่ได้ join")

@tree.command(name="play", description="เล่นเพลง Spotify")
@app_commands.describe(track_uri="Spotify track URI")
async def play(interaction: discord.Interaction, track_uri: str):
    sp = spotify_client()
    if not sp:
        await interaction.response.send_message("❌ ต้อง Login Spotify ก่อนใน web.")
        return

    sp.start_playback(uris=[track_uri])
    await interaction.response.send_message(f"🎵 กำลังเล่น: {track_uri}")

# === Web UI ===
@app.route("/")
def index():
    token = get_spotify_token()
    return render_template("index.html", is_spotify_linked=bool(token))

@app.route("/spotify_login")
def spotify_login():
    auth_url = sp_oauth.get_authorize_url()
    return redirect(auth_url)

@app.route("/callback")
def callback():
    code = request.args.get("code")
    if code:
        sp_oauth.get_access_token(code)
        return redirect("/")
    return "Authorization failed."

# === Run Flask + Discord bot ===
def run_web():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

if __name__ == "__main__":
    threading.Thread(target=run_web).start()
    bot.run(TOKEN)
