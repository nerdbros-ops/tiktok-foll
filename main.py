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


async def send_telegram_photo(photo_url: str, caption: str = "") -> bool:
    """
    Kirim foto (dari URL) ke Telegram.
    Kalau gagal sebagai foto (misal rasio gambar terlalu panjang),
    otomatis fallback kirim sebagai dokumen/file.
    """
    async with httpx.AsyncClient(timeout=30) as client:
        # ── Coba kirim sebagai foto dulu ──
        try:
            resp = await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
                json={"chat_id": TELEGRAM_CHAT_ID, "photo": photo_url, "caption": caption, "parse_mode": "HTML"}
            )
            if resp.status_code == 200:
                return True
            print(f"[telegram] sendPhoto gagal ({resp.status_code}), coba sebagai dokumen...")
        except Exception as e:
            print(f"[telegram] sendPhoto exception: {e}")

        # ── Fallback: kirim sebagai dokumen ──
        try:
            resp = await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument",
                json={"chat_id": TELEGRAM_CHAT_ID, "document": photo_url, "caption": caption, "parse_mode": "HTML"}
            )
            if resp.status_code != 200:
                print(f"[telegram] sendDocument juga gagal: {resp.text[:300]}")
            return resp.status_code == 200
        except Exception as e:
            print(f"[telegram] sendDocument exception: {e}")
            return False


# ─── SCREENSHOT PROFIL TIKTOK ────────────────────────────────────────────────

async def get_profile_screenshot(profile_url: str) -> str | None:
    """
    Ambil screenshot halaman profil TikTok via Microlink API (gratis).
    - Mode mobile (user-agent iPhone + viewport mobile)
    - Full page (termasuk grid video terbaru/pinned)
    Return URL gambar screenshot, atau None kalau gagal.
    """
    api_url = "https://api.microlink.io/"
    mobile_ua = (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/17.4 Mobile/15E148 Safari/604.1"
    )
    params = {
        "url": profile_url,
        "screenshot": "true",
        "meta": "false",
        "embed": "screenshot.url",
        # ── Mode mobile (viewport + user agent iPhone) ──
        "viewport.width": "390",
        "viewport.height": "844",
        "viewport.isMobile": "true",
        "viewport.deviceScaleFactor": "2",
        "viewport.userAgent": mobile_ua,
        # ── Tangkap seluruh halaman (termasuk grid video) ──
        "screenshot.fullPage": "true",
        "screenshot.type": "jpeg",
        "waitFor": "6000",  # tunggu 6 detik agar video grid sempat load
    }
    async with httpx.AsyncClient(timeout=90, follow_redirects=True) as client:
        try:
            resp = await client.get(api_url, params=params)
            if resp.status_code == 200:
                return str(resp.url)  # embed=screenshot.url -> redirect ke gambar
            print(f"[screenshot] error {resp.status_code}: {resp.text[:200]}")
            return None
        except Exception as e:
            print(f"[screenshot] exception: {e}")
            return None


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

            # Ambil & kirim screenshot profil sebagai bukti visual
            print("[monitor] Mengambil screenshot profil...")
            screenshot_url = await get_profile_screenshot(data["profile_url"])
            if screenshot_url:
                caption = (
                    f"📸 <b>Screenshot profil @{data['username']}</b>\n"
                    f"Saat mencapai {followers:,} followers"
                )
                sent = await send_telegram_photo(screenshot_url, caption)
                if sent:
                    print("[monitor] Screenshot berhasil dikirim!")
                else:
                    await send_telegram("⚠️ Gagal mengirim screenshot, tapi target tetap tercapai.")
            else:
                await send_telegram("⚠️ Gagal mengambil screenshot profil.")

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


@app.get("/debug/{username}")
async def debug_raw(username: str):
    """
    Tampilkan SEMUA data mentah dari Apify untuk username tertentu.
    Gunakan ini untuk cari field follower count yang paling presisi.
    Contoh: GET /debug/arthaadaa
    """
    username = username.lstrip("@")
    url = "https://api.apify.com/v2/acts/automation-lab~tiktok-profile-scraper/run-sync-get-dataset-items"
    params  = {"token": APIFY_TOKEN}
    payload = {"profiles": [f"@{username}"]}

    async with httpx.AsyncClient(timeout=90) as client:
        resp = await client.post(url, params=params, json=payload)

        if resp.status_code not in (200, 201):
            raise HTTPException(status_code=502, detail=f"Apify error {resp.status_code}: {resp.text[:500]}")

        items = resp.json()
        if not items:
            raise HTTPException(status_code=404, detail="Data kosong.")

        return {
            "raw_data": items[0],
            "available_keys": list(items[0].keys()),
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


@app.post("/test-screenshot")
async def test_screenshot():
    """
    Test ambil screenshot profil dan kirim ke Telegram.
    Gunakan ini untuk mencoba fitur screenshot tanpa menunggu target tercapai.
    """
    if not TIKTOK_USERNAME:
        raise HTTPException(status_code=400, detail="TIKTOK_USERNAME belum diset.")

    profile_url = f"https://www.tiktok.com/@{TIKTOK_USERNAME}"
    screenshot_url = await get_profile_screenshot(profile_url)

    if not screenshot_url:
        raise HTTPException(status_code=500, detail="Gagal mengambil screenshot.")

    sent = await send_telegram_photo(
        screenshot_url,
        caption=f"📸 Test screenshot profil @{TIKTOK_USERNAME}"
    )

    if sent:
        return {"success": True, "message": "Screenshot berhasil dikirim ke Telegram!", "screenshot_url": screenshot_url}
    raise HTTPException(status_code=500, detail="Screenshot berhasil diambil tapi gagal kirim ke Telegram.")
