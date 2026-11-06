# Melbete Bingo - Android app

A small native Android app (no dependencies, no permissions, no network access of its own). It gives players
a normal app icon that opens the Melbete bot in Telegram, where the game's **Play** button is, and helps
them install Telegram if it is missing.

**Why it opens Telegram instead of showing the game itself:** the game is a Telegram Mini App. Every wallet and
game action is verified with Telegram's signed login data, and browser login was deliberately switched off
after the security incident (see `docs/security.md`). A web view inside this app has no Telegram login, so
deposits, withdrawals and play would all be refused. A fully standalone app needs a new, secure sign-in
(see "Standalone version" below) and is a separate piece of work.

## Build it (pick one)

### A. GitHub Actions (no Android tools needed)
1. In the repository: **Settings > Secrets and variables > Actions > Variables**: add `TELEGRAM_BOT_USERNAME`
   = your bot's username without the `@`.
2. Create a release key once (keep the file and passwords somewhere safe; **if you lose it you can never publish
   an update that installs over the old app**):

       keytool -genkeypair -v -keystore melbete-release.keystore -alias melbete -keyalg RSA -keysize 2048 -validity 10000
       base64 -w0 melbete-release.keystore      # copy the output

   Add these **Secrets**: `ANDROID_KEYSTORE_BASE64` (that output), `ANDROID_KEYSTORE_PASSWORD`,
   `ANDROID_KEY_ALIAS` (`melbete`), `ANDROID_KEY_PASSWORD`. (Skipping this still builds an installable APK, signed
   with a debug key.)
3. **Actions > Build Android APK > Run workflow**. When it finishes, download the `melbete-bingo-apk` artifact:
   it contains `melbete-bingo.apk` and `melbete-bingo.apk.sha256`.

### B. Android Studio
Open the `android/` folder, set `MELBETE_BOT_USERNAME` in `android/gradle.properties`, then
**Build > Build Bundle(s) / APK(s) > Build APK(s)**. For a signed release use **Build > Generate Signed App Bundle / APK**.

## Make it downloadable from your site
1. Put `melbete-bingo.apk` and `melbete-bingo.apk.sha256` in `src/static/downloads/`.
2. Redeploy. Players open `https://<your-domain>/download`, tap **Download**, and follow the on-screen steps.
   Share that link in your Telegram channel and bot messages.

Updates: raise the version code (workflow input / `MELBETE_VERSION_CODE`), build with the **same key**, replace the
two files, redeploy. Players install the new file over the old one. There is no automatic update.

## Notes
* Android shows a "install unknown apps" prompt for files not from Google Play. That is normal for direct downloads.
* Publishing on Google Play is possible but they may reject an app that only opens another app; ask before investing.
* Not built or tested yet: this project was prepared without an Android SDK. Build it once and install it on a real
  phone before sharing the link.

## Standalone version (not built)
To let players use the game without Telegram open, the server would need its own sign-in for the app, for example a
one-time code confirmed through the bot that issues a short-lived, device-bound token. That reintroduces
non-Telegram sessions for money actions, so it needs a deliberate security review first.
