// Cast to a TV: AirPlay (Safari, to an Apple TV) and Chromecast (Chrome / Edge, to a Chromecast or
// a Google TV). In both cases the TV fetches the video: the server prepares a stream it
// can play, at an address usable without the browser session (for the duration of the playback).
import { tr } from './i18n.js';

const SDK = 'https://www.gstatic.com/cv/js/sender/v1/cast_sender.js?loadCastFramework=1';

// what the Chromecast's default receiver plays (HLS in fMP4, H.264 and stereo AAC)
export const CHROMECAST_CAPS = { video: ['h264'], audio: ['aac', 'mp3'], containers: ['mp4'], native: false };
// Apple TV: native HLS, H.264 and HEVC, Dolby multichannel audio passed through
export const AIRPLAY_CAPS = { video: ['h264', 'hevc', 'hevc10'], audio: ['aac', 'ac3', 'eac3', 'mp3'], containers: ['mp4'],
  native: false, multichannel: true };

// ------------------------------------------------------------ AirPlay
export const airplaySupported = () => !!window.WebKitPlaybackTargetAvailabilityEvent;

// onAvailable(bool): is an Apple TV in range; onWireless(bool): is the video playing on the TV
export function watchAirplay(video, onAvailable, onWireless) {
  if (!airplaySupported()) return () => {};
  const a = (e) => onAvailable(e.availability === 'available');
  const w = () => onWireless(!!video.webkitCurrentPlaybackTargetIsWireless);
  video.addEventListener('webkitplaybacktargetavailabilitychanged', a);
  video.addEventListener('webkitcurrentplaybacktargetiswirelesschanged', w);
  return () => {
    video.removeEventListener('webkitplaybacktargetavailabilitychanged', a);
    video.removeEventListener('webkitcurrentplaybacktargetiswirelesschanged', w);
  };
}

// ------------------------------------------------------------ Chromecast
let sdk = null;
export const chromecastPossible = () => !!window.chrome && !/firefox|fxios/i.test(navigator.userAgent) && window.isSecureContext !== false;

// Google's SDK, loaded when the player opens, in Chrome / Edge only -> cast.framework, or null
export function loadCastSdk() {
  if (!chromecastPossible()) return Promise.resolve(null);
  if (sdk) return sdk;
  sdk = new Promise((resolve) => {
    const done = (ok) => {
      if (!ok || !window.cast || !window.cast.framework) { resolve(null); return; }
      try {
        window.cast.framework.CastContext.getInstance().setOptions({
          receiverApplicationId: window.chrome.cast.media.DEFAULT_MEDIA_RECEIVER_APP_ID,
          autoJoinPolicy: window.chrome.cast.AutoJoinPolicy.ORIGIN_SCOPED,
        });
        resolve(window.cast.framework);
      } catch { resolve(null); }
    };
    window.__onGCastApiAvailable = done;
    const s = document.createElement('script');
    s.src = SDK;
    s.async = true;
    s.onerror = () => resolve(null);
    document.head.append(s);
    setTimeout(() => resolve(null), 15000);
  });
  return sdk;
}

// Loads the stream prepared by the server on the connected Chromecast -> RemotePlayer and its controller
export async function castLoad(framework, { url, mode, offset, title, subtitle, image }) {
  const session = framework.CastContext.getInstance().getCurrentSession();
  if (!session) throw new Error(tr('Aucun appareil de diffusion connecté'));
  const media = window.chrome.cast.media;
  const absolute = new URL(url, location.href).href;
  const info = new media.MediaInfo(absolute, mode === 'direct' ? 'video/mp4' : 'application/x-mpegurl');
  if (mode !== 'direct') {
    info.hlsSegmentFormat = media.HlsSegmentFormat.FMP4;
    info.hlsVideoSegmentFormat = media.HlsVideoSegmentFormat.FMP4;
  }
  info.streamType = media.StreamType.BUFFERED;
  const meta = new media.GenericMediaMetadata();
  meta.title = title;
  if (subtitle) meta.subtitle = subtitle;
  if (image) meta.images = [new window.chrome.cast.Image(new URL(image, location.href).href)];
  info.metadata = meta;
  const req = new media.LoadRequest(info);
  req.currentTime = (offset || 0) / 1000;
  req.autoplay = true;
  await session.loadMedia(req);
  return session.getSessionObj().receiver.friendlyName;
}

export function castDeviceName(framework) {
  const s = framework && framework.CastContext.getInstance().getCurrentSession();
  return s ? s.getSessionObj().receiver.friendlyName : '';
}
