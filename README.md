# AI Image Upscale Telegram Bot

A Telegram bot that enhances image resolution using AI via the Replicate API. Built with Python and Firebase Firestore for user management.

## Features

- 🎯 **Image Upscaling**: Uses Real-ESRGAN AI model to enhance image resolution
- 🆓 **Freemium Model**: 3 free trial upscales per user
- 💎 **Subscription System**: 150 credits per month for 10,000 MMK
- 📊 **User Management**: Firebase Firestore for tracking credits and user status
- 🖼️ **Smart Size Limits**: Prevents oversized images (max 2560x1440)

## Setup Instructions

### 1. Prerequisites

- Python 3.8 or higher
- Telegram Bot Token (from [@BotFather](https://t.me/BotFather))
- Replicate API Token (from [replicate.com](https://replicate.com/account))
- Firebase project with Firestore enabled

### 2. Installation

1. Clone or download this repository
2. Create a virtual environment:
   ```bash
   python -m venv .venv
   .venv\Scripts\activate  # On Windows
   ```

3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

### 3. Configuration

1. Copy `.env.example` to `.env`:
   ```bash
   copy .env.example .env
   ```

2. Fill in your credentials in `.env`:
   - `TELEGRAM_TOKEN`: Your bot token from BotFather
   - `REPLICATE_TOKEN`: Your Replicate API token
   - `FIREBASE_CREDENTIALS_PATH`: Path to your Firebase service account JSON file

3. Download your Firebase service account JSON file and place it in the project directory as `firebase-credentials.json`

### 4. Firebase Setup

1. Create a new Firebase project
2. Enable Firestore Database
3. Create a service account and download the JSON credentials file
4. The bot will automatically create the required `users` collection

### 5. Running the Bot

```bash
python bot.py
```

## Bot Commands

- `/start` - Welcome message and instructions
- `/subscribe` - Payment instructions for subscription
- `/credits` - Show remaining credits and renewal date (paid/expired users)

## Database Structure

The bot uses Firebase Firestore with a `users` collection:

```json
{
  "user_id": {
    "status": "trial|paid|expired",
    "trial_credits_used": 0-3,
    "paid_credits_remaining": 0-150,
    "renewal_date": "YYYY-MM-DD"
  }
}
```

## Usage Flow

1. User sends `/start` to get welcome message
2. User sends a photo (≤ 2560x1440 pixels)
3. Bot checks user's credit status:
   - **New users**: Get 3 free trial credits
   - **Trial users**: Use remaining trial credits
   - **Paid users**: Use monthly credits
   - **Out of credits**: Prompted to subscribe
4. Bot processes image via Replicate Real-ESRGAN API
5. Enhanced image is sent back to user

## Subscription Management

The subscription system is manual:
1. Users pay via WavePay/KBZPay (instructions in `/subscribe`)
2. Admin manually updates user status in Firestore
3. Set `status: "paid"` and `paid_credits_remaining: 150`

## Dependencies

- `python-telegram-bot==21.7` - Telegram Bot API wrapper
- `replicate==0.24.0` - Replicate AI API client
- `firebase-admin==6.4.0` - Firebase Admin SDK
- `python-dotenv==1.0.1` - Environment variable management
- `Pillow==10.3.0` - Image processing

## Error Handling

- Oversized images are rejected with instructions
- API failures are caught and reported to user
- All errors are logged for debugging

## License

This project is for educational/commercial use. Make sure to comply with all API terms of service.

---

## Deploying to Railway (Step-by-step)

This bot uses long polling (no HTTP server). On Railway, run it as a worker process.

### 1) Push your code to GitHub (Windows PowerShell)

If this folder isn’t a git repo yet:

```powershell
git init
git add .
git commit -m "Initial commit"
git branch -M main
```

Create an empty repository on GitHub (via the website), then set it as the remote and push:

```powershell
# Replace <your-username> and <your-repo>
git remote add origin https://github.com/<your-username>/<your-repo>.git
git push -u origin main
```

After the first push, subsequent updates:

```powershell
git add .
git commit -m "Update"
git push
```

### 2) Ensure Procfile exists

This repo includes a `Procfile` with:

```
worker: python bot.py
```

Railway will treat the app as a worker (no port binding required).

### 3) Create a Railway project and connect GitHub

1. In Railway dashboard, click “New” → “Deploy from GitHub” and select your repo.
2. Enable Auto Deploys if you want each push to deploy automatically.

### 4) Configure Environment Variables (Settings → Variables)

- `TELEGRAM_TOKEN` — Your BotFather token
- `REPLICATE_TOKEN` — Your Replicate API token
- One of the Firebase credential methods:
   - Recommended (cloud): `FIREBASE_CREDENTIALS_JSON_CONTENT` — Paste the full JSON from your Firebase service account
   - Alternative (local): `FIREBASE_CREDENTIALS_PATH` — File path (used for local dev; not needed on Railway if using JSON content)
- Optional: `ADMIN_USERNAME` — e.g. `@yourname` (defaults to value in code if not set)

The code prefers `FIREBASE_CREDENTIALS_JSON_CONTENT` when set and falls back to `FIREBASE_CREDENTIALS_PATH` for local development.

### 5) Deploy

Railway will install `requirements.txt`, detect `Procfile`, and start the worker.

### 6) Verify logs

Open the service → Logs. You should see messages like:

- `Firebase successfully initialized.`
- `Bot starting…`

Send a message to your bot on Telegram to confirm it responds.

### 7) Troubleshooting

- No updates received: confirm `TELEGRAM_TOKEN` is correct. Long polling means you don’t need a public URL.
- Firebase errors: check `FIREBASE_CREDENTIALS_JSON_CONTENT` formatting (must be the full JSON). Ensure the service account has Firestore access.
- Real-time Firestore sync: the bot uses Firestore `on_snapshot` to reflect changes (like `renewal_date`) without restarts.
