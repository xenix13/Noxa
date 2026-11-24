package io.github.xenix13.noxa.tv;

import android.view.View;

/** A screen of the app (sign-in, profiles, navigation, title page…), stacked by MainActivity. */
abstract class Screen {
    final MainActivity act;
    View view;
    View lastFocus;

    Screen(MainActivity act) {
        this.act = act;
    }

    abstract View create();

    /** Back in the foreground (after playback, a title page…): the screen can update. */
    void onResume() {}

    /** Back key: true when the screen handled it itself. */
    boolean onBack() {
        return false;
    }

    void onDestroy() {}

    int dp(float v) {
        return Ui.dp(act, v);
    }
}
