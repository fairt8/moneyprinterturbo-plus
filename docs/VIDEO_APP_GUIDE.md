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
