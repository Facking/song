from flask import Flask, request, jsonify
import subprocess, tempfile, os, requests, traceback

app = Flask(__name__)
AUDD_TOKEN = os.environ.get("AUDD_TOKEN", "")

@app.route("/")
def home():
    return {"status": "ok", "service": "music-identify", "ffmpeg": _has_ffmpeg(), "token": bool(AUDD_TOKEN)}

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
    if not stream or not isinstance(stream, str) or not stream.startswith("http"):
        return jsonify({"ok": False, "error": "bad_stream", "message": "آدرس استریم نامعتبر"}), 400

    if not AUDD_TOKEN:
        return jsonify({"ok": False, "error": "no_token", "message": "توکن AudD تنظیم نشده"}), 500

    if not _has_ffmpeg():
        return jsonify({"ok": False, "error": "no_ffmpeg", "message": "ffmpeg روی سرور نصب نیست"}), 500

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

    try:
        with open(tmp, "rb") as f:
            r = requests.post(
                "https://api.audd.io/",
                data={"api_token": AUDD_TOKEN, "return": "apple_music,spotify,deezer,lyrics"},
                files={"file": f},
                timeout=30
            )
        result_json = r.json()
    except Exception as e:
        return jsonify({"ok": False, "error": "audd_error", "message": str(e)}), 500
    finally:
        if os.path.exists(tmp): os.unlink(tmp)

    if result_json.get("status") != "success":
        err = result_json.get("error", {})
        err_msg = err.get("error_message", "خطای AudD") if isinstance(err, dict) else str(err)
        return jsonify({"ok": False, "error": "audd_bad", "message": err_msg}), 500

    if not result_json.get("result"):
        return jsonify({"ok": False, "error": "not_found", "message": "آهنگ شناسایی نشد"}), 404

    r = result_json["result"]
    title = r.get("title", "")
    artist = r.get("artist", "")
    album = r.get("album", "")

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

    lrc = None
    if artist and title:
        try:
            lr = requests.get(
                "https://lrclib.net/api/get",
                params={"artist_name": artist, "track_name": title, "album_name": album},
                timeout=12
            )
            if lr.ok and lr.json().get("syncedLyrics"):
                lrc = lr.json()["syncedLyrics"]
            else:
                sr = requests.get(
                    "https://lrclib.net/api/search",
                    params={"q": f"{artist} {title}"},
                    timeout=12
                )
                if sr.ok:
                    for item in sr.json():
                        if item.get("syncedLyrics"):
                            lrc = item["syncedLyrics"]
                            break
        except Exception:
            pass

    timecode = r.get("timecode") if isinstance(r.get("timecode"), (int, float)) else None

    return jsonify({
        "ok": True,
        "title": title,
        "artist": artist,
        "album": album,
        "cover": cover,
        "links": links,
        "lyrics_plain": r.get("lyrics", ""),
        "lrc": lrc,
        "timecode": timecode
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
