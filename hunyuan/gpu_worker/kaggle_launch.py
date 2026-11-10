#!/usr/bin/env python3
"""Start the free Kaggle GPU worker headlessly (no browser) through the Kaggle public API, and report its status.

  python kaggle_launch.py quota                         -> GPU hours used / allowed this week
  python kaggle_launch.py status --mode app|drive       -> queued | running | complete | error | none
  python kaggle_launch.py build  --mode app|drive -o kernel_dir   (writes kernel-metadata.json + script, for `kaggle kernels push -p`)
  python kaggle_launch.py ensure --mode app|drive       -> push a new run unless one is already queued/running
  python kaggle_launch.py ensure --mode selftest        -> one 9:16 test clip on Kaggle (no app/n8n needed)
  python kaggle_launch.py fetch  --mode selftest -o dir -> download that run's output files (test_clip_9x16.mp4)
Every command prints ONE line of JSON (n8n / the app router parse it) and exits 0; check "ok".

Credentials (never hard-code them): KAGGLE_USERNAME + KAGGLE_KEY (legacy kaggle.json key, HTTP Basic) or
KAGGLE_USERNAME + KAGGLE_API_TOKEN (new token from kaggle.com/settings, Bearer). They can also live in a KEY=VALUE file
named by GPU_CONFIG_FILE (default: secrets/gpu.env next to this script).
Mode app:   the kernel pulls clips from the app backend:  YTA_SERVER_URL + YTA_WORKER_TOKEN
Mode drive: the kernel pulls n8n Drive jobs via the GPU Bridge: GPU_BRIDGE_PUBLIC_URL + GPU_BRIDGE_TOKEN
Endpoints match kaggle CLI 2.x / kagglesdk (POST https://api.kaggle.com/v1/kernels.KernelsApiService/<Method>).
Accelerator "NvidiaTeslaT4" = "GPU T4 x2" (the CLI's own help text); KAGGLE_API_BASE overrides the host (tests).
"""
import argparse, base64, io, json, os, sys, tarfile, time
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
WORKER_DIR = os.path.dirname(HERE)
FILES = {"app": [os.path.join(WORKER_DIR, f) for f in ("worker.py", "camera.py", "t4_models.py", "fallback_media.py")],
         "selftest": [os.path.join(WORKER_DIR, f) for f in ("worker.py", "camera.py", "t4_models.py", "fallback_media.py")],
         "drive": [os.path.join(WORKER_DIR, f) for f in ("worker.py", "camera.py", "t4_models.py", "fallback_media.py")]
                  + [os.path.join(HERE, "yta_gpu_worker.py")]}


def load_env_file():
    path = os.environ.get("GPU_CONFIG_FILE") or os.path.join(HERE, "secrets", "gpu.env")
    try:
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass


def E(k, d=""): return os.environ.get(k, d)


def slug_for(mode): return E("KAGGLE_KERNEL_SLUG_" + mode.upper(), f"yta-gpu-worker-{mode}")


class Kaggle:
    def __init__(self):
        self.user = E("KAGGLE_USERNAME")
        self.base = E("KAGGLE_API_BASE", "https://api.kaggle.com").rstrip("/")
        self.s = requests.Session()
        self.s.headers.update({"Content-Type": "application/json", "User-Agent": "yta-kaggle-launch/1.0"})
        tok = E("KAGGLE_API_TOKEN") or (E("KAGGLE_KEY") if E("KAGGLE_KEY").startswith("KGAT_") else "")
        if tok: self.s.headers["Authorization"] = "Bearer " + tok   # new-style token (also accepted in KAGGLE_KEY)
        elif E("KAGGLE_KEY"): self.s.auth = (self.user, E("KAGGLE_KEY"))   # legacy kaggle.json key
        self.ok = bool(self.user and (E("KAGGLE_API_TOKEN") or E("KAGGLE_KEY")))

    def call(self, method, body):
        r = self.s.post(f"{self.base}/v1/kernels.KernelsApiService/{method}", json=body, timeout=60)
        if r.status_code == 404: return None
        if r.status_code >= 400: raise RuntimeError(f"Kaggle {method} HTTP {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else {}

    def quota(self):
        d = self.call("GetAcceleratorQuotaStatistics", {}) or {}
        g = d.get("gpuQuota") or {}
        h = lambda v: round(float(str(v or "0s").rstrip("s") or 0) / 3600, 2)
        used, res, total = h(g.get("timeUsed")), h(g.get("timeReserved")), h(g.get("totalTimeAllowed"))
        return {"used_h": used, "reserved_h": res, "total_h": total, "remaining_h": round(total - used - res, 2),
                "refresh": d.get("quotaRefreshTime")}

    def status(self, slug):
        d = self.call("GetKernelSessionStatus", {"userName": self.user, "kernelSlug": slug})
        if d is None: return {"state": "none"}
        raw = str(d.get("status", "")).upper()
        for k in ("CANCEL", "QUEUED", "RUNNING", "COMPLETE", "ERROR", "NEW_SCRIPT"):
            if k in raw: return {"state": {"CANCEL": "cancelled", "NEW_SCRIPT": "queued"}.get(k, k.lower()), "failure": d.get("failureMessage")}
        return {"state": raw.lower() or "unknown", "failure": d.get("failureMessage")}

    def outputs(self, slug):
        d = self.call("ListKernelSessionOutput", {"userName": self.user, "kernelSlug": slug, "pageSize": 50}) or {}
        return [{"name": f.get("fileName"), "url": f.get("url")} for f in d.get("files", [])]

    def push(self, slug, title, text):
        body = {"slug": f"{self.user}/{slug}", "newTitle": title, "text": text, "language": "python", "kernelType": "script",
                "isPrivate": True, "enableGpu": True, "enableInternet": True, "machineShape": E("KAGGLE_ACCELERATOR", "NvidiaTeslaT4"),
                "datasetDataSources": [], "kernelDataSources": [], "competitionDataSources": [], "categoryIds": [], "modelDataSources": []}
        d = self.call("SaveKernel", body) or {}
        if d.get("error"): raise RuntimeError("Kaggle SaveKernel: " + str(d["error"])[:300])
        return {"url": d.get("url"), "version": d.get("versionNumber"), "ref": d.get("ref")}


def build_script(mode):
    if mode == "selftest":
        cfg = {"mode": "selftest", "token": "", "prompt": E("KAGGLE_TEST_PROMPT")}
    elif mode == "app":
        server, token = E("YTA_SERVER_URL"), E("YTA_WORKER_TOKEN") or E("WORKER_TOKEN")
        if not server or not token: raise RuntimeError("YTA_SERVER_URL and YTA_WORKER_TOKEN are needed for mode app")
        if ("localhost" in server or "127.0.0.1" in server) and E("KAGGLE_SIMULATE") != "1":
            raise RuntimeError("YTA_SERVER_URL must be a public https URL the Kaggle session can reach")
        cfg = {"mode": "app", "server": server.rstrip("/"), "token": token}
    else:
        bridge, token = E("GPU_BRIDGE_PUBLIC_URL"), E("GPU_BRIDGE_TOKEN")
        if not bridge or not token: raise RuntimeError("GPU_BRIDGE_PUBLIC_URL and GPU_BRIDGE_TOKEN are needed for mode drive")
        cfg = {"mode": "drive", "bridge": bridge.rstrip("/"), "token": token}
    cfg.update({"video_model": E("KAGGLE_VIDEO_MODEL", "wan21-diffusers"), "i2v_model": E("KAGGLE_I2V_MODEL", "ltx"),
                "image_model": E("KAGGLE_IMAGE_MODEL", "flux-schnell"), "avatar_model": E("KAGGLE_AVATAR_MODEL", "fallback"),
                "parallel": int(E("KAGGLE_PARALLEL", "2")), "max_hours": float(E("KAGGLE_MAX_HOURS", "11")),
                "second_gpu_delay_min": float(E("KAGGLE_SECOND_GPU_DELAY_MIN", "12")),
                "min_free_ram_gb": float(E("KAGGLE_MIN_FREE_RAM_GB", "14")),
                "simulate": E("KAGGLE_SIMULATE") == "1", "gpus": int(E("KAGGLE_SIM_GPUS", "2"))})
    if E("KAGGLE_SIM_WORKDIR"): cfg["workdir"] = E("KAGGLE_SIM_WORKDIR")
    # model tuning knobs travel to the Kaggle session (t4_models.py reads them there); no secrets match these prefixes
    cfg["env"] = {k: v for k, v in os.environ.items() if k.startswith(("HY15_", "WAN_", "LTX_"))}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for f in FILES[mode]: tf.add(f, arcname=os.path.basename(f))
    tpl = open(os.path.join(HERE, "kernel_main.py")).read()
    return (tpl.replace("__CONFIG_B64__", base64.b64encode(json.dumps(cfg).encode()).decode())
               .replace("__PAYLOAD_B64__", base64.b64encode(buf.getvalue()).decode()))


def main():
    load_env_file()
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["quota", "status", "build", "ensure", "fetch"])
    ap.add_argument("--mode", default="drive", choices=["app", "drive", "selftest"])
    ap.add_argument("-o", "--out", default="")
    ap.add_argument("--min-quota-h", type=float, default=float(E("KAGGLE_MIN_QUOTA_H", "1.0")))
    a = ap.parse_args()
    out = {"ok": False, "cmd": a.cmd, "mode": a.mode, "ts": int(time.time())}
    try:
        if a.cmd == "build":
            slug = slug_for(a.mode); d = a.out or os.path.join(HERE, "build", a.mode); os.makedirs(d, exist_ok=True)
            open(os.path.join(d, "yta_worker.py"), "w").write(build_script(a.mode))
            json.dump({"id": f"{E('KAGGLE_USERNAME', 'YOUR_KAGGLE_USERNAME')}/{slug}", "title": slug, "code_file": "yta_worker.py",
                       "language": "python", "kernel_type": "script", "is_private": True, "enable_gpu": True, "enable_tpu": False,
                       "enable_internet": True, "machine_shape": "NvidiaTeslaT4", "dataset_sources": [], "competition_sources": [],
                       "kernel_sources": [], "model_sources": [], "keywords": []}, open(os.path.join(d, "kernel-metadata.json"), "w"), indent=2)
            out.update(ok=True, dir=d); return
        k = Kaggle()
        if not k.ok: out.update(reason="no_credentials", hint="set KAGGLE_USERNAME + KAGGLE_KEY (or KAGGLE_API_TOKEN)"); return
        if a.cmd == "quota": out.update(ok=True, **k.quota()); return
        st = k.status(slug_for(a.mode))
        if a.cmd == "status": out.update(ok=True, **st); return
        if a.cmd == "fetch":
            files = k.outputs(slug_for(a.mode)); d = a.out or os.path.join(HERE, "output", a.mode); os.makedirs(d, exist_ok=True)
            got = []
            for f in files:
                if not f["url"] or not f["name"]: continue
                r = requests.get(f["url"], timeout=300); r.raise_for_status()
                p = os.path.join(d, os.path.basename(f["name"])); open(p, "wb").write(r.content); got.append(p)
            out.update(ok=bool(got), state=st["state"], files=got, reason=None if got else "no output yet"); return
        if st["state"] in ("queued", "running"): out.update(ok=True, state=st["state"], launched=False); return
        try:
            q = k.quota(); out["quota"] = q
            if q["total_h"] and q["remaining_h"] < a.min_quota_h:
                out.update(reason="quota_exhausted", refresh=q.get("refresh")); return
        except Exception as e:
            out["quota_error"] = str(e)[:200]                  # quota endpoint is optional; the push itself is the real test
        res = k.push(slug_for(a.mode), slug_for(a.mode), build_script(a.mode))
        out.update(ok=True, state="launched", launched=True, previous=st["state"], **res)
    except Exception as e:
        msg = str(e)
        out.update(reason=("quota_exhausted" if "quota" in msg.lower() else "auth_failed" if " 401" in msg or " 403" in msg else "error"),
                   error=msg[:400])
    finally:
        print(json.dumps(out), flush=True)


if __name__ == "__main__": main()
