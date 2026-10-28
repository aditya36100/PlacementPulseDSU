# PlacementPulse (cloud + phone)

Telegram groups → **worker** (Telethon + Gemini, runs 24/7 in the cloud) → **Supabase** (Postgres + Realtime) → **phone app** (installable PWA, one tab per group).

## Setup
1. **Supabase**: create a project → SQL Editor → run `supabase/schema.sql`.
   Authentication → Users → *Add user* (your email + password); Settings → disable "Allow new users to sign up".
   Copy the Project URL, `anon` key and `service_role` key.
2. **Telegram session** (local, once): copy `.env.example` → `.env`, fill API id/hash, run
   `pip install -r requirements.txt && python tools/make_session.py` → copy `TELEGRAM_SESSION_STRING`.
   Stop the old local app first.
3. **Worker**: deploy this folder (uses `Dockerfile`) to Railway / Fly.io / any VPS as an always-on service.
   Set every variable from `.env.example` there (`TELEGRAM_GROUP` = your two groups, comma-separated; `SUPABASE_SERVICE_KEY` = service_role key).
   Find group ids with `python -m app.list_groups`.
4. **App**: put the Project URL and `anon` key in `web/config.js`, host the `web/` folder (Netlify / Vercel / Cloudflare Pages, HTTPS required).
5. **Phone**: open the URL → sign in → browser menu → *Add to Home Screen*. Tap **Enable alerts**.

## Notes
- Message → app delay = Gemini extraction (~5–10 s, free tier is rate-limited to 1 request / 4.5 s).
- Alerts fire while the app is open or backgrounded. Alerts with the app fully closed need Web Push (not included).
- `service_role` key is server-only. Never put it in `web/`.
- Old SQLite files, `app/main.py`, and the SQLite tests are gone; Supabase replaces them.

## Android app (Expo) + closed-app alerts
`mobile/` is a native Android shell (WebView of the hosted `web/` app) with native push notifications:
new live placement → worker → Expo Push API → FCM → your phone, even when the app is closed.
Backfilled (old) messages never trigger alerts.

1. Run `supabase/push.sql` in Supabase. Redeploy the worker and the `web/` folder.
2. `npx create-expo-app@latest pp-mobile --template blank`, then copy `mobile/App.js`, `app.json`, `eas.json`, `assets/` into it.
   In `App.js` set `APP_URL` to your hosted web URL. Then:
   `npx expo install react-native-webview expo-notifications expo-device expo-constants`
3. Firebase (free): create a project → add Android app, package `com.placementpulse.app` → download `google-services.json` into `pp-mobile/`.
   Project settings → Service accounts → *Generate new private key*.
4. `npm i -g eas-cli && eas login && eas init`, then `eas credentials` → Android → upload the service-account key (FCM V1).
5. `eas build -p android --profile preview` → install the APK from the link. Open it, sign in, allow notifications.
