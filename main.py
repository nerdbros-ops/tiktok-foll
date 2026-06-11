from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import httpx
import os

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── KONFIGURASI ─────────────────────────────────────────────────────────────
APIFY_TOKEN      = os.environ.get("APIFY_TOKEN", "")
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TIKTOK_USERNAME  = os.environ.get("TIKTOK_USERNAME", "")
TARGET_FOLLOWERS = int(os.environ.get("TARGET_FOLLOWERS", "2000000"))

# Pastikan notif "target tercapai" hanya dikirim sekali
target_reached = False


# ─── AMBIL FOLLOWER DARI APIFY ───────────────────────────────────────────────

async def get_tiktok_followers(username: str) -> dict:
    username = username.lstrip("@")
    url = "https://api.apify.com/v2/acts/clockworks~free-tiktok-scraper/run-sync-get-dataset-items"
    params = {"token": APIFY_TOKEN}
    payload = {
        "profiles": [f"https://www.tiktok.com/@{username}"],
        "resultsPerPage": 0,
        "shouldDownloadVideos": False,
        "shouldDownloadCovers": False,
    }

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(url, params=params, json=payload)

        if resp.status_code != 200:
            raise Exception(f"Apify error {resp.status_code}: {resp.text[:200]}")

        items = resp.json()
        if not items:
            raise Exception(f"Data tidak ditemukan untuk @{username}")

        profile      = items[0]
        followers    = profile.get("followersCount", 0) or profile.get("authorMeta", {}).get("fans", 0)
        display_name = profile.get("authorMeta", {}).get("name") or profile.get("authorMeta", {}).get("nickName", username)

        return {
            "username": username,
            "display_name": display_name,
            "followers": followers,
            "followers_formatted": format_number(followers),
            "profile_url": f"https://www.tiktok.com/@{username}",
        }


def format_number(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    elif n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def progress_bar(pct: float) -> str:
    filled = int(pct / 10)
    return "🟩" * filled + "⬜" * (10 - filled)


# ─── KIRIM TELEGRAM ──────────────────────────────────────────────────────────

async def send_telegram(message: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.post(url, json=payload)
            return resp.status_code == 200
        except Exception:
            return False


# ─── JOB UTAMA (dijalankan otomatis setiap 15 menit) ────────────────────────

async def monitor_job():
    global target_reached

    if not TIKTOK_USERNAME:
        print("[monitor] TIKTOK_USERNAME belum diset, skip.")
        return

    if target_reached:
        print("[monitor] Target sudah tercapai sebelumnya, scheduler berhenti.")
        return

    print(f"[monitor] Mengecek @{TIKTOK_USERNAME}...")

    try:
        data        = await get_tiktok_followers(TIKTOK_USERNAME)
        followers   = data["followers"]
        target      = TARGET_FOLLOWERS
        pct         = round(min(100, (followers / target) * 100), 2)
        remaining   = max(0, target - followers)
        reached     = followers >= target

        print(f"[monitor] @{TIKTOK_USERNAME}: {followers:,} followers ({pct}%)")

        if reached and not target_reached:
            # Notif SPESIAL — target tercapai, kirim sekali lalu berhenti
            target_reached = True
            msg = (
                f"🎉 <b>TARGET TERCAPAI!</b>\n\n"
                f"👤 <b>@{data['username']}</b> ({data['display_name']})\n"
                f"👥 Followers: <b>{followers:,}</b> ({data['followers_formatted']})\n"
                f"🎯 Target: <b>{target:,}</b>\n\n"
                f"{progress_bar(100)} 100%\n\n"
                f"🔗 {data['profile_url']}"
            )
            await send_telegram(msg)
            print("[monitor] Target tercapai! Notif spesial dikirim.")

        else:
            # Update RUTIN setiap 15 menit
            status = "✅ Sudah tercapai!" if reached else "⏳ Masih monitoring..."
            msg = (
                f"📊 <b>Update Follower</b>\n\n"
                f"👤 <b>@{data['username']}</b> ({data['display_name']})\n"
                f"👥 Followers: <b>{followers:,}</b> ({data['followers_formatted']})\n"
                f"🎯 Target: {target:,}\n"
                f"📉 Sisa: <b>{remaining:,}</b> lagi\n\n"
                f"{progress_bar(pct)} {pct}%\n\n"
                f"{status}"
            )
            await send_telegram(msg)
            print(f"[monitor] Update rutin dikirim ke Telegram.")

    except Exception as e:
        print(f"[monitor] Error: {e}")
        await send_telegram(f"⚠️ <b>Monitor Error</b>\n\nGagal mengecek @{TIKTOK_USERNAME}:\n{str(e)}")


# ─── STARTUP & SHUTDOWN ──────────────────────────────────────────────────────

scheduler = AsyncIOScheduler()

@app.on_event("startup")
async def startup():
    # Jalankan sekali langsung saat server start
    await monitor_job()
    # Lalu jadwalkan setiap 15 menit
    scheduler.add_job(monitor_job, "interval", minutes=15, id="tiktok_monitor")
    scheduler.start()
    print(f"[scheduler] Berjalan — cek setiap 15 menit untuk @{TIKTOK_USERNAME}")

@app.on_event("shutdown")
async def shutdown():
    scheduler.shutdown()


# ─── ENDPOINTS ───────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {
        "status": "aktif ✓",
        "monitoring": TIKTOK_USERNAME or "belum diset",
        "target": TARGET_FOLLOWERS,
        "target_reached": target_reached,
    }

@app.get("/status")
async def get_status():
    """Cek status monitoring terkini."""
    if not TIKTOK_USERNAME:
        raise HTTPException(status_code=400, detail="TIKTOK_USERNAME belum diset di Railway.")
    try:
        data      = await get_tiktok_followers(TIKTOK_USERNAME)
        followers = data["followers"]
        pct       = round(min(100, (followers / TARGET_FOLLOWERS) * 100), 2)
        return {
            **data,
            "target": TARGET_FOLLOWERS,
            "progress_pct": pct,
            "remaining": max(0, TARGET_FOLLOWERS - followers),
            "reached": followers >= TARGET_FOLLOWERS,
            "target_reached_notified": target_reached,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/test-telegram")
async def test_telegram():
    """Test koneksi Telegram."""
    sent = await send_telegram(
        "✅ <b>TikTok Monitor aktif!</b>\n\n"
        f"Memantau: <b>@{TIKTOK_USERNAME}</b>\n"
        f"Target: <b>{TARGET_FOLLOWERS:,}</b>\n\n"
        "Update otomatis setiap 15 menit. 🚀"
    )
    if sent:
        return {"success": True, "message": "Pesan test berhasil dikirim!"}
    raise HTTPException(status_code=500, detail="Gagal. Cek token dan chat ID.")
