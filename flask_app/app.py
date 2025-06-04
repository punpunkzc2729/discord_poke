# web.py

from flask import Flask, render_template, request, redirect, url_for, session, flash
import os
import logging
import json
from dotenv import load_dotenv
import spotipy
from spotipy.oauth2 import SpotifyOAuth
import httpx # เพิ่ม httpx

# --- โหลดตัวแปรสภาพแวดล้อมจากไฟล์ .env ---
load_dotenv()

# --- ตั้งค่า Spotify API Credentials ---
SPOTIPY_CLIENT_ID = os.getenv("SPOTIPY_CLIENT_ID")
SPOTIPY_CLIENT_SECRET = os.getenv("SPOTIPY_CLIENT_SECRET")
SPOTIPY_REDIRECT_URI = os.getenv("SPOTIPY_REDIRECT_URI")
SPOTIPY_SCOPES = "user-read-playback-state user-modify-playback-state user-read-currently-playing playlist-read-private playlist-read-collaborative user-library-read"

# --- ตั้งค่า Discord OAuth Credentials ---
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
DISCORD_REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI")
DISCORD_OAUTH_SCOPES = "identify guilds"

# --- Global Dictionaries / Variables for Web App ---
# web_logged_in_users: Key: Flask Session ID, Value: Discord User ID
web_logged_in_users = {} 
# spotify_users_web_temp: ใช้เก็บ Spotify client ชั่วคราวสำหรับเว็บแอป (เพื่อการตรวจสอบเท่านั้น)
# ไม่ใช่ global spotify_users ที่บอทใช้จริง
spotify_users_web_temp = {} 


# --- ตั้งค่า Logger ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s:%(levelname)s:%(message)s',
    handlers=[
        logging.FileHandler("web.log"), # เปลี่ยนชื่อ log file เป็น web.log
        logging.StreamHandler()
    ]
)

# --- ตั้งค่า Flask App ---
app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = os.getenv("FLASK_SECRET_KEY")

# --- Helper Function: Save Spotify Tokens (สำหรับ web.py เท่านั้น) ---
# โดยจะอ่านจากไฟล์และอัปเดต ถ้ามีการเชื่อมต่อใหม่
def save_spotify_tokens_from_web(discord_user_id: int, token_info: dict):
    try:
        tokens_data = {}
        if os.path.exists("spotify_tokens.json"):
            with open("spotify_tokens.json", "r") as f:
                tokens_data = json.load(f)
        
        tokens_data[str(discord_user_id)] = token_info
        
        with open("spotify_tokens.json", "w") as f:
            json.dump(tokens_data, f, indent=4)
        logging.info(f"Saved Spotify token for user {discord_user_id} to file from web app.")
    except Exception as e:
        logging.error(f"Error saving Spotify tokens from web app: {e}", exc_info=True)

# Helper function: Check if Spotify linked (สำหรับแสดงบนเว็บ)
def is_spotify_linked_on_web(discord_user_id: int):
    # สำหรับ Flask (web.py) เราจะตรวจสอบจากไฟล์ spotify_tokens.json โดยตรง
    # ไม่ได้อ้างอิงจาก bot.py's spotify_users
    try:
        if os.path.exists("spotify_tokens.json"):
            with open("spotify_tokens.json", "r") as f:
                tokens_data = json.load(f)
            if str(discord_user_id) in tokens_data:
                # อาจจะลองสร้าง SpotifyOAuth object เพื่อตรวจสอบความถูกต้องเบื้องต้น
                auth_manager = SpotifyOAuth(
                    client_id=SPOTIPY_CLIENT_ID,
                    client_secret=SPOTIPY_CLIENT_SECRET,
                    redirect_uri=SPOTIPY_REDIRECT_URI,
                    scope=SPOTIPY_SCOPES,
                    token_info=tokens_data[str(discord_user_id)]
                )
                try:
                    sp_user = spotipy.Spotify(auth_manager=auth_manager)
                    sp_user.current_user() # ลองเรียก API เพื่อตรวจสอบ
                    return True
                except spotipy.exceptions.SpotifyException:
                    logging.warning(f"Spotify token for {discord_user_id} expired/invalid on web check.")
                    # ถ้า token หมดอายุ ก็ลบออกจากไฟล์เพื่อการ cleanup
                    del tokens_data[str(discord_user_id)]
                    with open("spotify_tokens.json", "w") as f:
                        json.dump(tokens_data, f, indent=4)
                    return False
        return False
    except Exception as e:
        logging.error(f"Error checking Spotify link status from web app: {e}", exc_info=True)
        return False

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
    if discord_user_id:
        is_discord_linked = True

    is_spotify_linked = False
    if discord_user_id:
        # เรียก helper function เพื่อตรวจสอบสถานะ Spotify จากไฟล์
        is_spotify_linked = is_spotify_linked_on_web(discord_user_id)

    return render_template(
        "index.html", 
        is_discord_linked=is_discord_linked,
        discord_user_id=discord_user_id,
        is_spotify_linked=is_spotify_linked
    )

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
async def discord_callback(): # Make this async for httpx
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
        # Use httpx to fetch token and user data
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
        
        discord_user_id = int(user_data["id"])
        discord_username = user_data["username"]

        current_session_id = session.get('session_id')
        if not current_session_id:
            current_session_id = os.urandom(16).hex()
            session['session_id'] = current_session_id
            logging.info(f"New Flask session created during Discord callback: {current_session_id}")

        web_logged_in_users[current_session_id] = discord_user_id
        session['discord_user_id_for_web'] = discord_user_id
        
        flash(f"✅ เข้าสู่ระบบ Discord สำเร็จ: {discord_username} ({discord_user_id})", "success")
        logging.info(f"Discord User {discord_username} ({discord_user_id}) logged in via web.")

    except httpx.HTTPStatusError as e:
        flash(f"❌ ข้อผิดพลาด HTTP จาก Discord API: {e.response.text}", "error")
        logging.error(f"Discord OAuth HTTP error: {e.response.text}", exc_info=True)
    except Exception as e:
        flash(f"❌ เกิดข้อผิดพลาดในการเชื่อมต่อ Discord: {e}", "error")
        logging.error(f"Unexpected error during Discord OAuth: {e}", exc_info=True)
    
    return redirect(url_for("index"))


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
async def spotify_callback(): # Make this async for httpx if used by spotipy internally for token refresh (unlikely directly, but good practice)
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
        token_info = auth_manager.get_access_token(code) # This is a synchronous call
        # บันทึก token ลงไฟล์ทันที
        save_spotify_tokens_from_web(discord_user_id, token_info)
        
        flash("✅ เชื่อมต่อ Spotify สำเร็จ!", "success")
        logging.info(f"User {discord_user_id} successfully linked Spotify via web.")
    except Exception as e:
        flash(f"❌ เกิดข้อผิดพลาดในการเชื่อมต่อ Spotify: {e}", "error")
        logging.error(f"Error during Spotify callback for user {discord_user_id}: {e}", exc_info=True)
    
    return redirect(url_for("index"))

# --- Flask Routes สำหรับควบคุม Spotify ---
# สำคัญ: ส่วนนี้จะใช้ไม่ได้จริงแล้ว เพราะ web.py ไม่ได้มี Spotify client ที่ active อยู่
# การควบคุม Spotify ควรทำผ่าน Discord Bot (bot.py)
# คุณสามารถลบส่วนนี้ออก หรือปรับให้มันส่งคำสั่งไปที่ Discord Bot ผ่านกลไกบางอย่าง (เช่น RabbitMQ, Redis Pub/Sub, HTTP POST to bot's internal API)
# เพื่อความเรียบง่ายในตอนนี้ เราจะลบมันออกไปก่อน เพื่อไม่ให้เกิดความเข้าใจผิด

# @app.route("/control_spotify/<action>")
# def control_spotify(action: str):
#    flash("❌ การควบคุม Spotify จากหน้าเว็บยังไม่รองรับในเวอร์ชันนี้ โปรดใช้คำสั่ง Discord แทน", "error")
#    return redirect(url_for("index"))

if __name__ == "__main__":
    # เมื่อรัน web.py โดยตรง จะใช้ app.run()
    # แต่เมื่อรันบน Railway ด้วย Gunicorn จะไม่ใช้ app.run()
    # Railway จะใช้ Procfile และ Gunicorn เป็นตัวจัดการเอง
    app.run(debug=True, port=os.getenv("PORT", 5000))