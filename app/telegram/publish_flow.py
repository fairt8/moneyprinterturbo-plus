"""Telegram account picker, auto-publish, and per-account status text."""

from __future__ import annotations

from app.publish.errors import PublishRejected
from app.telegram.logic import Reply


def describe_job(job: dict) -> str:
    label = job.get("account_label") or job.get("account_id") or "account"
    platform = "YouTube" if job.get("platform") == "youtube" else "TikTok"
    state = job.get("state")
    if state == "complete":
        if job.get("youtube_url"):
            return f"{platform} · {label}\n{job['youtube_url']}"
        status = job.get("tiktok_status") or "sent"
        post = job.get("tiktok_post_id") or ""
        post_line = f"\nPost id: {post}" if post else ""
        if status == "SEND_TO_USER_INBOX":
            return (
                f"{platform} · {label}\n"
                "The draft is in the TikTok inbox. Open TikTok on your phone to finish it."
                f"{post_line}"
            )
        privacy = ""
        if job.get("platform") == "tiktok" and job.get("privacy") != "public":
            privacy = "\nTikTok kept this private. Unaudited apps can only post as private until TikTok approves the app."
        return f"{platform} · {label}\nStatus: {status}.{post_line}{privacy}"
    if state == "failed":
        return f"{platform} · {label}\nCould not publish.\n{job.get('error') or 'Unknown error.'}"
    return f"{platform} · {label}\n{state or 'queued'}"


class PublishFlow:
    def __init__(self, conversation):
        self.conversation = conversation

    @property
    def publisher(self):
        return getattr(self.conversation, "publisher", None)

    def offer(self, user_id: int, task_id: str) -> Reply:
        session = self._session(user_id)
        session["task_id"] = task_id
        self._save(user_id, session)
        buttons = self._offer_buttons()
        if not session.get("auto_publish") or self.publisher is None:
            return Reply(
                "The video is ready. Choose where to publish it, or turn on auto-publish in /accounts.",
                buttons=buttons,
            )
        try:
            jobs = self._enqueue(
                user_id,
                {
                    "task_id": task_id,
                    "target_id": session.get("auto_target_id") or "",
                    "account_ids": session.get("auto_account_ids") or [],
                    "all": not session.get("auto_target_id") and not session.get("auto_account_ids"),
                    "privacy": session.get("privacy") or "public",
                    "vary_copy": bool(session.get("vary_copy")),
                },
            )
        except PublishRejected as exc:
            return Reply(f"The video is ready, but auto-publish did not start.\n{exc.message}", buttons=buttons)
        lines = []
        watch = []
        for job in jobs:
            if job.get("state") == "failed":
                lines.append(describe_job(job))
            else:
                lines.append(f"Queued {job.get('account_label')} on {job.get('platform')}.")
                watch.append(job["id"])
        return Reply(
            "The video is ready. Auto-publish queued a separate job for each account.\n" + "\n".join(lines),
            buttons=buttons,
            watch_publish_ids=watch or None,
        )

    def on_accounts(self, user_id: int, chat_type: str = "private") -> Reply:
        blocked = self.conversation._guard(user_id, chat_type)
        if blocked:
            return blocked
        return self._accounts(user_id, self._session(user_id))

    def on_callback(self, user_id: int, data: str) -> Reply | None:
        if not self._known(data):
            return None
        session = self._session(user_id)
        if self.publisher is None:
            return Reply("Publishing is not available in this process.")
        try:
            return self._dispatch(user_id, session, data)
        except PublishRejected as exc:
            return Reply(exc.message, buttons=self._offer_buttons())

    def on_text(self, user_id: int, text: str) -> Reply | None:
        session = self._session(user_id)
        awaiting = session.get("awaiting") or ""
        if awaiting not in {"publish_title", "publish_caption", "account_label", "target_name", "account_rename"}:
            return None
        body = text.strip()
        if awaiting == "publish_title":
            session["draft_title"] = body[:150]
            session["awaiting"] = ""
            self._save(user_id, session)
            return self._draft(user_id, session)
        if awaiting == "publish_caption":
            session["draft_description"] = body[:2200]
            session["awaiting"] = ""
            self._save(user_id, session)
            return self._draft(user_id, session)
        if awaiting == "account_label":
            label = "" if body == "-" else body
            return self._send_link(user_id, session, session.get("link_platform") or "youtube", label=label)
        if awaiting == "account_rename":
            account_id = session.get("rename_id") or ""
            session["awaiting"] = ""
            self.publisher.rename(self._owner(user_id), account_id, body)
            self._save(user_id, session)
            return self._accounts(user_id, session, note=f"Renamed to {body}.")
        if awaiting == "target_name":
            picked = list(session.get("picked") or [])
            session["awaiting"] = ""
            target = self.publisher.save_target(self._owner(user_id), body, picked, is_default=False)
            session["auto_target_id"] = target["id"]
            self._save(user_id, session)
            return self._accounts(user_id, session, note=f"Saved target “{target['name']}”.")
        return None

    def _dispatch(self, user_id: int, session: dict, data: str) -> Reply:
        if data == "ac":
            session["awaiting"] = ""
            self._save(user_id, session)
            return self._accounts(user_id, session)
        if data == "ap":
            return self._auto_menu(session)
        if data == "ap:1":
            session["auto_publish"] = True
            self._save(user_id, session)
            return self._auto_menu(session)
        if data == "ap:0":
            session["auto_publish"] = False
            self._save(user_id, session)
            return self._auto_menu(session)
        if data == "ap:save":
            session["auto_publish"] = True
            session["auto_account_ids"] = list(session.get("picked") or [])
            session["auto_target_id"] = ""
            self._save(user_id, session)
            return Reply("Auto-publish will use the accounts you picked.", buttons=self._offer_buttons())
        if data == "vy":
            session["vary_copy"] = not bool(session.get("vary_copy"))
            self._save(user_id, session)
            return self._draft(user_id, session)
        if data in {"ln:yt", "ln:tt"}:
            session["awaiting"] = "account_label"
            session["link_platform"] = "youtube" if data.endswith("yt") else "tiktok"
            session["reauth_id"] = ""
            self._save(user_id, session)
            return Reply("Send a short label for this account, or send - to keep the name from the platform.")
        if data.startswith("up:"):
            return self._begin_publish(user_id, session, data.split(":", 1)[1])
        if data.startswith("pk:"):
            account_id = data[3:]
            picked = list(session.get("picked") or [])
            if account_id in picked:
                picked.remove(account_id)
            else:
                picked.append(account_id)
            session["picked"] = picked
            self._save(user_id, session)
            return self._picker(user_id, session)
        if data == "ok:all":
            session["picked"] = [item["id"] for item in self._visible_accounts(user_id, session)]
            self._save(user_id, session)
            return self._prepare_draft(user_id, session)
        if data == "ok":
            return self._prepare_draft(user_id, session)
        if data == "do":
            return self._publish_now(user_id, session)
        if data.startswith("pr:") and data[3:] in {"public", "unlisted", "private"}:
            session["privacy"] = data[3:]
            self._save(user_id, session)
            return self._draft(user_id, session)
        if data == "ed:title":
            session["awaiting"] = "publish_title"
            self._save(user_id, session)
            return Reply("Send the title to use. YouTube keeps the first 100 characters.")
        if data == "ed:caption":
            session["awaiting"] = "publish_caption"
            self._save(user_id, session)
            return Reply("Send the description. Hashtags can stay on their own line.")
        if data.startswith("ul:"):
            return Reply(
                "Unlink this account? Its refresh token will be deleted from this server.",
                buttons=[
                    [{"text": "Unlink", "callback_data": "uy:" + data[3:]}],
                    [{"text": "Back", "callback_data": "ac"}],
                ],
            )
        if data.startswith("uy:"):
            self.publisher.unlink(self._owner(user_id), data[3:])
            session["picked"] = [item for item in session.get("picked") or [] if item != data[3:]]
            self._save(user_id, session)
            return self._accounts(user_id, session, note="Account unlinked.")
        if data.startswith("rn:"):
            session["awaiting"] = "account_rename"
            session["rename_id"] = data[3:]
            self._save(user_id, session)
            return Reply("Send the new label.")
        if data.startswith("ra:"):
            return self._send_link(user_id, session, "", label="", account_id=data[3:])
        if data == "nt":
            if not session.get("picked"):
                return Reply("Pick one or more accounts first.", buttons=self._account_buttons(user_id, session))
            session["awaiting"] = "target_name"
            self._save(user_id, session)
            return Reply("Send a name for this publish target, for example Nightly shorts.")
        if data.startswith("df:"):
            target = self.publisher.set_default_target(self._owner(user_id), data[3:])
            session["auto_target_id"] = target["id"]
            self._save(user_id, session)
            return self._accounts(user_id, session, note=f"Default target is now {target['name']}.")
        return Reply("That publish button is no longer valid. Use /accounts.")

    def _begin_publish(self, user_id: int, session: dict, kind: str) -> Reply:
        if not session.get("task_id"):
            return Reply("There is no finished video to publish yet.")
        owner = self._owner(user_id)
        accounts = self.publisher.accounts(owner)
        if kind == "def":
            target = next((item for item in self.publisher.targets(owner) if item.get("is_default")), None)
            if target is None and session.get("auto_target_id"):
                target = next((item for item in self.publisher.targets(owner) if item["id"] == session["auto_target_id"]), None)
            if target is None:
                return Reply("No default publish target yet. Create one with /accounts.", buttons=self._offer_buttons())
            session["picked"] = list(target["account_ids"])
            session["publish_platform"] = ""
        elif kind == "all":
            session["picked"] = [item["id"] for item in accounts]
            session["publish_platform"] = ""
        else:
            platform = "youtube" if kind == "yt" else "tiktok"
            matching = [item for item in accounts if item["platform"] == platform]
            session["publish_platform"] = platform
            session["picked"] = [item["id"] for item in matching]
            if not matching:
                session["link_platform"] = platform
                self._save(user_id, session)
                return self._send_link(user_id, session, platform, label="")
            if len(matching) > 1:
                self._save(user_id, session)
                return self._picker(user_id, session)
        self._save(user_id, session)
        return self._prepare_draft(user_id, session)

    def _prepare_draft(self, user_id: int, session: dict) -> Reply:
        if not session.get("picked"):
            return Reply("Pick at least one account.", buttons=self._offer_buttons())
        platform = session.get("publish_platform") or "youtube"
        if platform not in {"youtube", "tiktok"}:
            platform = "youtube"
        draft = self.publisher.preview(self._owner(user_id), session["task_id"], platform)
        session["draft_title"] = draft.get("title") or ""
        session["draft_description"] = draft.get("description") or ""
        session["draft_hashtags"] = list(draft.get("hashtags") or [])
        session["awaiting"] = ""
        self._save(user_id, session)
        return self._draft(user_id, session)

    def _publish_now(self, user_id: int, session: dict) -> Reply:
        jobs = self._enqueue(
            user_id,
            {
                "task_id": session.get("task_id") or "",
                "account_ids": list(session.get("picked") or []),
                "title": session.get("draft_title") or "",
                "description": session.get("draft_description") or "",
                "hashtags": list(session.get("draft_hashtags") or []),
                "privacy": session.get("privacy") or "public",
                "vary_copy": bool(session.get("vary_copy")),
                "platform": session.get("publish_platform") or "",
            },
        )
        watch = [job["id"] for job in jobs if job.get("state") != "failed"]
        lines = [describe_job(job) if job.get("state") == "failed" else f"Queued {job.get('account_label')} ({job.get('platform')})." for job in jobs]
        return Reply(
            "Each account has its own job.\n" + "\n".join(lines),
            buttons=self._offer_buttons(),
            watch_publish_ids=watch or None,
        )

    def _draft(self, user_id: int, session: dict) -> Reply:
        tags = " ".join(session.get("draft_hashtags") or [])
        names = ", ".join(self._picked_labels(user_id, session)) or "none"
        vary = "on" if session.get("vary_copy") else "off"
        text = (
            f"Title: {session.get('draft_title') or '(empty)'}\n"
            f"Description: {session.get('draft_description') or '(empty)'}\n"
            f"Hashtags: {tags or '(none)'}\n"
            f"Privacy: {session.get('privacy') or 'public'}\n"
            f"Accounts: {names}\n"
            f"Vary the wording per account: {vary}\n\n"
            "YouTube Shorts get #Shorts when the video is vertical. "
            "An unaudited TikTok app can only post as private."
        )
        return Reply(
            text,
            buttons=[
                [{"text": "Publish", "callback_data": "do"}],
                [
                    {"text": "Edit title", "callback_data": "ed:title"},
                    {"text": "Edit description", "callback_data": "ed:caption"},
                ],
                [
                    {"text": "Public", "callback_data": "pr:public"},
                    {"text": "Unlisted", "callback_data": "pr:unlisted"},
                    {"text": "Private", "callback_data": "pr:private"},
                ],
                [{"text": "Vary caption per account", "callback_data": "vy"}],
                [{"text": "Accounts", "callback_data": "ac"}],
            ],
        )

    def _picker(self, user_id: int, session: dict) -> Reply:
        rows = []
        picked = set(session.get("picked") or [])
        for account in self._visible_accounts(user_id, session):
            mark = "✓ " if account["id"] in picked else ""
            warn = " (re-link)" if account.get("needs_reauth") else ""
            rows.append([{"text": f"{mark}{account['label']}{warn}", "callback_data": "pk:" + account["id"]}])
        rows.append(
            [
                {"text": "Publish selected", "callback_data": "ok"},
                {"text": "All of these", "callback_data": "ok:all"},
            ]
        )
        rows.append([{"text": "Back", "callback_data": "ac"}])
        return Reply("Choose one, several, or all accounts. Each one is a separate upload.", buttons=rows)

    def _accounts(self, user_id: int, session: dict, note: str = "") -> Reply:
        owner = self._owner(user_id)
        accounts = self.publisher.accounts(owner) if self.publisher else []
        targets = self.publisher.targets(owner) if self.publisher else []
        lines = [note] if note else []
        if not accounts:
            lines.append("No YouTube channels or TikTok accounts are linked yet.")
        for account in accounts[:20]:
            state = "needs re-authorization" if account.get("needs_reauth") else "linked"
            project = f" · project {account['project_id']}" if account.get("project_id") else ""
            lines.append(f"{account['platform']}: {account['label']} ({state}){project}")
        if len(accounts) > 20:
            lines.append("Showing 20 accounts. The accounts page lists the rest.")
        for target in targets:
            mark = " default" if target.get("is_default") else ""
            lines.append(f"Target {target['name']}{mark}: {len(target.get('account_ids') or [])} account(s)")
        auto = "on" if session.get("auto_publish") else "off"
        lines.append(f"Auto-publish is {auto}.")
        self._save(user_id, session)
        return Reply("\n".join(lines), buttons=self._account_buttons(user_id, session))

    def _account_buttons(self, user_id: int, session: dict) -> list[list[dict]]:
        rows: list[list[dict]] = [
            [
                {"text": "Link YouTube", "callback_data": "ln:yt"},
                {"text": "Link TikTok", "callback_data": "ln:tt"},
            ]
        ]
        if self.publisher is None:
            return rows
        for account in self.publisher.accounts(self._owner(user_id))[:8]:
            rows.append(
                [
                    {"text": "Rename " + account["label"][:24], "callback_data": "rn:" + account["id"]},
                    {"text": "Re-link", "callback_data": "ra:" + account["id"]},
                    {"text": "Unlink", "callback_data": "ul:" + account["id"]},
                ]
            )
        rows.append([{"text": "Save picked accounts as a target", "callback_data": "nt"}])
        for target in self.publisher.targets(self._owner(user_id))[:6]:
            rows.append([{"text": "Default: " + target["name"][:28], "callback_data": "df:" + target["id"]}])
        rows.append([{"text": "Auto-publish", "callback_data": "ap"}])
        return rows

    def _auto_menu(self, session: dict) -> Reply:
        state = "on" if session.get("auto_publish") else "off"
        target = session.get("auto_target_id") or "none"
        return Reply(
            f"Auto-publish is {state}. Default target id: {target}. "
            "When a video finishes, each account in that target gets its own job.",
            buttons=[
                [
                    {"text": "Turn on", "callback_data": "ap:1"},
                    {"text": "Turn off", "callback_data": "ap:0"},
                ],
                [{"text": "Use the accounts I picked", "callback_data": "ap:save"}],
                [{"text": "Accounts", "callback_data": "ac"}],
            ],
        )

    def _send_link(self, user_id: int, session: dict, platform: str, *, label: str, account_id: str = "") -> Reply:
        if account_id:
            accounts = self.publisher.accounts(self._owner(user_id))
            match = next((item for item in accounts if item["id"] == account_id), None)
            if match is None:
                return Reply("That account is not linked here.")
            platform = match["platform"]
        session["awaiting"] = ""
        self._save(user_id, session)
        url = self.publisher.connect_url(
            self._owner(user_id),
            platform,
            label=label,
            account_id=account_id,
        )
        name = "YouTube" if platform == "youtube" else "TikTok"
        return Reply(
            f"Open this link to authorize {name}. It expires soon and can be used once.\n{url}",
            buttons=[[{"text": "Accounts", "callback_data": "ac"}]],
        )

    def _enqueue(self, user_id: int, payload: dict) -> list[dict]:
        jobs = self.publisher.enqueue(self._owner(user_id), payload)
        if not isinstance(jobs, list):
            raise PublishRejected("The publish service did not return any jobs.")
        return jobs

    def _visible_accounts(self, user_id: int, session: dict) -> list[dict]:
        accounts = self.publisher.accounts(self._owner(user_id))
        platform = session.get("publish_platform") or ""
        if platform in {"youtube", "tiktok"}:
            return [item for item in accounts if item["platform"] == platform]
        return accounts

    def _picked_labels(self, user_id: int, session: dict) -> list[str]:
        wanted = set(session.get("picked") or [])
        return [item["label"] for item in self.publisher.accounts(self._owner(user_id)) if item["id"] in wanted]

    def _offer_buttons(self) -> list[list[dict]]:
        return [
            [
                {"text": "Upload to YouTube", "callback_data": "up:yt"},
                {"text": "Upload to TikTok", "callback_data": "up:tt"},
            ],
            [
                {"text": "All accounts", "callback_data": "up:all"},
                {"text": "Default target", "callback_data": "up:def"},
            ],
            [{"text": "Manage accounts", "callback_data": "ac"}],
        ]

    def _session(self, user_id: int) -> dict:
        session = self.conversation._load(user_id)
        session.setdefault("picked", [])
        session.setdefault("privacy", "public")
        session.setdefault("auto_publish", False)
        session.setdefault("auto_target_id", "")
        session.setdefault("auto_account_ids", [])
        session.setdefault("vary_copy", False)
        session.setdefault("draft_title", "")
        session.setdefault("draft_description", "")
        session.setdefault("draft_hashtags", [])
        session.setdefault("publish_platform", "")
        return session

    def _save(self, user_id: int, session: dict) -> None:
        self.conversation.sessions.save(user_id, session)

    def _owner(self, user_id: int) -> str:
        return f"tg:{user_id}"

    def _known(self, data: str) -> bool:
        if data in {"ac", "ap", "ap:1", "ap:0", "ap:save", "vy", "ok", "ok:all", "do", "nt", "ln:yt", "ln:tt"}:
            return True
        prefixes = ("up:", "pk:", "pr:", "ed:", "ul:", "uy:", "rn:", "ra:", "df:")
        return data.startswith(prefixes)
