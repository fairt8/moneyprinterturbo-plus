"""Drive-queue worker for the n8n pipeline (workflows A / S1 write job JSONs to Drive `longcat_jobs`).

It never touches Google Drive itself: it talks only to the n8n "GPU Bridge" webhooks (outbound HTTPS, pull model, no tunnel):
  POST {bridge}/claim  {worker, backend}                         -> {job, job_file_id} | {job: null}
  POST {bridge}/clip   multipart file + job_id + name            -> {file_id}     (uploaded to Drive longcat_outputs)
  POST {bridge}/done   {job_id, job_file_id, clips, backend}     -> {ok, manifest_id}  (writes <job_id>.done.json for B / S2)
  POST {bridge}/fail   {job_id, job_file_id, error, worker}      -> {ok}
Header: X-Bridge-Token: <GPU_BRIDGE_TOKEN>

Backends:
  --backend gpu  --model wan21-diffusers|ltx-t2v|wan22-5b-diffusers   (Kaggle / Colab T4; see t4_models.py)
  --backend cpu                                                         (no GPU: HF Space / stock / AI image + Ken Burns)
  --backend mock                                                        (tests: ffmpeg test pattern)
Exits when the queue is empty (saves Kaggle quota) or when --deadline-min is reached (Kaggle sessions stop at 12 h).
"""
import argparse, json, os, socket, sys, tempfile, time, traceback
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.dirname(HERE)]          # worker/ (camera.py, fallback_media.py, t4_models.py, worker.py)
import requests


def scene_seconds(sc):
    words = len(str(sc.get("narration") or "").split())
    return float(min(5.0, max(2.0, words / 2.6)))


def build(backend, model):
    if backend == "mock":
        from worker import mock_clip
        return mock_clip
    if backend == "cpu":
        import fallback_media
        return lambda c, out: fallback_media.scene_clip(c, out)
    import t4_models
    return t4_models.T2V[model]()


def load_env_file():
    """Same KEY=VALUE secrets file as kaggle_launch.py (GPU_CONFIG_FILE, default secrets/gpu.env next to this script)."""
    path = os.environ.get("GPU_CONFIG_FILE") or os.path.join(HERE, "secrets", "gpu.env")
    try:
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1); os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except (FileNotFoundError, PermissionError):
        pass


def main():
    load_env_file()
    E = os.environ.get
    ap = argparse.ArgumentParser()
    ap.add_argument("--bridge", default=E("GPU_BRIDGE_URL", "http://127.0.0.1:5678/webhook/gpu-bridge"))
    ap.add_argument("--token", default=E("GPU_BRIDGE_TOKEN", ""))
    ap.add_argument("--backend", default=E("GPU_BACKEND", "gpu"), choices=["gpu", "cpu", "mock"])
    ap.add_argument("--model", default=E("T2V_MODEL", "wan21-diffusers"), choices=["wan21-diffusers", "ltx-t2v", "wan22-5b-diffusers", "hy15-i2v", "hy15-t2v"])
    ap.add_argument("--name", default=E("WORKER_NAME", socket.gethostname()))
    ap.add_argument("--max-jobs", type=int, default=int(E("MAX_JOBS", 0) or 0), help="0 = until the queue is empty")
    ap.add_argument("--deadline-min", type=float, default=float(E("WORKER_DEADLINE_MIN", 0) or 0))
    ap.add_argument("--job", default="", help="only this job_id (CPU fallback for one stuck job)")
    a = ap.parse_args()
    if not a.token: sys.exit("GPU_BRIDGE_TOKEN missing")
    H = {"X-Bridge-Token": a.token}
    stop_at = time.time() + a.deadline_min * 60 if a.deadline_min else None
    gen, gen_model, done_jobs, name = None, None, 0, f"{a.name}-{a.backend}"

    def post(path, **kw):
        tmo = kw.pop("timeout", 120)
        for k in range(4):
            try:
                r = requests.post(f"{a.bridge}/{path}", headers=H, timeout=tmo, **kw)
                r.raise_for_status(); return r.json()
            except Exception as e:
                if k == 3: raise
                print(f"[bridge] {path} failed ({str(e)[:120]}), retrying"); time.sleep(5 * 2 ** k)

    print(f"[worker] {name} -> {a.bridge}", flush=True)
    while True:
        if stop_at and time.time() > stop_at: print("[worker] deadline reached"); break
        if a.max_jobs and done_jobs >= a.max_jobs: break
        got = post("claim", json={"worker": name, "backend": a.backend, "job_id": a.job})
        job = got.get("job")
        if not job: print("[worker] queue empty, exiting", flush=True); break
        jid, jfid = job["job_id"], got.get("job_file_id", "")
        try:
            # per-job model (MoneyPrinter jobs set job.video_model; YT Auto jobs keep the kernel default)
            want = job.get("video_model") if a.backend == "gpu" and job.get("video_model") else a.model
            if gen is None or gen_model != want:
                if gen is not None:
                    import gc; gen = None; gc.collect()
                    try:
                        import torch; torch.cuda.empty_cache()
                    except Exception: pass
                gen, gen_model = build(a.backend, want), want
            v = job.get("video") or {}
            W, H_ = int(v.get("width") or 480), int(v.get("height") or 832)
            clips, src = [], {}
            for sc in job.get("scenes", []):
                if stop_at and time.time() > stop_at: raise RuntimeError("deadline reached mid-job (released for the next worker)")
                i = int(sc.get("idx", len(clips))); nm = f"scene_{i:03d}.mp4"
                out = os.path.join(tempfile.gettempdir(), f"{jid}_{nm}")
                c = {"id": f"{jid}-{i}", "idx": i, "prompt": sc.get("prompt") or job.get("topic") or "abstract", "width": W, "height": H_,
                     "seconds": scene_seconds(sc), "attempts": 1, "image_b64": sc.get("image_b64") or ""}
                t0 = time.time(); used = gen(c, out)
                body = open(out, "rb").read()          # bytes, so a retried upload resends the whole file
                r = post("clip", data={"job_id": jid, "name": nm}, files={"file": (f"{jid}.{nm}", body, "video/mp4")}, timeout=600)
                os.remove(out)
                if not r.get("file_id"): raise RuntimeError(f"bridge returned no file_id for {nm}: {r}")
                clips.append({"name": nm, "file_id": r["file_id"]}); src[nm] = used if isinstance(used, str) else a.backend
                print(f"[worker] {jid} {nm} ({src[nm]}) in {time.time()-t0:.0f}s", flush=True)
            r = post("done", json={"job_id": jid, "job_file_id": jfid, "clips": clips, "backend": a.backend, "worker": name, "sources": src})
            print(f"[worker] READY {jid}: {len(clips)} clips, manifest {r.get('manifest_id')}", flush=True); done_jobs += 1
        except Exception as e:
            traceback.print_exc()
            try: post("fail", json={"job_id": jid, "job_file_id": jfid, "error": str(e)[:500], "worker": name, "backend": a.backend})
            except Exception: pass
            if a.job: break
    print(f"[worker] finished, {done_jobs} job(s)", flush=True)


if __name__ == "__main__": main()
