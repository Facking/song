from flask import Flask, request, jsonify
import subprocess, tempfile, os, requests, traceback, asyncio

app = Flask(__name__)
AUDD_TOKEN = os.environ.get("AUDD_TOKEN", "")

@app.route("/")
def home():
    return {
        "status": "ok",
        "service": "music-identify",
        "ffmpeg": _has_ffmpeg(),
        "token": bool(AUDD_TOKEN),
        "engines": ["shazam", "audd"]
    }

def _has_ffmpeg():
    try:
        r = subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False

@app.route("/identify", methods=["POST"])
def identify():
    try:
        return _identify_impl()
    except Exception as e:
        return jsonify({
            "ok": False,
            "error": "unhandled",
            "message": str(e),
            "trace": traceback.format_exc()[:800]
        }), 500

def _identify_impl():
    data = request.get_json(silent=True) or {}
    stream = data.get("stream", "")
    engine = (data.get("engine") or "shazam").lower()

    if not stream or not isinstance(stream, str) or not stream.startswith("http"):
        return jsonify({"ok": False, "error": "bad_stream", "message": "آدرس استریم نامعتبر"}), 400

    if not _has_ffmpeg():
        return jsonify({"ok": False, "error": "no_ffmpeg", "message": "ffmpeg روی سرور نصب نیست"}), 500

    # ضبط ۸ ثانیه از استریم
    tmp = tempfile.mktemp(suffix=".mp3")
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-i", stream, "-t", "8", "-ac", "1", "-ar", "16000",
             "-b:a", "48k", tmp],
            timeout=25, capture_output=True
        )
        if result.returncode != 0:
            err = (result.stderr or b"").decode("utf-8", errors="replace")[:300]
            return jsonify({"ok": False, "error": "capture_failed", "message": err or "ffmpeg خطا داد"}), 500
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "capture_timeout", "message": "زمان ضبط تمام شد"}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": "capture_error", "message": str(e)}), 500

    if not os.path.exists(tmp) or os.path.getsize(tmp) < 2000:
        if os.path.exists(tmp): os.unlink(tmp)
        return jsonify({"ok": False, "error": "capture_empty", "message": "فایل ضبط‌شده خالی است"}), 500

    # شناسایی
    try:
        if engine == "audd":
            res = _identify_via_audd(tmp)
        else:
            # Shazam پیش‌فرض — در صورت شکست به AudD برمی‌گردد
            res = _identify_via_shazam(tmp)
            if not res.get("ok") and AUDD_TOKEN:
                fallback = _identify_via_audd(tmp)
                if fallback.get("ok"):
                    fallback["engine"] = "audd (fallback)"
                    return jsonify(fallback), 200
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

    if res.get("ok"):
        res["engine"] = engine

    status = 200 if res.get("ok") else (404 if res.get("error") == "not_found" else 500)
    return jsonify(res), status


# ==================== ShazamIO ====================
def _identify_via_shazam(audio_path: str) -> dict:
    try:
        from shazamio import Shazam
    except ImportError as e:
        return {"ok": False, "error": "shazam_missing", "message": "shazamio نصب نیست: " + str(e)}

    async def _run():
        async with Shazam() as shazam:
            return await shazam.recognize(audio_path)

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(asyncio.wait_for(_run(), timeout=25))
        finally:
            loop.close()
    except asyncio.TimeoutError:
        return {"ok": False, "error": "shazam_timeout", "message": "Shazam پاسخ نداد"}
    except Exception as e:
        return {"ok": False, "error": "shazam_error", "message": str(e)[:300]}

    if not result or not result.get("track"):
        return {"ok": False, "error": "not_found", "message": "آهنگ شناسایی نشد"}

    track = result["track"]
    title  = track.get("title", "") or ""
    artist = track.get("subtitle", "") or ""

    # کاور: بالاترین کیفیت از images
    cover = ""
    for img in (track.get("images") or {}).get("coverart", []) or []:
        cover = img.get("url", "") or cover
    if not cover:
        cover = (track.get("images") or {}).get("background", "") or ""

    # لینک‌ها
    links = {}
    if track.get("url"):
        links["shazam"] = track["url"]

    # جستجوی Spotify/Apple از طریق متادیتای Shazam
    for section in track.get("sections", []) or []:
        for meta in section.get("metadata", []) or []:
            if meta.get("title") == "Album":
                pass  # می‌توان آلبوم را اینجا استخراج کرد

    # آلبوم
    album = ""
    for section in track.get("sections", []) or []:
        for meta in section.get("metadata", []) or []:
            if meta.get("title") == "Album":
                album = meta.get("text", "") or ""

    # متن همگام از LRCLIB
    lrc = None
    if artist and title:
        lrc = _lrclib_fetch(artist, title, album)

    return {
        "ok": True,
        "title": title,
        "artist": artist,
        "album": album,
        "cover": cover,
        "links": links,
        "lyrics_plain": "",
        "lrc": lrc,
        "timecode": None
    }


# ==================== AudD (پشتیبان) ====================
def _identify_via_audd(audio_path: str) -> dict:
    if not AUDD_TOKEN:
        return {"ok": False, "error": "no_token", "message": "توکن AudD تنظیم نشده"}

    try:
        with open(audio_path, "rb") as f:
            r = requests.post(
                "https://api.audd.io/",
                data={"api_token": AUDD_TOKEN, "return": "apple_music,spotify,deezer,lyrics"},
                files={"file": f},
                timeout=30
            )
        result_json = r.json()
    except Exception as e:
        return {"ok": False, "error": "audd_error", "message": str(e)}

    if result_json.get("status") != "success":
        err = result_json.get("error", {})
        err_msg = err.get("error_message", "خطای AudD") if isinstance(err, dict) else str(err)
        return {"ok": False, "error": "audd_bad", "message": err_msg}

    if not result_json.get("result"):
        return {"ok": False, "error": "not_found", "message": "آهنگ شناسایی نشد"}

    r = result_json["result"]
    title  = r.get("title", "")
    artist = r.get("artist", "")
    album  = r.get("album", "")

    cover = (
        r.get("apple_music", {}).get("artwork", {}).get("url")
        or (r.get("spotify", {}).get("album", {}).get("images") or [{}])[0].get("url")
        or r.get("deezer", {}).get("album", {}).get("cover_xl", "")
    )

    links = {}
    if r.get("spotify", {}).get("external_urls", {}).get("spotify"):
        links["spotify"] = r["spotify"]["external_urls"]["spotify"]
    if r.get("apple_music", {}).get("url"):
        links["apple"] = r["apple_music"]["url"]
    if r.get("deezer", {}).get("link"):
        links["deezer"] = r["deezer"]["link"]
    if r.get("song_link"):
        links["audd"] = r["song_link"]

    lrc = _lrclib_fetch(artist, title, album)

    timecode = r.get("timecode") if isinstance(r.get("timecode"), (int, float)) else None

    return {
        "ok": True,
        "title": title,
        "artist": artist,
        "album": album,
        "cover": cover,
        "links": links,
        "lyrics_plain": r.get("lyrics", ""),
        "lrc": lrc,
        "timecode": timecode
    }


# ==================== LRCLIB ====================
def _lrclib_fetch(artist: str, title: str, album: str = "") -> str | None:
    if not artist or not title:
        return None
    try:
        lr = requests.get(
            "https://lrclib.net/api/get",
            params={"artist_name": artist, "track_name": title, "album_name": album},
            timeout=12
        )
        if lr.ok and lr.json().get("syncedLyrics"):
            return lr.json()["syncedLyrics"]
    except Exception:
        pass

    try:
        sr = requests.get(
            "https://lrclib.net/api/search",
            params={"q": f"{artist} {title}"},
            timeout=12
        )
        if sr.ok:
            for item in sr.json():
                if item.get("syncedLyrics"):
                    return item["syncedLyrics"]
    except Exception:
        pass

    return None


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
