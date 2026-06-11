from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import httpx
import os

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── KONFIGURASI (diset via Environment Variables di Railway) ─────────────────
APIFY_TOKEN      = os.environ.get("APIFY_TOKEN", "")
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Simpan agar notif "TARGET TERCAPAI" tidak terkirim berulang
notified_targets: set = set()


# ─── AMBIL FOLLOWER DARI APIFY ───────────────────────────────────────────────

async def get_tiktok_followers(username: str) -> dict:
    """
    Panggil Apify TikTok Profile Scraper untuk ambil data profil + follower count.
    Actor ID: clockworks~free-tiktok-scraper (gratis, no credit card)
    """
    username = username.lstrip("@")

    # Apify: jalankan actor secara synchronous (tunggu hasil langsung)
    url = "https://api.apify.com/v2/acts/clockworks~free-tiktok-scraper/run-sync-get-dataset-items"
    params = {"token": APIFY_TOKEN}
    payload = {
        "profiles": [f"https://www.tiktok.com/@{username}"],
        "resultsPerPage": 0,       # hanya ambil info profil, tidak perlu video
        "shouldDownloadVideos": False,
        "shouldDownloadCovers": False,
    }

    async with httpx.AsyncClient(timeout=60) as client:
        try:
            resp = await client.post(url, params=params, json=payload)

            if resp.status_code != 200:
                raise HTTPException(
                    status_code=502,
                    detail=f"Apify error {resp.status_code}: {resp.text[:200]}"
                )

            items = resp.json()

            if not items:
                raise HTTPException(
                    status_code=404,
                    detail=f"Data tidak ditemukan untuk @{username}. Cek apakah akun publik."
                )

            # Apify mengembalikan list — ambil item pertama
            profile = items[0]

            followers    = profile.get("followersCount", 0)
            display_name = profile.get("authorMeta", {}).get("name", username)

            # Beberapa versi actor pakai struktur berbeda
            if followers == 0:
                followers = profile.get("authorMeta", {}).get("fans", 0)
            if not display_name or display_name == username:
                display_name = profile.get("authorMeta", {}).get("nickName", username)

            return {
                "success": True,
                "username": username,
                "display_name": display_name,
                "followers": followers,
                "followers_formatted": format_number(followers),
                "profile_url": f"https://www.tiktok.com/@{username}",
            }

        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error: {str(e)}")


def format_number(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    elif n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def progress_bar(pct: float) -> str:
    filled = int(pct / 10)
    empty  = 10 - filled
    return "🟩" * filled + "⬜" * empty


# ─── KIRIM TELEGRAM ──────────────────────────────────────────────────────────

async def send_telegram(message: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
    }
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.post(url, json=payload)
            return resp.status_code == 200
        except Exception:
            return False


# ─── ENDPOINTS ───────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {
        "status": "aktif ✓",
        "endpoints": [
            "GET  /followers/{username}",
            "GET  /check/{username}/{target}",
            "POST /test-telegram",
        ]
    }


@app.get("/followers/{username}")
async def get_followers(username: str):
    """Ambil follower count terkini. Contoh: /followers/charlidamelio"""
    return await get_tiktok_followers(username)


@app.get("/check/{username}/{target}")
async def check_target(username: str, target: int):
    """
    Dipanggil dashboard setiap 15 menit.
    Kirim update Telegram setiap check + notif spesial saat target tercapai.
    Contoh: /check/charlidamelio/2000000
    """
    data = await get_tiktok_followers(username)

    followers    = data["followers"]
    remaining    = max(0, target - followers)
    progress_pct = round(min(100, (followers / target) * 100), 2)
    reached      = followers >= target

    data["target"]       = target
    data["reached"]      = reached
    data["remaining"]    = remaining
    data["progress_pct"] = progress_pct

    notif_key = f"{username}_{target}"

    if reached and notif_key not in notified_targets:
        # Notif SPESIAL — dikirim sekali saat target tercapai
        notified_targets.add(notif_key)
        msg = (
            f"🎉 <b>TARGET TERCAPAI!</b>\n\n"
            f"👤 <b>@{data['username']}</b> ({data['display_name']})\n"
            f"👥 Followers: <b>{followers:,}</b> ({data['followers_formatted']})\n"
            f"🎯 Target: <b>{target:,}</b>\n\n"
            f"{progress_bar(100)} 100%\n\n"
            f"🔗 {data['profile_url']}"
        )
        data["telegram_sent"] = await send_telegram(msg)

    else:
        # Update RUTIN — dikirim setiap 15 menit
        bar = progress_bar(progress_pct)
        status = "✅ Sudah tercapai!" if reached else "⏳ Masih monitoring..."
        msg = (
            f"📊 <b>Update Follower</b>\n\n"
            f"👤 <b>@{data['username']}</b> ({data['display_name']})\n"
            f"👥 Followers: <b>{followers:,}</b> ({data['followers_formatted']})\n"
            f"🎯 Target: {target:,}\n"
            f"📉 Sisa: <b>{remaining:,}</b> lagi\n\n"
            f"{bar} {progress_pct}%\n\n"
            f"{status}"
        )
        data["telegram_sent"] = await send_telegram(msg)

    return data


@app.post("/test-telegram")
async def test_telegram():
    """Test apakah Telegram sudah terhubung dengan benar."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise HTTPException(
            status_code=400,
            detail="TELEGRAM_BOT_TOKEN atau TELEGRAM_CHAT_ID belum diset di Railway."
        )
    sent = await send_telegram(
        "✅ <b>TikTok Monitor aktif!</b>\n\n"
        "Bot Telegram berhasil terhubung ke server Railway.\n"
        "Lo akan dapat update follower setiap 15 menit. 🚀"
    )
    if sent:
        return {"success": True, "message": "Pesan test berhasil dikirim ke Telegram!"}
    raise HTTPException(status_code=500, detail="Gagal kirim pesan. Cek token dan chat ID.")
