from flask import Flask, request, jsonify
import subprocess, tempfile, os, requests

app = Flask(__name__)

AUDD_TOKEN = os.environ.get("AUDD_TOKEN", "")

@app.route("/")
def home():
    return {"status": "ok", "service": "music-identify"}

@app.route("/identify", methods=["POST"])
def identify():
    data = request.get_json(silent=True) or {}
    stream = data.get("stream", "")
    if not stream.startswith("http"):
        return jsonify({"ok": False, "error": "bad_stream"}), 400

    tmp = tempfile.mktemp(suffix=".mp3")
    try:
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-i", stream, "-t", "8", "-ac", "1", "-ar", "16000",
             "-b:a", "48k", tmp],
            timeout=25, check=True
        )
    except Exception as e:
        return jsonify({"ok": False, "error": "capture_failed", "message": str(e)}), 500

    if not os.path.exists(tmp) or os.path.getsize(tmp) < 2000:
        os.unlink(tmp)
        return jsonify({"ok": False, "error": "capture_empty"}), 500

    try:
        with open(tmp, "rb") as f:
            r = requests.post(
                "https://api.audd.io/",
                data={"api_token": AUDD_TOKEN, "return": "apple_music,spotify,deezer,lyrics"},
                files={"file": f},
                timeout=30
            )
        result = r.json()
    finally:
        os.unlink(tmp)

    if result.get("status") != "success" or not result.get("result"):
        return jsonify({"ok": False, "error": "not_found"}), 404

    r = result["result"]
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

    return jsonify({
        "ok": True,
        "title": title,
        "artist": artist,
        "album": album,
        "cover": cover,
        "links": links,
        "lyrics_plain": r.get("lyrics", ""),
        "lrc": lrc
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
