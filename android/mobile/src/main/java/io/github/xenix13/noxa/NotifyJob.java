package io.github.xenix13.noxa;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.job.JobInfo;
import android.app.job.JobParameters;
import android.app.job.JobScheduler;
import android.app.job.JobService;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;

/**
 * Noxa notifications (new episode, requested title arrived, server alert).
 * Android's web view does not handle websites' push notifications: so the app fetches its messages
 * from the server (/api/push/device, token specific to this device) about every 15 minutes, when the network is there,
 * even with the app closed. Neither Google nor a developer account: nothing goes through a third-party service.
 */
public class NotifyJob extends JobService {
    static final String CHANNEL = "noxa";
    private static final int JOB = 7001;
    private static final long EVERY = 15 * 60 * 1000L; // the shortest Android accepts for a periodic job

    static SharedPreferences prefs(Context ctx) { return ctx.getSharedPreferences("noxa", MODE_PRIVATE); }

    static void channel(Context ctx) {
        NotificationChannel c = new NotificationChannel(CHANNEL, ctx.getString(R.string.channel_name), NotificationManager.IMPORTANCE_DEFAULT);
        c.setDescription(ctx.getString(R.string.channel_text));
        ctx.getSystemService(NotificationManager.class).createNotificationChannel(c);
    }

    static void schedule(Context ctx) {
        JobInfo job = new JobInfo.Builder(JOB, new ComponentName(ctx, NotifyJob.class))
                .setRequiredNetworkType(JobInfo.NETWORK_TYPE_ANY)
                .setPeriodic(EVERY)
                .setPersisted(true) // resumes after a phone restart
                .build();
        ctx.getSystemService(JobScheduler.class).schedule(job);
    }

    static void cancel(Context ctx) { ctx.getSystemService(JobScheduler.class).cancel(JOB); }

    @Override
    public boolean onStartJob(JobParameters params) {
        new Thread(() -> {
            poll(this);
            jobFinished(params, false);
        }, "noxa-notify").start();
        return true;
    }

    @Override
    public boolean onStopJob(JobParameters params) { return true; }

    /** Messages arrived since the last visit: shown. Device unsubscribed (410): stop. */
    static void poll(Context ctx) {
        SharedPreferences p = prefs(ctx);
        String srv = p.getString("server", null), token = p.getString("push_token", null);
        if (srv == null || token == null) { cancel(ctx); return; }
        try {
            HttpURLConnection c = (HttpURLConnection) new URL(srv + "/api/push/device").openConnection();
            c.setConnectTimeout(15000);
            c.setReadTimeout(20000);
            c.setRequestProperty("X-Noxa", "1");
            c.setRequestProperty("X-Noxa-Device", token);
            int code = c.getResponseCode();
            if (code == 410) { p.edit().remove("push_token").apply(); cancel(ctx); return; }
            if (code != 200) return;
            JSONArray msgs = new JSONObject(read(c.getInputStream())).optJSONArray("messages");
            c.disconnect();
            if (msgs == null) return;
            channel(ctx);
            for (int i = 0; i < msgs.length(); i++) {
                JSONObject m = msgs.getJSONObject(i);
                show(ctx, m.optString("title", "Noxa"), m.optString("body", ""), m.optString("url", "#/"), m.optString("tag", "noxa"));
            }
        } catch (Exception ignored) {
            // offline, server being updated: will try again at the next pass
        }
    }

    static void show(Context ctx, String title, String body, String url, String tag) {
        Intent open = new Intent(ctx, MainActivity.class)
                .putExtra("open", url)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        PendingIntent pi = PendingIntent.getActivity(ctx, tag.hashCode(), open,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        Notification n = new Notification.Builder(ctx, CHANNEL)
                .setSmallIcon(R.drawable.ic_notify)
                .setColor(0xFFFFA43A)
                .setContentTitle(title)
                .setContentText(body)
                .setStyle(new Notification.BigTextStyle().bigText(body))
                .setContentIntent(pi)
                .setAutoCancel(true)
                .build();
        // same topic (same show…): the notification is replaced, not stacked
        ctx.getSystemService(NotificationManager.class).notify(tag.hashCode(), n);
    }

    private static String read(InputStream in) throws Exception {
        byte[] buf = new byte[8192];
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        int n;
        while ((n = in.read(buf)) > 0 && out.size() < (1 << 20)) out.write(buf, 0, n);
        in.close();
        return out.toString("UTF-8");
    }
}
