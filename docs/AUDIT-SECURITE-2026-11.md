# Security audit of Noxa — version 4.5

*November 26, 2026. Third audit, after [3.1.0](AUDIT-SECURITE.md) and [3.19.0 to 3.39](AUDIT-SECURITE-2026-10.md).
It covers what was added since 3.39 (cold storage, freeing space, external backup storages, Letterboxd import,
two-factor authentication, the desktop and Android apps, the apps catalogue, Whisper, OpenSubtitles) and the
changes of 4.5 (package rename, library sorts, player rework, online demo). Code review and tests on a test
server; each fix has a regression test in `tests/test_security.py`.*

The threat model is unchanged: a home network, possibly reachable from the Internet behind an HTTPS reverse
proxy; an unauthenticated visitor, a household member trying to go beyond their rights, a malicious website
visited by a signed-in member, and hostile content from outside (lists, metadata, files, linked servers). The
administrator is trusted.

## Summary

| # | Severity | Area | Problem | Status |
|---|---|---|---|---|
| 1 | Medium | Android app | The app's bridge (`window.NoxaAndroid`) is visible to every frame of the page, including a trailer's YouTube iframe: a frame could call `setServer()` and point the app at another server (fake sign-in page) | Fixed |
| 2 | Low | Desktop app | The page could change the app's server through the bridge (`setServer`), not only the app's own server choice screen | Fixed |
| 3 | Info | Whisper | The models are downloaded over HTTPS from Hugging Face, without a checksum | Recommendation |

No critical flaw (remote code execution, authentication bypass, SQL or command injection) was found.

## 1. Android app: the bridge and the frames

`addJavascriptInterface` exposes the bridge to **all** the frames of the WebView, whatever their origin. The
interface embeds YouTube trailers in iframes (`youtube-nocookie.com`). Such a frame could call
`NoxaAndroid.setServer("https://…")`: the app checks that the address answers like a Noxa server, then loads it
and keeps it. A server made to look like Noxa could then show a fake sign-in page.

**Fix:** `setServer()` now only acts when the page shown is the app's own server choice screen
(`file:///android_asset/setup.html`). The other methods were checked: `changeServer()` only opens that screen,
`checkUpdate()` downloads the APK from the configured server, and Android refuses an update signed with another
key; the notification methods only concern this device's own subscription.

## 2. Desktop app: changing the server

The Electron app is well isolated (`contextIsolation`, `sandbox`, the preload script only in the main frame,
links to other sites opened in the browser). Its bridge still let the server's page change the app's server.
**Fix:** the `nexis:set-server` request is refused unless it comes from the app's `setup.html`.

## 3. Checked, nothing to fix

- **Library sorts and filters (4.5):** the sort and the order come from a fixed list; the filters are checked
  (language codes, resolutions) before going into the SQL query. The `lib_sorts` preference is validated.
- **Cold storage:**
  - the names of the archives sent by a linked server are confined to that server's folder (real path checked),
    with the quota reserved by the administrator;
  - the local address read by ffmpeg (`/cold/<token>`) only answers on `127.0.0.1`, without a proxy header,
    with a random token that expires.
- **Freeing space:** when Noxa deletes the original itself, the file's real path (symbolic links resolved) must
  be inside one of the library's folders.
- **Backups:** an archive to restore is refused if it holds anything but regular files with the expected names
  (no link, no absolute path, no `..`); the database is checked before it is put in place. Encryption is
  authenticated (HMAC) before decryption.
- **Letterboxd import:** 20 MB at most per upload; each file of the ZIP is read up to its declared size, which is
  bounded too.
- **Two-factor authentication:** the code step allows 5 tries per sign-in ticket (valid 5 minutes), behind the
  password lockout; codes are compared in constant time and a code cannot be used twice.
- **Addresses reachable without signing in:** the setup, sign-in, shared album (random token), TV streams
  (random 96-bit id), device notifications (device token), apps catalogue and `/tv` routes. The apps catalogue
  asks GitHub at most every 10 minutes, whatever the number of visitors.
- **External programs:** no shell is used; ffmpeg only receives absolute paths with the local-files protocol
  list; curl and rclone get the file names inside a URL or a `remote:path` argument.
- **Online demo (4.5):** a static site on another origin (GitHub Pages), with no access to any server.

## Recommendations

1. **Whisper models:** pin the SHA-256 of each model offered and check it after the download.
2. **Android app:** in a later version, replace `addJavascriptInterface` with `WebViewCompat.addWebMessageListener`,
   which can restrict the bridge to the server's origin.
