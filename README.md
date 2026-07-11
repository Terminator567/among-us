# Among Us: Campus Edition

A lightweight Flask app for running a live, real-world Among Us game.

## Player registration and login flow

Players do **not** create accounts on the website.

1. The organiser creates a Google Form asking for at least:
   - Name (or Full Name)
   - Email Address (or Email)
   - Login Code
2. Players submit the Google Form.
3. The organiser opens the linked Google Sheet and selects **File → Download → Comma-separated values (.csv)**.
4. In the existing admin dashboard, upload that CSV under **Import Google Form responses**.
5. Before downloading the sheet, the organiser enters a chosen login code for each player in the Login Code column.
6. The app creates each new player, uses the exact login code from the CSV, generates a private kill code, and emails both details to them.
7. Players visit the main website and enter the emailed login code.

The importer accepts `login_code`, `login code`, or `logincode` as the login-code header and ignores extra Google Forms columns such as Timestamp. Rows without a login code are skipped. Re-importing an updated response sheet skips email addresses and login codes already in the game, so only new players are added and emailed.

## Shared player layout

Impostors and civilians receive the same dashboard structure:

- remaining-player count
- game-action code form
- task/photo upload area
- death log

The server still enforces permissions. A civilian submitting the game-action form cannot eliminate anyone. This keeps the interface consistent without weakening role security.

## Private photo submissions

Every living player can upload task photos. Photos are stored in `private_uploads/`, outside Flask's public static directory. The photo-viewing route requires an active admin session, so players cannot open another player's submission through the app.

## Admin

The existing password-protected admin remains in place. It can:

- import Google Form response CSV files
- add or remove players manually
- assign impostors randomly
- add tasks
- view private task photos
- eject players
- view the elimination log
- change game settings
- reset the game

## Local setup

```bash
pip install -r requirements.txt
python3 app.py
```

Open `http://127.0.0.1:5000` for player login and `http://127.0.0.1:5000/admin` for the admin.

## Email configuration

Set these environment variables before starting the app:

```bash
export SMTP_HOST="smtp.gmail.com"
export SMTP_PORT="587"
export SMTP_USER="youraddress@gmail.com"
export SMTP_PASSWORD="your-google-app-password"
export FROM_EMAIL="youraddress@gmail.com"
export ADMIN_PASSWORD="a-private-admin-password"
export SECRET_KEY="a-long-random-secret"
```

For Gmail, use a Google App Password rather than the normal account password. Without SMTP configuration, the app prints the emails and codes in the terminal for local testing.
