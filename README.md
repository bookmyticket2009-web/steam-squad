# STEAM SQUAD MOMOS

Flask ordering system for a momo stall: online ordering, UPI/FAM payment, daily pickup tokens, 5-minute cancellation, admin queue.

## Payment flow (no payment gateway)
1. Customer builds a cart and checks out (name + 10-digit mobile).
2. Payment page shows **Pay with UPI app** buttons (GPay / PhonePe / Paytm / any UPI app) and a **QR code**. Both already contain your FAM ID, the exact amount and the order number.
3. After paying, the customer enters the **12-digit UTR** from their app.
4. You check that UTR + amount in your FAM app and press **VERIFY PAYMENT** (admin → Orders).
5. Only then the order becomes PAID, gets a token (SS-001...) and the 2-minute cancellation window starts.

Why manual: without a payment gateway nothing can tell the website automatically that money arrived. Always compare the amount, not just the UTR. One UTR can only be used on one order.

## Setup
```bash
python -m venv .venv
.venv\Scripts\activate          # Windows   (Mac/Linux: source .venv/bin/activate)
pip install -r requirements.txt
copy .env.example .env          # Mac/Linux: cp .env.example .env
```
Fill in `.env` (SECRET_KEY, FAM_ID, ADMIN_USERNAME, ADMIN_PASSWORD_HASH; commands are inside the file), then:
```bash
python app.py
```
Open http://127.0.0.1:5000 . Admin login: /admin/login. Run the tests with `python -m unittest test_app -v`.

**Test before opening:** place an order for the cheapest plate, pay it from a UPI app on your own phone, and check that the app opens with the right amount.

## Tokens
Verification runs inside one SQLite `BEGIN IMMEDIATE` transaction: lock, read the highest token of today, write the next one, commit. Two verifications cannot overlap. `UNIQUE(business_date, token)` is a second safety net. The day follows Indian time (IST). Tokens restart at SS-001 each day, and a cancelled order keeps its token (the number is not reused that day).

## Cancellation / refunds
The 2-minute deadline is stored on the server when you verify the payment and checked again when the customer cancels. The countdown on the page is only for display. Cancelling is only possible while the order is PAID or ACCEPTED (not once PREPARING). It creates a refund request on the admin Refunds page; you send the money back from your FAM app and mark the order REFUNDED.

## Deploy
Use HTTPS (set `COOKIE_SECURE=1`). Run with `gunicorn -w 1 --threads 4 app:app`. Keep `.env` and `instance/` out of Git. Back up `instance/steam_squad.db` regularly. Keep the database on persistent disk. Free tiers that wipe disk on restart will delete your orders.

## Security checklist
- Set `COOKIE_SECURE=1`, `SECRET_KEY` and a strong admin password on the server. Never commit `.env`.
- Built in: CSRF tokens, rate limits (login, checkout, payment, order lookup), CSP with per-request nonces, clickjacking protection, no-store caching on private pages, HSTS over HTTPS, 2-hour admin idle logout, login attempts written to `audit_logs`.
- Turn on 2-factor login for your GitHub and Render accounts, and back up `instance/steam_squad.db`.

## Keep your orders (important on Render)
Render's free plan wipes the project disk on every restart and deploy, so a SQLite database stored there starts empty again (orders and tokens back to zero). To keep data:
1. Use a paid Render instance (Disks are not available on free).
2. Service → Disks → Add Disk: mount path `/var/data`.
3. Add environment variable `DB_PATH=/var/data/steam_squad.db`, then redeploy.
Until then, download a backup from Admin → Payments → "Backup database" at the end of each day.

## Auto-verify payments from your bank SMS
1. Set `SMS_WEBHOOK_SECRET` on the server (and optionally `SMS_SENDER_CONTAINS`, e.g. `HDFCBK`). Redeploy.
2. On the phone that receives your bank's SMS, install an SMS-forwarding app that can call a web address.
3. Configure it: **POST** to `https://YOUR-SITE/api/bank-sms`, header `Authorization: Bearer YOUR_SECRET` (if the app cannot set headers, use `?key=YOUR_SECRET` on the address), body `{"from":"<sender>","text":"<message>"}` (use the app's own placeholders). Forward **only** messages from your bank.
4. Admin → Payments → paste one real SMS into "Test your bank's SMS" to confirm it is understood.
5. Place a ₹99 test order, pay it, and enter the UTR. It should confirm by itself within seconds.
If the secret is empty the feature is off and you keep verifying by hand. Always compare with your bank statement at the end of the day.
