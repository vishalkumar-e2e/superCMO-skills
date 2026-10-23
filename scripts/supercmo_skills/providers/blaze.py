"""Blaze provider — queued video on Blaze Video (MiniMax H3), OpenAI-style /v1/videos.

Uniform provider contract:
  BYOK_ENV                            env var whose presence means "use me directly"
  is_available()                      True when that key is set
  video_submit / video_status         queued job: POST /v1/videos, GET /v1/videos/{id}
  video_generate(route, payload, key) submit + blocking poll
  video_request_spec(route, payload)  dry-run shape (key masked)

Blaze takes every picture, video and audio as a public https URL or a base64 data: URI, so a local
file (already a data: URI by the time it reaches us) is sent inline; no upload step is needed.

The finished clip lives behind GET /v1/videos/{id}/content, which needs the API key, so the status
call downloads it here and hands the bytes back inline (`b64`) for the client to persist.

Environment:
  BLAZE_API_KEY            the Blaze API key
  BLAZE_API_URL            API base (default: the Blaze staging API)
  BLAZE_ENHANCE_PROMPT     "false" to send the prompt as written (default "true": Blaze enhances it)
"""
import base64
import json
import os
import time

import supercmo_env

BYOK_ENV = "BLAZE_API_KEY"
KEY_ENABLES = "video (MiniMax H3)"
KEY_SIGNUP = "Blaze console"
DEFAULT_API_URL = "https://blaze.e2enetworks.dev"
MODEL_ID = "MiniMaxAI/MiniMax-H3"
MAX_PARALLEL = 2         # H3 renders one clip at a time per pool; extra jobs only queue
_POLL_INTERVAL = 5
_MAX_POLLS = 100
_STATUS_POLL_TIMEOUT = 15
_CONTENT_TIMEOUT = 120
_PENDING_STATES = ("queued", "in_progress", "running", "preparing", "pending")
_FAILED_STATES = ("failed", "cancelled", "canceled", "expired")
SIZES = {"16:9": "1344x768", "9:16": "768x1344", "21:9": "1344x576", "4:3": "1024x768",
         "3:4": "768x1024", "1:1": "768x768"}
DEFAULT_ASPECT = "16:9"
DEFAULT_SECONDS = 6
MAX_IMAGES, MAX_VIDEOS, MAX_AUDIOS = 9, 3, 3


def is_available():
    return bool(os.environ.get(BYOK_ENV))


def api_url():
    return (os.environ.get("BLAZE_API_URL") or DEFAULT_API_URL).rstrip("/")


def _enhance():
    return (os.environ.get("BLAZE_ENHANCE_PROMPT") or "true").strip().lower() not in ("0", "false", "no", "off")


def _headers(key):
    return {"Authorization": f"Bearer {key}", "Accept": "application/json"}


# --------------------------------------------------------------------------------- media
def _hosted(value):
    """An https URL or a data: URI (a local file, encoded by the client) goes to Blaze as is."""
    if isinstance(value, str) and (value.startswith("https://") or value.startswith("data:")):
        return value, None
    return None, "Blaze accepts https URLs or local files"


# ------------------------------------------------------------------------- request body
def _media_count(payload):
    return sum(len(payload.get(k) or []) for k in ("reference_images", "reference_videos", "reference_audios")) + \
        sum(1 for k in ("start_frame_image", "end_frame_image") if payload.get(k))


def _check(payload):
    if _media_count(payload) == 0:
        return ("MiniMax H3 on Blaze needs at least one picture, video or audio reference — "
                "pass a start_frame_image or reference_images.")
    images = len(payload.get("reference_images") or []) + \
        sum(1 for k in ("start_frame_image", "end_frame_image") if payload.get(k))
    if images > MAX_IMAGES:
        return f"MiniMax H3 on Blaze accepts at most {MAX_IMAGES} pictures (frames + references); got {images}."
    if len(payload.get("reference_videos") or []) > MAX_VIDEOS:
        return f"MiniMax H3 on Blaze accepts at most {MAX_VIDEOS} reference videos."
    if len(payload.get("reference_audios") or []) > MAX_AUDIOS:
        return f"MiniMax H3 on Blaze accepts at most {MAX_AUDIOS} reference audios."
    return None


def _seconds(payload):
    d = payload.get("duration")
    d = DEFAULT_SECONDS if d is None else int(d)
    return max(4, min(15, d))


def _build_body(payload, resolve):
    """The /v1/videos body. `resolve(value)` → (url, error) turns each media value into a URL."""
    aspect = (payload.get("aspect_ratio") or DEFAULT_ASPECT).lower()
    body = {"model": MODEL_ID, "prompt": payload["prompt"], "mode": "reference_to_video",
            "seconds": _seconds(payload), "size": SIZES.get(aspect, SIZES[DEFAULT_ASPECT]),
            "enhance_prompt": _enhance()}
    images = []
    for field, typ in (("start_frame_image", "scene"),):
        if payload.get(field):
            url, err = resolve(payload[field])
            if err:
                return None, err
            images.append({"url": url, "type": typ})
    for v in payload.get("reference_images") or []:
        url, err = resolve(v)
        if err:
            return None, err
        images.append({"url": url, "type": "identity"})
    if payload.get("end_frame_image"):
        url, err = resolve(payload["end_frame_image"])
        if err:
            return None, err
        images.append({"url": url, "type": "end"})
    if images:
        body["reference_images"] = images
    for field, key in (("reference_videos", "reference_videos"), ("reference_audios", "audio_references")):
        items = []
        for v in payload.get(field) or []:
            url, err = resolve(v)
            if err:
                return None, err
            items.append({"url": url})
        if items:
            body[key] = items
    return body, None


def _mask(value):
    if isinstance(value, str) and value.startswith("data:"):
        return f"{value[:32]}...<{len(value)} chars>"
    return value


# ----------------------------------------------------------------------------- queue
def _reason(raw):
    try:
        body = json.loads(raw)
    except (TypeError, ValueError):
        return None
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        return err.get("message") or err.get("code")
    if isinstance(err, str):
        return err
    return None


def video_submit(route, payload, key):
    """POST /v1/videos. Returns {ok, request_id, status_url, response_url} | {ok: False, ...}."""
    problem = _check(payload)
    if problem:
        return {"ok": False, "error": problem}
    body, err = _build_body(payload, _hosted)
    if err:
        return {"ok": False, "error": err}
    base = api_url()
    sub, status, raw = supercmo_env._request("POST", f"{base}/v1/videos", body=body, headers=_headers(key))
    if sub is None:
        return {"ok": False, "error": _reason(raw) or f"submit failed ({status})",
                "status": status, "detail": (raw or "")[:500]}
    vid = sub.get("id")
    if not vid:
        return {"ok": False, "error": "Blaze: no job id in the response", "detail": json.dumps(sub)[:500]}
    supercmo_env.dbg(f"blaze submit -> {vid} ({sub.get('status')})")
    return {"ok": True, "request_id": vid, "status_url": f"{base}/v1/videos/{vid}",
            "response_url": f"{base}/v1/videos/{vid}/content"}


def _download(response_url, key):
    data, ctype, status, err = supercmo_env._request_raw(
        "GET", response_url, headers={"Authorization": f"Bearer {key}"},
        timeout=_CONTENT_TIMEOUT, retries=2)
    if data is None:
        kind = "transient" if supercmo_env.retryable_status(status) else "terminal"
        return {"ok": False, kind: True, "error": _reason(err) or f"content download failed ({status})",
                "status": status, "detail": (err or "")[:500]}
    return {"ok": True, "data": data, "content_type": (ctype or "video/mp4").split(";")[0]}


def video_status(status_url, response_url, key):
    """One check. Pending → {ok, done: False, state}; succeeded → {ok, done, video:{b64,…}, duration};
    failed → {ok: False, terminal: True, ...}; network hiccup → {ok: False, transient: True, ...}."""
    st, code, raw = supercmo_env._request("GET", status_url, headers=_headers(key),
                                          timeout=_STATUS_POLL_TIMEOUT, retries=1)
    if st is None:
        kind = "transient" if supercmo_env.retryable_status(code) else "terminal"
        return {"ok": False, kind: True, "error": _reason(raw) or f"status check failed ({code})",
                "status": code, "detail": (raw or "")[:500]}
    state = (st.get("status") or "").lower()
    if state in _PENDING_STATES:
        return {"ok": True, "done": False, "state": state}
    if state == "succeeded" or state == "completed":
        dl = _download(response_url, key)
        if not dl.get("ok"):
            return dl
        video = {"b64": base64.b64encode(dl["data"]).decode("ascii"), "content_type": dl["content_type"],
                 "url": response_url, "file_size": len(dl["data"])}
        seconds = st.get("generated_seconds") or st.get("seconds")
        return {"ok": True, "done": True, "video": video, "duration": seconds}
    if state in _FAILED_STATES:
        err = st.get("error")
        msg = st.get("error_message") or (err.get("message") if isinstance(err, dict) else err)
        return {"ok": False, "terminal": True, "error": f"Blaze job {state}: {msg or st.get('error_code') or 'no reason given'}",
                "detail": json.dumps(st)[:500]}
    return {"ok": True, "done": False, "state": state or "unknown"}


def video_generate(route, payload, key):
    """Submit + blocking poll. Returns {ok, model, video, duration} | {ok: False, ...}."""
    sub = video_submit(route, payload, key)
    if not sub.get("ok"):
        return sub
    for _ in range(_MAX_POLLS):
        st = video_status(sub["status_url"], sub["response_url"], key)
        if st.get("ok") and st.get("done"):
            return {"ok": True, "model": payload.get("model"), "video": st["video"],
                    "duration": st.get("duration") or payload.get("duration")}
        if not st.get("ok") and not st.get("transient"):
            return st
        time.sleep(_POLL_INTERVAL)
    return {"ok": False, "error": "Blaze job timed out", "job_id": sub["request_id"]}


def video_request_spec(route, payload):
    body, err = _build_body(payload, lambda v: (_mask(v), None))
    spec = {"method": "POST", "url": f"{api_url()}/v1/videos",
            "headers": {"Authorization": "Bearer ***", "Content-Type": "application/json"}, "body": body}
    problem = _check(payload) or err
    if problem:
        spec["error"] = problem
    return spec


if __name__ == "__main__":
    os.environ.pop("BLAZE_ENHANCE_PROMPT", None)
    spec = video_request_spec({}, {"prompt": "a goat tugs a letter", "start_frame_image": "https://x/a.jpg",
                                   "reference_images": ["https://x/b.jpg"], "end_frame_image": "https://x/c.jpg",
                                   "aspect_ratio": "9:16", "duration": 20})
    b = spec["body"]
    assert b["model"] == MODEL_ID and b["mode"] == "reference_to_video" and b["enhance_prompt"] is True, b
    assert b["size"] == "768x1344" and b["seconds"] == 15, b
    assert [r["type"] for r in b["reference_images"]] == ["scene", "identity", "end"], b
    assert "error" not in spec, spec
    bad = video_request_spec({}, {"prompt": "text only"})
    assert "at least one" in bad["error"], bad
    os.environ["BLAZE_ENHANCE_PROMPT"] = "false"
    assert video_request_spec({}, {"prompt": "x", "reference_images": ["https://x/b.jpg"]})["body"]["enhance_prompt"] is False
    os.environ.pop("BLAZE_ENHANCE_PROMPT")
    assert _hosted("data:image/png;base64,AAAA") == ("data:image/png;base64,AAAA", None)
    assert _hosted("http://x/a.jpg")[1], "plain http must be refused"

    _real = supercmo_env._request, supercmo_env._request_raw
    script = []
    supercmo_env._request = lambda *a, **k: script.pop(0)
    supercmo_env._request_raw = lambda *a, **k: script.pop(0)
    try:
        script[:] = [({"id": "video_1", "status": "queued"}, 200, None)]
        sub = video_submit({}, {"prompt": "x", "reference_images": ["https://x/b.jpg"]}, "k")
        assert sub["ok"] and sub["status_url"].endswith("/v1/videos/video_1"), sub
        script[:] = [({"status": "in_progress"}, 200, None)]
        assert video_status(sub["status_url"], sub["response_url"], "k")["done"] is False
        script[:] = [({"status": "succeeded", "seconds": 6}, 200, None), (b"MP4", "video/mp4", 200, None)]
        st = video_status(sub["status_url"], sub["response_url"], "k")
        assert st["done"] and base64.b64decode(st["video"]["b64"]) == b"MP4" and st["duration"] == 6, st
        script[:] = [({"status": "failed", "error_message": "bad picture"}, 200, None)]
        st = video_status(sub["status_url"], sub["response_url"], "k")
        assert st["terminal"] and "bad picture" in st["error"], st
        script[:] = [(None, 400, '{"error":{"message":"seconds must be between 4 and 15"}}')]
        st = video_submit({}, {"prompt": "x", "reference_images": ["https://x/b.jpg"]}, "k")
        assert not st["ok"] and "seconds" in st["error"], st
    finally:
        supercmo_env._request, supercmo_env._request_raw = _real
    print("blaze OK:", spec["url"], b["size"], [r["type"] for r in b["reference_images"]])
