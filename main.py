from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import httpx
import asyncio
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

target_reached = False


# ─── AMBIL FOLLOWER VIA APIFY ────────────────────────────────────────────────

async def get_tiktok_followers(username: str) -> dict:
    """
    Pakai Apify actor 'automation-lab/tiktok-profile-scraper' yang
    mengembalikan data profil termasuk followerCount.
    """
    username = username.lstrip("@")

    url = "https://api.apify.com/v2/acts/automation-lab~tiktok-profile-scraper/run-sync-get-dataset-items"
    params  = {"token": APIFY_TOKEN}
    payload = {
        "profiles": [f"@{username}"],
    }

    async with httpx.AsyncClient(timeout=90) as client:
        resp = await client.post(url, params=params, json=payload)

        # Apify bisa return 200 atau 201 — keduanya valid
        if resp.status_code not in (200, 201):
            raise Exception(f"Apify error {resp.status_code}: {resp.text[:300]}")

        items = resp.json()
        if not items:
            raise Exception(f"Data kosong untuk @{username}. Pastikan akun publik dan masih aktif.")

        profile   = items[0]
        followers = (
            profile.get("followerCount") or
            profile.get("followers") or
            profile.get("stats", {}).get("followerCount") or 0
        )
        name = (
            profile.get("nickname") or
            profile.get("name") or
            profile.get("displayName") or
            username
        )

        if followers == 0:
            print(f"[debug] Keys dari Apify: {list(profile.keys())}")
            raise Exception(f"followerCount = 0. Keys tersedia: {list(profile.keys())}")

        return {
            "username": username,
            "display_name": name,
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
    url     = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.post(url, json=payload)
            return resp.status_code == 200
        except Exception:
            return False


# ─── JOB UTAMA ───────────────────────────────────────────────────────────────

async def monitor_job():
    global target_reached

    if not TIKTOK_USERNAME:
        print("[monitor] TIKTOK_USERNAME belum diset, skip.")
        return

    if target_reached:
        print("[monitor] Target sudah tercapai, scheduler berhenti.")
        return

    print(f"[monitor] Mengecek @{TIKTOK_USERNAME}...")

    try:
        data      = await get_tiktok_followers(TIKTOK_USERNAME)
        followers = data["followers"]
        target    = TARGET_FOLLOWERS
        pct       = round(min(100, (followers / target) * 100), 2)
        remaining = max(0, target - followers)
        reached   = followers >= target

        print(f"[monitor] @{TIKTOK_USERNAME}: {followers:,} followers ({pct}%)")

        if reached and not target_reached:
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
            print("[monitor] Notif target tercapai dikirim!")

        else:
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
            print(f"[monitor] Update rutin dikirim.")

    except Exception as e:
        err = str(e)
        print(f"[monitor] Error: {err}")
        await send_telegram(f"⚠️ <b>Monitor Error</b>\n\nGagal mengecek @{TIKTOK_USERNAME}:\n{err[:500]}")


# ─── STARTUP & SHUTDOWN ──────────────────────────────────────────────────────

scheduler = AsyncIOScheduler()

@app.on_event("startup")
async def startup():
    scheduler.add_job(monitor_job, "interval", minutes=15, id="tiktok_monitor")
    scheduler.start()
    print(f"[scheduler] Aktif — cek @{TIKTOK_USERNAME} setiap 15 menit")
    asyncio.create_task(run_first_check())

async def run_first_check():
    await asyncio.sleep(5)
    print("[startup] Pengecekan pertama...")
    await monitor_job()

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
    if not TIKTOK_USERNAME:
        raise HTTPException(status_code=400, detail="TIKTOK_USERNAME belum diset.")
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
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/test-telegram")
async def test_telegram():
    sent = await send_telegram(
        f"✅ <b>TikTok Monitor aktif!</b>\n\n"
        f"Memantau: <b>@{TIKTOK_USERNAME}</b>\n"
        f"Target: <b>{TARGET_FOLLOWERS:,}</b>\n\n"
        "Update otomatis setiap 15 menit. 🚀"
    )
    if sent:
        return {"success": True, "message": "Pesan test berhasil dikirim!"}
    raise HTTPException(status_code=500, detail="Gagal. Cek token dan chat ID.")
