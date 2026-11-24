package io.github.xenix13.noxa.tv;

import android.content.Context;
import android.graphics.Color;
import android.graphics.Typeface;
import android.media.MediaCodecInfo;
import android.media.MediaCodecList;

import androidx.annotation.OptIn;
import androidx.media3.common.AudioAttributes;
import androidx.media3.common.C;
import androidx.media3.common.util.UnstableApi;
import androidx.media3.datasource.DefaultHttpDataSource;
import androidx.media3.exoplayer.DefaultLoadControl;
import androidx.media3.exoplayer.DefaultRenderersFactory;
import androidx.media3.exoplayer.ExoPlayer;
import androidx.media3.exoplayer.audio.AudioCapabilities;
import androidx.media3.exoplayer.source.DefaultMediaSourceFactory;
import androidx.media3.ui.CaptionStyleCompat;
import androidx.media3.ui.SubtitleView;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.Collections;
import java.util.HashSet;
import java.util.Set;

/** ExoPlayer player set up for Noxa, and the device's decoding profile sent to the server. */
@OptIn(markerClass = UnstableApi.class)
final class Players {
    private static JSONObject caps;

    private Players() {}

    static ExoPlayer create(Context c) {
        DefaultHttpDataSource.Factory http = new DefaultHttpDataSource.Factory()
                .setUserAgent(Api.VERSION_HEADER)
                .setConnectTimeoutMs(15000)
                // a segment converted on the fly can take a while to arrive (ffmpeg restarted after a seek)
                .setReadTimeoutMs(120000)
                .setAllowCrossProtocolRedirects(true)
                .setDefaultRequestProperties(Collections.singletonMap("Cookie", Api.cookieHeader()));
        DefaultRenderersFactory renderers = new DefaultRenderersFactory(c).setEnableDecoderFallback(true);
        DefaultLoadControl load = new DefaultLoadControl.Builder()
                .setBufferDurationsMs(20000, 90000, 2500, 5000)
                .build();
        ExoPlayer p = new ExoPlayer.Builder(c, renderers)
                .setMediaSourceFactory(new DefaultMediaSourceFactory(c).setDataSourceFactory(http))
                .setLoadControl(load)
                .build();
        p.setAudioAttributes(new AudioAttributes.Builder().setUsage(C.USAGE_MEDIA)
                .setContentType(C.AUDIO_CONTENT_TYPE_MOVIE).build(), true);
        return p;
    }

    /** Subtitles: size and background chosen in the account's preferences. */
    static void styleSubtitles(SubtitleView v) {
        if (v == null) return;
        String bg = Session.pref("sub_bg", "box");
        int background = "solid".equals(bg) ? Color.BLACK : "none".equals(bg) ? Color.TRANSPARENT : Color.argb(150, 0, 0, 0);
        int color;
        try {
            color = Color.parseColor(Session.pref("sub_color", "#ffffff"));
        } catch (IllegalArgumentException e) {
            color = Color.WHITE;
        }
        CaptionStyleCompat style = new CaptionStyleCompat(color, background, Color.TRANSPARENT,
                "none".equals(bg) ? CaptionStyleCompat.EDGE_TYPE_OUTLINE : CaptionStyleCompat.EDGE_TYPE_DROP_SHADOW,
                Color.BLACK, Typeface.DEFAULT_BOLD);
        v.setStyle(style);
        v.setApplyEmbeddedStyles(false);
        v.setApplyEmbeddedFontSizes(false);
        int size = Session.prefs.optInt("sub_size", 100);
        v.setFractionalTextSize(SubtitleView.DEFAULT_TEXT_SIZE_FRACTION * size / 100f);
        v.setBottomPaddingFraction("top".equals(Session.pref("sub_position", "bottom")) ? 0.8f
                : SubtitleView.DEFAULT_BOTTOM_PADDING_FRACTION);
    }

    /**
     * What the device can play: video and audio decoders present, multichannel audio passed as is to
     * the amplifier when the HDMI output accepts it. The server only converts what is missing.
     */
    @SuppressWarnings("deprecation")
    static synchronized JSONObject caps(Context c) {
        if (caps != null) return caps;
        Set<String> video = new HashSet<>(), audio = new HashSet<>();
        video.add("h264");
        audio.add("aac");
        audio.add("mp3");
        for (MediaCodecInfo info : new MediaCodecList(MediaCodecList.REGULAR_CODECS).getCodecInfos()) {
            if (info.isEncoder()) continue;
            for (String type : info.getSupportedTypes()) {
                String t = type.toLowerCase();
                switch (t) {
                    case "video/hevc":
                        video.add("hevc");
                        if (hasProfile(info, type, MediaCodecInfo.CodecProfileLevel.HEVCProfileMain10)) video.add("hevc10");
                        break;
                    case "video/av01":
                        video.add("av1");
                        if (hasProfile(info, type, 0x2 /* AV1ProfileMain10 */)) video.add("av1_10");
                        break;
                    case "video/x-vnd.on2.vp9":
                        video.add("vp9");
                        break;
                    case "video/mpeg2":
                        video.add("mpeg2video");
                        break;
                    case "video/wvc1":
                    case "video/vc1":
                        video.add("vc1");
                        break;
                    case "audio/ac3":
                        audio.add("ac3");
                        break;
                    case "audio/eac3":
                        audio.add("eac3");
                        break;
                    case "audio/vnd.dts":
                    case "audio/vnd.dts.hd":
                        audio.add("dts");
                        break;
                    case "audio/true-hd":
                        audio.add("truehd");
                        break;
                    case "audio/flac":
                        audio.add("flac");
                        break;
                    case "audio/opus":
                        audio.add("opus");
                        break;
                    case "audio/vorbis":
                        audio.add("vorbis");
                        break;
                    default:
                        break;
                }
            }
        }
        try {
            AudioCapabilities ac = AudioCapabilities.getCapabilities(c);
            if (ac.supportsEncoding(C.ENCODING_AC3)) audio.add("ac3");
            if (ac.supportsEncoding(C.ENCODING_E_AC3) || ac.supportsEncoding(C.ENCODING_E_AC3_JOC)) audio.add("eac3");
            if (ac.supportsEncoding(C.ENCODING_DTS) || ac.supportsEncoding(C.ENCODING_DTS_HD)) audio.add("dts");
            if (ac.supportsEncoding(C.ENCODING_DOLBY_TRUEHD)) audio.add("truehd");
        } catch (RuntimeException ignored) {
            // unknown audio output: stick to the decoders
        }
        JSONObject o = new JSONObject();
        try {
            o.put("video", new JSONArray(video));
            o.put("audio", new JSONArray(audio));
            o.put("containers", new JSONArray(new String[]{"matroska", "mp4", "mov", "webm", "mpegts"}));
            o.put("native", true);
            o.put("multichannel", true);
            o.put("pgs", false);
        } catch (org.json.JSONException ignored) {
            // literal keys
        }
        caps = o;
        return caps;
    }

    private static boolean hasProfile(MediaCodecInfo info, String type, int profile) {
        try {
            for (MediaCodecInfo.CodecProfileLevel pl : info.getCapabilitiesForType(type).profileLevels) {
                if (pl.profile == profile) return true;
            }
        } catch (IllegalArgumentException ignored) {
            // type not handled by this decoder
        }
        return false;
    }
}
