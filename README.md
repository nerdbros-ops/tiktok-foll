# TikTok Follower Monitor

Backend API untuk monitoring follower TikTok, deploy di Railway.

## Environment Variables (wajib diset di Railway)

| Variable | Keterangan |
|---|---|
| `RAPIDAPI_KEY` | API key dari RapidAPI |
| `TELEGRAM_BOT_TOKEN` | Token bot Telegram dari BotFather |
| `TELEGRAM_CHAT_ID` | Chat ID Telegram lo |

## Endpoints

- `GET /` — Status server
- `GET /followers/{username}` — Ambil follower count
- `GET /check/{username}/{target}` — Cek vs target, kirim Telegram jika tercapai
- `POST /test-telegram` — Test koneksi Telegram
