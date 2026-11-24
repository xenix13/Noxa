import { tr } from './i18n.js';
// Choice of audio / subtitle tracks: choice remembered for the movie or the show, otherwise general preferences.

// languages specific to a library (Settings → Playback), instead of the general languages
export function withLibLangs(prefs, item) {
  const o = item && prefs.lib_langs && prefs.lib_langs[String(item.section)];
  if (!o) return prefs;
  const out = { ...prefs };
  for (const k of ['audio_lang', 'sub_mode', 'sub_lang']) if (o[k] != null && o[k] !== '') out[k] = o[k];
  return out;
}

export function pickAudio(audio, prefs, trackPrefs) {
  if (!audio || !audio.length) return null;
  const want = (trackPrefs && trackPrefs.audio_lang) || prefs.audio_lang;
  return (want && audio.find((a) => a.lang === want)) || audio.find((a) => a.default) || audio[0];
}

export function pickSubtitle(subs, audio, prefs, trackPrefs) {
  if (!subs || !subs.length) return null;
  if (trackPrefs && trackPrefs.sub_lang) {
    if (trackPrefs.sub_lang === 'off') return null;
    const same = subs.filter((s) => s.lang === trackPrefs.sub_lang);
    // "forced" is only used when the audio is in the same language (otherwise there would only be the signs)
    const forced = !!trackPrefs.sub_forced && !!(audio && audio.lang === trackPrefs.sub_lang);
    const pick = same.find((s) => s.forced === forced && s.overlay)
      || same.find((s) => s.forced === forced) || same[0];
    if (pick) return fixForced(pick, audio, subs);
  }
  if (prefs.sub_mode === 'never') return null;
  const lang = prefs.sub_lang;
  const inLang = subs.filter((s) => !lang || s.lang === lang);
  const best = inLang.find((s) => !s.forced && s.overlay) || inLang.find((s) => !s.forced) || inLang[0];
  if (prefs.sub_mode === 'always') return best || null;
  // "foreign": subtitles only when the audio is not in the user's language
  if (audio && prefs.audio_lang && audio.lang && audio.lang !== prefs.audio_lang) return best || null;
  return subs.find((s) => s.forced && audio && s.lang === audio.lang) || null;
}

export function trackPayload(audio, sub) {
  return { audio_lang: audio ? audio.lang || '' : undefined, sub_lang: sub ? sub.lang || '' : 'off', sub_forced: !!(sub && sub.forced) };
}

// "Forced" tracks: they only hold the translated signs, inscriptions and songs, for someone
// ALREADY watching in that language (dub of an anime: the Japanese signs still need translating). With another
// audio, they look empty: they are hidden when a full track exists, unless the audio suits them.
export function isSignsOnly(s, subs) {
  return !!s.forced && subs.some((x) => !x.forced && x.lang === s.lang);
}

export function menuSubs(subs, audio) {
  return (subs || []).filter((s) => !isSignsOnly(s, subs) || (audio && audio.lang && audio.lang === s.lang));
}

export const SIGNS_HINT = tr('panneaux et chansons seulement');

// A forced track chosen with audio in another language -> the full track of the same language
export function fixForced(sub, audio, subs) {
  if (!sub || !isSignsOnly(sub, subs) || (audio && audio.lang === sub.lang)) return sub;
  return subs.find((x) => !x.forced && x.lang === sub.lang && x.overlay) || subs.find((x) => !x.forced && x.lang === sub.lang) || sub;
}
