"""Optional AI video clips for MoneyPrinterTurbo scenes via the n8n 'MPT HunyuanVideo Clip Generator' workflow.

Copy to app/services/gpu_clips.py. Everything here is best-effort: any error, timeout or "fallback" answer returns {}
and the caller keeps its Ken Burns stills, so a dead n8n server, an empty Kaggle quota or a bad token never breaks a video.

Environment (.env on the MPT server):
  VIDEO_CLIP_BACKEND_URL   https://<n8n-host>/webhook/mpt-clips      (empty = feature off)
  VIDEO_CLIP_TOKEN         value of the n8n header credential "MoneyPrinter Upload Token"
  VIDEO_CLIP_HEADER        header name of that credential (default X-MPT-Token; must match the n8n credential)
  VIDEO_CLIP_MODEL         hy15-i2v | wan21-diffusers | ltx-t2v | wan22-5b-diffusers | hy15-t2v   (default wan21-diffusers)
  VIDEO_CLIP_WAIT_MIN      how long one render waits for the Kaggle batch (default 100; 0 = off)
  VIDEO_CLIP_POLL_SEC      poll interval (default 60)
"""
from __future__ import annotations

import base64
import io
import os
import time

import requests
from loguru import logger

I2V_MODELS = {"hy15-i2v"}


def _cfg():
    e = os.environ.get
    return {
        "url": (e("VIDEO_CLIP_BACKEND_URL") or "").rstrip("/"),
        "token": e("VIDEO_CLIP_TOKEN") or "",
        "header": e("VIDEO_CLIP_HEADER") or "X-MPT-Token",
        "model": e("VIDEO_CLIP_MODEL") or "wan21-diffusers",
        "wait": float(e("VIDEO_CLIP_WAIT_MIN") or 100),
        "poll": max(15.0, float(e("VIDEO_CLIP_POLL_SEC") or 60)),
    }


def enabled() -> bool:
    c = _cfg()
    return bool(c["url"] and c["token"] and c["wait"] > 0)


def _jpeg_b64(path: str, max_side: int = 832) -> str:
    from PIL import Image
    img = Image.open(path).convert("RGB")
    img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()


def _post(c, path, payload, timeout=60, stream=False):
    r = requests.post(f"{c['url']}/{path}", json=payload, headers={c["header"]: c["token"]}, timeout=timeout, stream=stream)
    r.raise_for_status()
    return r


def fetch_clips(task_id: str, scenes, work_dir: str, width: int = 480, height: int = 832, topic: str = "") -> dict[int, str]:
    """Submit one job for all scenes, wait up to VIDEO_CLIP_WAIT_MIN, download clips. Returns {scene_index: mp4_path}."""
    c = _cfg()
    if not enabled():
        return {}
    job = None
    try:
        items = []
        for i, s in enumerate(scenes):
            item = {"idx": i, "narration": getattr(s, "text", "")[:600], "prompt": (getattr(s, "prompt", "") or topic)[:1500]}
            if c["model"] in I2V_MODELS:
                if not getattr(s, "image_path", ""):
                    return {}
                item["image_b64"] = _jpeg_b64(s.image_path)
            items.append(item)
        ref = "".join(ch for ch in str(task_id) if ch.isalnum())[:24] or "short"
        job = _post(c, "submit", {"ref": ref, "topic": topic[:200], "video_model": c["model"], "width": width, "height": height,
                                  "deadline_min": int(c["wait"]) + 30, "scenes": items}).json()
        if job.get("status") != "queued":
            logger.info(f"gpu clips: not queued ({job.get('status')}: {job.get('error')}), using stills")
            return {}
        logger.info(f"gpu clips: queued {job['job_id']} ({len(items)} scenes, {c['model']})")
        stop = time.time() + c["wait"] * 60
        st = {}
        while time.time() < stop:
            time.sleep(c["poll"])
            try:
                st = _post(c, "status", {"job_id": job["job_id"], "job_file_id": job["job_file_id"]}).json()
            except Exception as e:  # n8n blip: keep waiting until the deadline
                logger.warning(f"gpu clips: status error {str(e)[:120]}")
                continue
            if st.get("status") in ("done", "fallback", "error"):
                break
        if st.get("status") != "done":
            try:
                _post(c, "status", {"job_id": job["job_id"], "job_file_id": job["job_file_id"], "cancel": True})
            except Exception:
                pass
            logger.info(f"gpu clips: {st.get('status', 'timeout')} ({st.get('reason')}), using stills")
            return {}
        out: dict[int, str] = {}
        os.makedirs(work_dir, exist_ok=True)
        for clip in st.get("clips", []):
            try:
                idx = int(str(clip["name"]).split("_")[1].split(".")[0])
                path = os.path.join(work_dir, f"clip-{idx:02d}.mp4")
                with _post(c, "clip", {"job_id": job["job_id"], "file_id": clip["file_id"]}, timeout=300, stream=True) as r:
                    with open(path, "wb") as f:
                        for chunk in r.iter_content(1 << 16):
                            f.write(chunk)
                if os.path.getsize(path) > 10_000:
                    out[idx] = path
            except Exception as e:
                logger.warning(f"gpu clips: download failed for {clip}: {str(e)[:120]}")
        logger.info(f"gpu clips: got {len(out)}/{len(items)} clips")
        return out
    except Exception as e:
        logger.warning(f"gpu clips: {str(e)[:200]}; using stills")
        return {}
    finally:
        if job and job.get("job_id"):
            try:  # no lingering data: move this job's Drive files to the bin once we have them locally
                _post(c, "cleanup", {"job_id": job["job_id"]}, timeout=60)
            except Exception:
                pass
