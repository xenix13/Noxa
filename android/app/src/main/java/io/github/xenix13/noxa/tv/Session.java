package io.github.xenix13.noxa.tv;

import org.json.JSONArray;
import org.json.JSONObject;

/** Signed-in profile, its preferences and its libraries (answer of /api/me), shared by the screens. */
final class Session {
    static JSONObject user = new JSONObject();
    static JSONObject prefs = new JSONObject();
    static JSONArray libraries = new JSONArray();
    static JSONObject features = new JSONObject();
    /** Live TV channels, to move from one to the next in the player. */
    static JSONArray channels = new JSONArray();

    private Session() {}

    static void load(JSONObject me) {
        user = me.optJSONObject("user") != null ? me.optJSONObject("user") : new JSONObject();
        prefs = me.optJSONObject("prefs") != null ? me.optJSONObject("prefs") : new JSONObject();
        libraries = me.optJSONArray("libraries") != null ? me.optJSONArray("libraries") : new JSONArray();
        features = me.optJSONObject("features") != null ? me.optJSONObject("features") : new JSONObject();
        Tr.use(user.optString("language", Tr.lang));
    }

    static String pref(String key, String def) {
        return prefs.optString(key, def);
    }

    static boolean flag(String key, boolean def) {
        return prefs.optBoolean(key, def);
    }

    static boolean live() {
        return features.optBoolean("live", false);
    }
}
