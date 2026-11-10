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
Each of the other agents adds its own section here: its projects, whether each is a workflow or an app, what each produces, its completion definition, its repo, and its template. These get added as they come in. The other agents' rows are as they reported them on 10 Nov 2026; see each agent's own guide for details.

| Project | Agent | Type | Produces | Complete? | Template |
|---|---|---|---|---|---|
| MoneyPrinterTurbo-plus | MoneyPrinter YT | App | Shorts videos, post text, delivery | No (see above) | Draft |
| Kaggle clip add-on | MoneyPrinter YT | Workflow | AI scene clips | No (switched off) | Part of MoneyPrinter YT |
| AmazonKDP research bot + Mini App, book builder, n8n Ideas-to-Drive, backups | AmazonKDP | Apps + workflow | KDP research, built books, ideas in Drive | Yes (reported) | Draft, unpublished ([guide](https://github.com/fairt8/kdp-bot/blob/main/docs/PROJECTS_GUIDE.md)) |
| CV rewrite, ebook summary | Data Annotation And Assessment | Workflows | Rewritten CVs, ebook summaries | Yes (reported) | CV rewrite draft, unpublished ([repo](https://github.com/fairt8/data-annotation-assessment)) |
| Deriv Signal Desk | Deriv AI | App | Rise/Fall signals, Mini App | No: paper test ends 13 Nov | Unpublished ([guide](https://github.com/fairt8/deriv-signal-desk/blob/main/docs/PROJECTS_GUIDE.md)) |
| YouTube Auto pipeline + Kaggle GPU path | YouTube Auto | Workflows | Videos, YouTube uploads | No | None yet (guide in its chat) |
| Punter: bot engine, daily run, Mini App/site, virtual football, arbitrage | Punter | Apps + workflows | Betting signals | No | None (guide in its chat, no repo yet) |
| FVG-X desk | FVG-X Trader | App | Futures alerts | No | None (guide in its chat, Oracle backup) |
| MEME TRADER | MEME TRADER | App | Paper meme trades | No | None (guide in its chat) |
| SolanaMeme desk | SolanaMeme | App | Solana meme signals | No: live trading not ready | None (guide in its chat, no repo yet) |
| GoHighLevel EaseBiz, Digital Product, Degen, Appm Setter, Stocking, Grok Futures Trader | (pending) | | | Waiting for reply | |
