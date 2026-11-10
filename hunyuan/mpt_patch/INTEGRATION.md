# How MoneyPrinterTurbo calls the video app (prepared, NOT deployed)

## Code
1. Copy `gpu_clips.py` to `app/services/gpu_clips.py`.
2. `git apply hunyuan/mpt_patch/gemini_media_clips.patch` adds `Scene.clip_path`. `build_ai_visuals()` asks the video app for clips
   after the FLUX pictures are ready. `render_scenes()` loops or trims a clip to the scene's narration length, and uses the Ken Burns
   still for any scene without a clip.
3. `git apply hunyuan/mpt_patch/autopilot_timeout.patch` replaces autopilot's fixed 60-minute render wait with `AUTO_RENDER_TIMEOUT_MIN`.
4. Test offline: `python hunyuan/mpt_patch/test_gpu_clips.py` should print `GPU_CLIPS TEST OK`.

## `.env` on the MPT server (`/opt/moneyprinterturbo/.env`, mode 600)
```
VIDEO_CLIP_BACKEND_URL=https://<n8n-host>/webhook/mpt-clips   # empty = feature off (today's behaviour)
VIDEO_CLIP_TOKEN=<value of n8n credential "MoneyPrinter Upload Token">
VIDEO_CLIP_HEADER=<header name of that credential>             # look it up in n8n → Credentials
VIDEO_CLIP_MODEL=wan21-diffusers                               # later: hy15-i2v
VIDEO_CLIP_WAIT_MIN=100
VIDEO_CLIP_POLL_SEC=60
AUTO_RENDER_TIMEOUT_MIN=150
AUTO_SCHEDULE_TIMES=07:00,16:00                                # start ~2 h earlier, so delivery still lands around 09:00/18:00
```

## Behaviour and fallback
- If `VIDEO_CLIP_BACKEND_URL` is empty or `VIDEO_CLIP_WAIT_MIN=0`, nothing changes.
- **Submit** sends one job per video: up to 12 scenes, each with its prompt and, for `hy15-i2v`, a ≤832 px JPEG of the FLUX picture.
  n8n answers `queued` (with `job_id` and `job_file_id`), or `fallback` when `MPT_GPU_CLIPS=off` is set in n8n.
- **Status** is polled every 60 s. The answer is `queued`, `rendering`, `done` (with a clip list), or `fallback`. Fallback happens when the job expired
  (`deadline_min` = wait + 30), failed on the GPU twice, or was cancelled. On fallback or timeout MoneyPrinter sends `cancel:true`, so no worker renders the job later.
- **Clip** streams each mp4 from Drive. n8n only serves files named `<job_id>.scene_XXX.mp4` for `mpt_*` jobs.
- **Cleanup** moves the job file, the manifest, and the clips to the Drive bin (recoverable for 30 days), so no data lingers.
- Any error at any step means MoneyPrinter uses the stills for that video. Partial results also work: scenes without a clip use stills.
- Manual Telegram renders go through the same code. Set `VIDEO_CLIP_WAIT_MIN` lower, or leave the feature off, if you want instant manual videos.
  An optional later step is a `/clips` Telegram command that shows `kaggle_launch.py quota`, but the n8n control form already covers this.
