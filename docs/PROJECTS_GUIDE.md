# Projects and templates guide

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
- [x] Videos generate on their own on the server (2 finance Shorts a day, 09:00 and 18:00 WAT, delivered to Telegram).
- [ ] Auto-posting to YouTube (needs a Google sign-in on the n8n upload workflow).
- [ ] Auto-posting to TikTok (choose a TikTok developer app or Buffer).
- [ ] The free Kaggle AI-clip add-on is switched on (needs a Kaggle token, see [VIDEO_APP_GUIDE.md](VIDEO_APP_GUIDE.md)).
- [ ] The script AI is reliable (fix the relay, or keep Gemini as the main one).

**Template:** "MoneyPrinter YT". A draft exists, unpublished until the items above are done.

**Run it without the assistant:** [README](../README.md), [VIDEO_APP_GUIDE.md](VIDEO_APP_GUIDE.md), [hunyuan/README.md](../hunyuan/README.md).

## Other projects
Each of the other agents adds its own section here: its projects, whether each is a workflow or an app, what each produces, its completion definition, its repo, and its template. These get added as they come in.

| Project | Agent | Type | Produces | Complete? | Template |
|---|---|---|---|---|---|
| MoneyPrinterTurbo-plus | MoneyPrinter YT | App | Shorts videos, post text, delivery | No (see above) | Draft |
| Kaggle clip add-on | MoneyPrinter YT | Workflow | AI scene clips | No (switched off) | Part of MoneyPrinter YT |
