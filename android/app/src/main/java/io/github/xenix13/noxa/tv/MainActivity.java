package io.github.xenix13.noxa.tv;

import android.app.Activity;
import android.content.SharedPreferences;
import android.os.Bundle;
import android.view.KeyEvent;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowManager;
import android.widget.FrameLayout;

import java.util.ArrayDeque;

/**
 * Noxa for Android TV: native app (screens drawn by the app, playback by ExoPlayer).
 * At launch: server address, then sign-in, then "Who's watching?", then the home page.
 */
public class MainActivity extends Activity {
    private FrameLayout root;
    private final ArrayDeque<Screen> stack = new ArrayDeque<>();
    private boolean started;
    private boolean quitting;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        Api.init(this);
        Tr.init();
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        root = new FrameLayout(this);
        root.setBackgroundColor(Ui.BG);
        setContentView(root);
        immersive();
        start();
    }

    /** Within this time of the last use, a restart of the app (Android freeing memory during playback, or a crash
     *  on leaving the player) goes back to the same profile instead of asking "Who's watching?" again. */
    private static final long SAME_SESSION_MS = 30 * 60 * 1000L;

    /** Marks the app as in use (screens and player call it). */
    static void touch() {
        SharedPreferences s = Api.store();
        if (s != null) s.edit().putLong("last_used", System.currentTimeMillis()).apply();
    }

    /** Closed on purpose: the next launch asks "Who's watching?" again. */
    void quit() {
        quitting = true;
        SharedPreferences s = Api.store();
        if (s != null) s.edit().remove("last_used").apply();
        finish();
    }

    private static boolean recentlyUsed() {
        SharedPreferences s = Api.store();
        long last = s == null ? 0 : s.getLong("last_used", 0);
        long age = System.currentTimeMillis() - last;
        return last > 0 && age >= 0 && age < SAME_SESSION_MS;
    }

    /** Starting point: known server? session still valid? */
    void start() {
        if (Api.server() == null) {
            reset(new SetupScreen(this, null));
            return;
        }
        reset(new LoadingScreen(this, Tr.t("Connexion à ") + Api.server() + "…"));
        Api.obj("/api/me", me -> {
            Session.load(me);
            afterLogin(!recentlyUsed());
        }, e -> {
            if (e.code == 401) reset(new LoginScreen(this));
            else reset(new SetupScreen(this, e.code == 0 ? Tr.t("Le serveur ") + Api.server() + Tr.t(" ne répond pas. ")
                    + Tr.t("Vérifiez la connexion réseau ou modifiez l'adresse.") : e.getMessage()));
        });
    }

    /** Signed in: profile choice when the household has several, otherwise home. */
    void afterLogin(boolean askProfile) {
        if (!askProfile) {
            openBrowse();
            return;
        }
        Api.arr("/api/profiles", list -> {
            if (list.length() > 1) reset(new ProfilesScreen(this, list));
            else openBrowse();
        }, e -> openBrowse());
    }

    /** Reloads the profile (/api/me) then opens the navigation. */
    void openBrowse() {
        Api.obj("/api/me", me -> {
            Session.load(me);
            reset(new BrowseScreen(this));
        }, e -> {
            if (e.code == 401) reset(new LoginScreen(this));
            else Ui.toast(this, e.getMessage());
        });
    }

    void logout() {
        Api.send("POST", "/api/logout", null, r -> {
            Api.forgetSession();
            reset(new LoginScreen(this));
        }, e -> {
            Api.forgetSession();
            reset(new LoginScreen(this));
        });
    }

    // ------------------------------------------------------------------ screen stack
    void push(Screen s) {
        Screen top = stack.peek();
        if (top != null) {
            top.lastFocus = getCurrentFocus();
            root.removeView(top.view);
        }
        stack.push(s);
        show(s, true);
    }

    void reset(Screen s) {
        while (!stack.isEmpty()) {
            Screen old = stack.pop();
            root.removeView(old.view);
            old.onDestroy();
        }
        stack.push(s);
        show(s, true);
    }

    void pop() {
        if (stack.size() <= 1) {
            finish();
            return;
        }
        Screen old = stack.pop();
        root.removeView(old.view);
        old.onDestroy();
        Screen top = stack.peek();
        show(top, false);
        top.onResume();
    }

    private void show(Screen s, boolean fresh) {
        if (fresh || s.view == null) s.view = s.create();
        root.addView(s.view, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        View f = s.lastFocus;
        if (!fresh && f != null && f.isAttachedToWindow()) f.requestFocus();
    }

    Screen top() {
        return stack.peek();
    }

    // ------------------------------------------------------------------ remote control
    @Override
    @SuppressWarnings("deprecation")
    public void onBackPressed() {
        Screen top = stack.peek();
        if (top != null && top.onBack()) return;
        pop();
    }

    @Override
    public boolean dispatchKeyEvent(KeyEvent event) {
        // some remotes send Escape instead of Back: same handling
        if (event.getKeyCode() == KeyEvent.KEYCODE_ESCAPE) {
            if (event.getAction() == KeyEvent.ACTION_UP) onBackPressed();
            return true;
        }
        return super.dispatchKeyEvent(event);
    }

    @Override
    protected void onPause() {
        super.onPause();
        if (!quitting) touch();
    }

    @Override
    protected void onResume() {
        super.onResume();
        touch();
        immersive();
        if (started) {
            // back from the player: resume and "watched" up to date (the time for the last position to reach the server)
            root.postDelayed(() -> {
                Screen top = stack.peek();
                if (top != null && top.view != null && top.view.isAttachedToWindow()) top.onResume();
            }, 700);
        }
        started = true;
    }

    @SuppressWarnings("deprecation")
    void immersive() {
        getWindow().getDecorView().setSystemUiVisibility(View.SYSTEM_UI_FLAG_FULLSCREEN
                | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION | View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION);
    }
}
