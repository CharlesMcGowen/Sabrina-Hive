"""Exactly-once Gmail delivery for a previously authorized outbox item."""
from __future__ import annotations

import time


def _plain(text):
    return " ".join((text or "").split())


def find_gmail_delivery(snapshot, expected, text, before_ids):
    for message in snapshot["messages"]:
        if (
            message["id"] not in before_ids
            and message["ownership"] == "self"
            and _plain(message["text"]) == _plain(text)
        ):
            if snapshot["account_id"] != expected["account_id"] or snapshot["thread_id"] != expected["thread_id"]:
                raise RuntimeError("Gmail delivery appeared in the wrong account or thread")
            return {
                "status": "sent",
                "message_id": message["id"],
                "account_id": snapshot["account_id"],
                "thread_id": snapshot["thread_id"],
                "text": text,
            }
    return None


class GmailMessageSender:
    def __init__(self, reader, store, outbox):
        self.reader = reader
        self.store = store
        self.outbox = outbox

    @staticmethod
    def _visible(locator):
        return [locator.nth(i) for i in range(locator.count()) if locator.nth(i).is_visible()]

    def send(self, outbox_id):
        entry = self.outbox.get(outbox_id)
        expected = self.store.snapshot(entry["account"], entry["thread"])
        fresh = self.reader.read_saved_thread(expected)
        version, _ = self.store.observe(fresh)
        fresh["version"] = version
        claimed = self.outbox.claim(outbox_id, fresh)
        before_ids = {message["id"] for message in fresh["messages"]}
        page = self.reader.page
        clicked = False
        try:
            existing = self._visible(page.locator('div[contenteditable="true"][aria-label*="Message Body"]'))
            if any(editor.inner_text().strip() for editor in existing):
                raise RuntimeError("A human draft already exists; Sabrina will not overwrite it")
            if existing:
                editor = existing[-1]
            else:
                replies = self._visible(page.get_by_text("Reply", exact=True))
                if not replies:
                    raise RuntimeError("Unique Gmail reply control not found")
                replies[-1].click()
                page.locator('div[contenteditable="true"][aria-label*="Message Body"]').last.wait_for(state="visible", timeout=10000)
                editor = self._visible(page.locator('div[contenteditable="true"][aria-label*="Message Body"]'))[-1]
            if editor.inner_text().strip():
                raise RuntimeError("A human draft appeared; Sabrina will not overwrite it")
            editor.fill(claimed["text"])
            if _plain(editor.inner_text()) != _plain(claimed["text"]):
                raise RuntimeError("Gmail composer text does not match the authorized draft")
            root = editor.locator("xpath=ancestor::div[contains(@class,'M9')][1]")
            buttons = self._visible(root.locator('div[role="button"][data-tooltip^="Send"]'))
            if len(buttons) != 1:
                raise RuntimeError("Unique Gmail send control not found")
            buttons[0].click()
            clicked = True
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                page.wait_for_timeout(750)
                snapshot = self.reader.read_open_thread(expected["contact_id"])
                receipt = find_gmail_delivery(snapshot, expected, claimed["text"], before_ids)
                if receipt:
                    self.store.observe(snapshot)
                    self.outbox.finish(outbox_id, receipt)
                    return receipt
            receipt = {"status": "uncertain", "reason": "No matching Gmail message ID appeared after the single send click"}
            self.outbox.finish(outbox_id, receipt)
            return receipt
        except Exception as error:
            if clicked:
                receipt = {"status": "uncertain", "reason": str(error)}
                self.outbox.finish(outbox_id, receipt)
                return receipt
            release = getattr(self.outbox, "release_before_send", None)
            if callable(release):
                release(outbox_id, str(error))
            raise
