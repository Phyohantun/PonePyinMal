# Admin Setup Guide

## How to Set Your Admin ID

The `/myuserlist` command is restricted to admins only. To set yourself as an admin:

### Step 1: Get Your Telegram User ID

1. Start your bot
2. Send the command `/myid` to your bot
3. The bot will reply with your Telegram user ID (a number)

### Step 2: Update the ADMIN_ID in bot.py

1. Open `bot.py`
2. Find line 236 where it says:
   ```python
   ADMIN_ID = 7931838878  # Replace with your actual Telegram user ID
   ```
3. Replace `7931838878` with your actual Telegram user ID from Step 1
4. Save the file

### Step 3: Restart Your Bot

After updating the `ADMIN_ID`, restart your bot for the changes to take effect.

## Using the /myuserlist Command

Once you've set your admin ID, you can use the `/myuserlist` command to:

- See total number of users
- View breakdown by status (Trial, Paid, Expired)
- See detailed information about each user:
  - User ID
  - Account status
  - Trial credits used
  - Paid credits remaining
  - Renewal date

### Example Output:

```
📊 **Bot User Statistics**

👥 **Total Users:** 45
🆓 **Trial Users:** 30
💎 **Paid Users:** 12
⛔ **Expired Users:** 3

**User Details (First 20):**

1. 🆓 ID: `123456789`
   Status: TRIAL
   Trial Used: 2/3

2. 💎 ID: `987654321`
   Status: PAID
   Credits: 145
   Renewal: 2025-11-15
...
```

## Security Note

⚠️ **Important:** Only the user ID matching `ADMIN_ID` can use the `/myuserlist` command. Other users will receive a "no permission" message in Burmese.

## Troubleshooting

If `/myuserlist` doesn't work:
1. Double-check that you updated `ADMIN_ID` with YOUR user ID
2. Make sure you restarted the bot after making changes
3. Verify there are no syntax errors by checking the bot logs
