# MoneyPrinter video app: free AI clips from HunyuanVideo / Wan / LTX on Kaggle

This folder turns MoneyPrinter's still pictures into short AI video clips, using free Kaggle GPUs. It runs entirely on
the owner's own accounts: the Oracle MPT server, the n8n server, Kaggle, Google Drive, and GitHub. **No assistant is needed
at runtime.** If any part is down, MoneyPrinter falls back to its FLUX pictures with Ken Burns motion and still delivers a video.

> For day-to-day use, read the operator guide: [`../docs/VIDEO_APP_GUIDE.md`](../docs/VIDEO_APP_GUIDE.md).

## How it works

```
MoneyPrinter autopilot (Oracle, 2 Shorts/day)
  ├─ makes script, Gemini voice, FLUX scene pictures (as today)
  ├─ POST /webhook/mpt-clips/submit  ──►  n8n "MPT HunyuanVideo Clip Generator"
  │                                        writes  mpt_<id>.json  into the shared Drive job queue
  │                                                     ▲
  │        n8n "GPU Router" (every 20 min, YouTube Auto) │ sees pending jobs → kaggle_launch.py ensure
  │                                                     │  (Kaggle API, private BATCH kernel, 2×T4, no tunnel)
  │        Kaggle kernel ── pulls jobs via "GPU Bridge" webhooks ── renders scene_XXX.mp4 ── uploads to Drive ── exits
  ├─ polls /mpt-clips/status every 60 s, for up to VIDEO_CLIP_WAIT_MIN (default 100)
  │     done      → downloads clips (/mpt-clips/clip) → uses them instead of Ken Burns → /mpt-clips/cleanup (Drive bin)
  │     fallback  → (expired, GPU failed, switched off) job is cancelled → FLUX + Ken Burns as today
  └─ delivers to Telegram as today
```

One Kaggle account and one launcher, shared with YouTube Auto. MoneyPrinter never starts Kaggle itself, so two kernels
can never compete for the 30-hour quota, and nothing serves traffic from Kaggle.

## Files

| Path | What it is |
|---|---|
| `gpu_worker/` | The complete Kaggle GPU worker (YouTube Auto's code, plus the HunyuanVideo-1.5 patch). Deploy it to `/opt/ytauto/scripts/gpu/` on the n8n server. |
| `gpu_worker/hunyuan15_t4.patch` | The same change as a diff against YouTube Auto's original files (t4_models, worker, kernel, launcher). |
| `gpu.env.example` | Template for the worker's secrets file. The real file is never committed. |
| `notebooks/hunyuan15_t4_benchmark.ipynb` | A one-off notebook you run by hand on Kaggle to time HunyuanVideo-1.5 on a T4. No tunnel. |
| `n8n/n8n-hunyuan-clip.json` | Export of "MPT HunyuanVideo Clip Generator" (webhooks submit, status, clip, cleanup). |
| `n8n/n8n-mpt-video-control.json` | Export of "MPT Video App Control" (password form: status/quota, run batch now, selftest, test clip). |
| `n8n/gen_*.py`, `*.sdk.ts` | Sources these workflows were built from. |
| `n8n/ROUTER_PATCH.md` | A small proposed edit to YouTube Auto's Router, so MoneyPrinter jobs never take the CPU fallback. |
| `mpt_patch/gpu_clips.py` | MoneyPrinter client. Copy it to `app/services/`. |
| `mpt_patch/gemini_media_clips.patch` | Makes the renderer use a clip for a scene when one exists. Tested with ffmpeg. |
| `mpt_patch/autopilot_timeout.patch` | `AUTO_RENDER_TIMEOUT_MIN`, so autopilot waits longer than 60 minutes when clips are on. |
| `mpt_patch/test_gpu_clips.py` | Offline test with a fake n8n. Run it with `python hunyuan/mpt_patch/test_gpu_clips.py`. |
| `mpt_patch/INTEGRATION.md` | How MoneyPrinter calls the video app: env flags, timeouts, fallback. |

The n8n exports have the Drive folder IDs and credential IDs replaced with placeholders, because this repo is public.
After importing, pick the credentials again and set `LONGCAT_JOBS_FOLDER_ID` and `LONGCAT_OUTPUTS_FOLDER_ID`.

## Verified facts (checked 10 Nov 2026)

- **hunyuanvideo.org is not Tencent.** The site says it "is independent and is not the official Tencent Hunyuan website".
  Look-alike sites (for example hy-video.org/pricing) sell credit plans ($17.90 to $59.90 a month). Tencent's own cloud API is paid:
  1.5 credits for a 5 s 720p clip, prepaid at ¥1 a credit, with a small one-time free pack.
  Sources: https://www.hunyuanvideo.org/, https://cloud.tencent.com/document/buy-guide/1616/118994
- **The free part is the open model.** HunyuanVideo-1.5 (8.3B parameters) is on https://github.com/Tencent-Hunyuan/HunyuanVideo-1.5.
  Tencent states a minimum of **14 GB GPU memory with offloading**. A 480p image-to-video *step-distilled* model needs 8–12 steps
  (about 75 s on an RTX 4090). Diffusers support (`HunyuanVideo15Pipeline`, `HunyuanVideo15ImageToVideoPipeline`) and ComfyUI support
  (https://huggingface.co/Comfy-Org/Hunyuan-Video-1.5, fp8 files 8.3 GB) are both official.
  Community GGUF builds are 4.9–9 GB, for example https://huggingface.co/jayn7/HunyuanVideo-1.5_I2V_480p-GGUF.
  The Qwen2.5-VL-7B text encoder adds about 14–16 GB. The full download is **about 35 GB**.
- **Licence:** Tencent Hunyuan Community License. It is not valid in the EU, UK, or South Korea, and it needs a separate licence above 100M monthly users.
  Nigeria is fine. Wan2.1/2.2 are Apache-2.0. LTX-Video uses its own open-weights licence.
- **Oracle Always Free has no GPU.** It covers Ampere A1 Arm compute only, now 2 OCPU and 12 GB in total (cut from 4/24 GB in 2026).
  https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm
- **Kaggle:** GPU T4 ×2 (2 × 16 GB, separate cards; the P100 was retired on 15 Sep 2026), 4 CPU cores, about 29 GB RAM, about 30 GPU hours a week
  ("or sometimes higher"), and 12-hour sessions. It needs a phone-verified account. https://www.kaggle.com/docs/efficient-gpu-usage
- **Kaggle terms:** "only ... for your own internal, personal, non-commercial use" (https://www.kaggle.com/terms). The AUP bans "server farming",
  "tools for circumvention/obfuscation" and "activity unrelated to ML data science" (https://www.kaggle.com/aup). People report bans for
  SSH/ngrok tunnels. **So: no tunnel and no always-on endpoint.** That's why this design uses a short private batch that pulls jobs and exits.
  A monetised channel could still count as commercial use. Treat Kaggle as a free bonus that can disappear, not as the business's foundation.
- **No free Kaggle/Colab notebook for HunyuanVideo-1.5 was found from Tencent.** The official repo lists ComfyUI, diffusers and Wan2GP
  (which claims as low as 6 GB VRAM). Our notebook is our own.

## Honest verdict on "unlimited"

It isn't unlimited. It is **free and capped**: about 30 GPU hours a week, shared with YouTube Auto, at older T4 speed, with possible
queues and changing terms. Estimated output inside that quota (estimates, not yet measured; run the benchmark notebook to confirm):

| Model (`video_model`) | Clip | Est. time per clip on one T4 | GPUs used | Est. clips/week (≈29 usable h) |
|---|---|---|---|---|
| `ltx-t2v` LTX-Video 0.9.5 2B | ~4 s, 24 fps | 1.5–3 min | 2 | ~1,000–2,000 |
| `wan21-diffusers` Wan2.1 T2V 1.3B | ~3 s, 16 fps, 480×832 | 6–10 min | 2 | ~350–550 |
| `hy15-i2v` HunyuanVideo-1.5 480p I2V step-distilled, NF4, 8 steps | ~2.5 s, 24 fps | 4–8 min (untested) | 1 (RAM) | ~200–400 |
| `wan22-5b-diffusers` Wan2.2 TI2V 5B | ~3 s, 24 fps | 12–20 min | 1–2 | ~90–250 |
| `hy15-t2v` HunyuanVideo-1.5 480p T2V CFG-distilled, 30 steps | ~2.5 s | 15–25 min (untested) | 1 | ~70–110 |

How these were estimated: the official speeds (Wan2.1-1.3B takes about 4 min for 5 s 480p on a 4090, and HunyuanVideo-1.5 step-distilled about 75 s on a 4090)
were scaled to shorter clips, with a T4 taken as roughly 5–8× slower than a 4090. Each session costs about 0.3–0.5 h for install and download.
MoneyPrinter needs about **8 clips × 14 Shorts = ~112 clips a week**, which fits easily with Wan2.1 or LTX and probably with HunyuanVideo i2v,
but every hour MoneyPrinter uses is an hour YouTube Auto can't use.

## Recommendation

1. Start with **`wan21-diffusers`**. It's the proven path in the existing worker, Apache-2.0, and uses both T4s.
2. Run the **HunyuanVideo selftest** once (from the control form, or the notebook). If one clip takes 8 minutes or less and doesn't come out black, switch MoneyPrinter to
   **`hy15-i2v`** (`VIDEO_CLIP_MODEL=hy15-i2v`). It animates MoneyPrinter's own FLUX picture for each scene, so the look stays consistent.
3. Keep the FLUX + Ken Burns fallback forever. Optionally switch on Google Imagen billing ($300 new-account credit) for sharper stills.
