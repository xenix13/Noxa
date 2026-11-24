package io.github.xenix13.noxa.tv;

import android.os.SystemClock;

import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;

/**
 * Bandwidth between the TV and the server, for automatic quality: a few megabytes downloaded at the first
 * start of a video, and the measurement kept 10 minutes. The server deduces the original or a converted quality.
 */
final class Bandwidth {
    private static final long KEEP_MS = 10 * 60 * 1000;
    private static final long MAX_MS = 3000;
    private static final long WARMUP = 1 << 20;
    private static long kbps, at;
    // throughput seen by the player itself while it downloads the video: on a slow TV the short probe can read far
    // below the real link (it measured ×0.25 in one case), and the server then converts videos for nothing
    private static long seen, seenAt;
    private static final long SEEN_KEEP_MS = 60 * 60 * 1000;

    private Bandwidth() {}

    /** Known bandwidth (kbit/s), otherwise measured now; 0 when the measurement fails (the server keeps the last one). */
    static void get(Api.Ok<Long> done) {
        if (!"auto".equals(Session.pref("quality", "auto"))) {
            done.run(0L);  // quality set by the account: nothing to measure
            return;
        }
        if (kbps > 0 && SystemClock.elapsedRealtime() - at < KEEP_MS) {
            done.run(kbps);
            return;
        }
        Api.run(Bandwidth::measure, v -> {
            if (seen > v && SystemClock.elapsedRealtime() - seenAt < SEEN_KEEP_MS) v = seen;
            if (v > 0) {
                kbps = v;
                at = SystemClock.elapsedRealtime();
            }
            done.run(v);
        }, e -> done.run(0L));
    }

    static void lower(long value) {
        kbps = value;
        at = SystemClock.elapsedRealtime();
        seen = 0;  // playback stalled: what the player saw before no longer holds
    }

    /** Estimate from the player (kbit/s), once it has downloaded enough to be meaningful. Only raises the value:
     *  lowering is left to stalls (see PlayerActivity.onStall). */
    static void observe(long value, long bytesLoaded) {
        if (value <= 0 || bytesLoaded < (8L << 20)) return;
        long now = SystemClock.elapsedRealtime();
        seen = value;
        seenAt = now;
        if (value > kbps) {
            kbps = value;
            at = now;
        }
    }

    private static long measure() throws Exception {
        HttpURLConnection c = (HttpURLConnection) new URL(Api.url("/api/bandwidth?size=" + (32 << 20))).openConnection();
        try {
            c.setConnectTimeout(5000);
            c.setReadTimeout(5000);
            c.setRequestProperty("User-Agent", Api.VERSION_HEADER);
            String ck = Api.cookieHeader();
            if (!ck.isEmpty()) c.setRequestProperty("Cookie", ck);
            if (c.getResponseCode() != 200) return 0;
            // the first megabyte is not counted: the TCP connection is still ramping up (otherwise a
            // fast connection looks slow, and a high-bitrate file is converted for no reason)
            long got = 0, timed = 0, t1 = SystemClock.elapsedRealtime(), t0 = 0;
            byte[] buf = new byte[65536];
            try (InputStream in = c.getInputStream()) {
                int n;
                while ((n = in.read(buf)) > 0) {
                    got += n;
                    long now = SystemClock.elapsedRealtime();
                    if (t0 == 0 && got >= WARMUP) t0 = now;
                    else if (t0 > 0) timed += n;
                    if (now - t1 > MAX_MS) break;
                }
            }
            long end = SystemClock.elapsedRealtime();
            if (t0 > 0 && timed >= 512 * 1024) return timed * 8 / Math.max(1, end - t0);
            return got < 256 * 1024 ? 0 : got * 8 / Math.max(1, end - t1);
        } finally {
            c.disconnect();
        }
    }
}
