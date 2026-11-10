# MoneyPrinter YT — Project Guide

Generated 10 Nov 2026


One place for each project: what counts as "complete", where its code lives, and its shareable bot template.
Projects that aren't finished don't get a published template.

## MoneyPrinter YT (this repo)
Automated faceless Shorts: AI script, AI scene images, Gemini voice, captions, scheduled delivery.

**Projects, type and what each produces:**

| Project | Type | Produces |
|---|---|---|
| MoneyPrinterTurbo-plus (Telegram bot, Mini App, web studio, API, on-server autopilot) | App | 1. Finished 30–60 s vertical Shorts (mp4) with voiceover and captions. 2. A ready-to-paste title, description and hashtags for each video. 3. Delivery: to Telegram today; YouTube and TikTok uploads once connected. |
| HunyuanVideo / Wan Kaggle clip add-on (`hunyuan/`, n8n + Kaggle batch) | Workflow | 1. Short AI video clips (about 3 s) for each scene, used in place of still images. 2. Weekly GPU quota and status reports in the n8n control form. |

**Complete means all of these:**

- Done: Videos generate on their own on the server (2 finance Shorts a day, 09:00 and 18:00 WAT, delivered to Telegram).
- To do: Auto-posting to YouTube (needs a Google sign-in on the n8n upload workflow).
- To do: Auto-posting to TikTok (choose a TikTok developer app or Buffer).
- To do: The free Kaggle AI-clip add-on is switched on (needs a Kaggle token, see [VIDEO_APP_GUIDE.md](VIDEO_APP_GUIDE.md)).
- To do: The script AI is reliable (fix the relay, or keep Gemini as the main one).

**Template:** "MoneyPrinter YT". A draft exists, unpublished until the items above are done.

**Run it without the assistant:** [README](../README.md), [VIDEO_APP_GUIDE.md](VIDEO_APP_GUIDE.md), [hunyuan/README.md](../hunyuan/README.md).

## Other projects
Each of the other agents adds its own section here: its projects, whether each is a workflow or an app, what each produces, its completion definition, its repo, and its template. These get added as they come in.

| Project | Agent | Type | Produces | Complete? | Template |
|---|---|---|---|---|---|
| MoneyPrinterTurbo-plus | MoneyPrinter YT | App | Shorts videos, post text, delivery | No (see above) | Draft |
| Kaggle clip add-on | MoneyPrinter YT | Workflow | AI scene clips | No (switched off) | Part of MoneyPrinter YT |

<div style="page-break-after:always"></div>
# Video app operator guide (no assistant needed)

This guide is for running the free AI-clip add-on yourself. Everything runs on your own accounts: the MoneyPrinter server,
your n8n server, Kaggle, Google Drive, and this GitHub repo.

**Status: prepared, not switched on.** Today MoneyPrinter uses FLUX pictures with Ken Burns motion. That keeps working whether or not this add-on is on.

## What you need
- A Kaggle account with a verified phone number. Get the API token from Kaggle, under Settings, then API, then Create New Token.
- SSH access to the n8n server and the MoneyPrinter server.

## 1. Put in your Kaggle token (one time)
On the n8n server, edit the worker secrets file (template: `hunyuan/gpu.env.example`):
```
sudo nano /opt/ytauto/kaggle/secrets/gpu.env     # set KAGGLE_USERNAME and KAGGLE_KEY
sudo chmod 600 /opt/ytauto/kaggle/secrets/gpu.env
```
Never commit this file or paste it anywhere public.

## 2. Switch it on
1. In n8n, open **GPU Bridge**, **GPU Router**, and **MPT HunyuanVideo Clip Generator**. Re-pick their credentials if asked, then click **Publish/Activate**.
2. On the MoneyPrinter server, add the `VIDEO_CLIP_*` lines from `hunyuan/mpt_patch/INTEGRATION.md` to `/opt/moneyprinterturbo/.env`, then:
```
cd /opt/moneyprinterturbo && sudo docker compose --profile tunnel up -d api
```

## 3. Check it
- Open the **MPT Video App Control** form in n8n (it's password protected). It shows your Kaggle hours left and the queue, and lets you **Run batch now** or **Selftest**.
- Or on the n8n server: `python3 /opt/ytauto/scripts/gpu/kaggle_launch.py quota`

## 4. Switch it off
- Fast: in n8n, set `MPT_GPU_CLIPS=off`. MoneyPrinter then falls back to pictures straight away.
- Fully: clear `VIDEO_CLIP_BACKEND_URL=` in the MoneyPrinter `.env` and run the same `docker compose ... up -d api`.

## When the weekly Kaggle hours run out
You don't need to do anything. Jobs fall back to pictures, and the quota resets weekly. You get about 30 GPU hours a week, shared with YouTube Auto.

## Change the model
Set `VIDEO_CLIP_MODEL` in the MoneyPrinter `.env`. The options are `wan21-diffusers` (default), `ltx-t2v` (fastest), `hy15-i2v` (HunyuanVideo 1.5, which animates your FLUX picture) and `wan22-5b-diffusers`. Restart the api container after changing it.
Run **Selftest** before using `hy15-i2v`, and keep it only if one clip takes 8 minutes or less.

## After a server reboot
The containers restart on their own. If they don't:
```
cd /opt/moneyprinterturbo && sudo docker compose --profile tunnel up -d
cd /opt/ytauto && sudo docker compose up -d
```

## Kaggle rules to respect
Use batch jobs only, with no tunnels or always-on servers, and one launcher per account. Kaggle's terms say personal, non-commercial use, so treat it as a bonus that could stop.
