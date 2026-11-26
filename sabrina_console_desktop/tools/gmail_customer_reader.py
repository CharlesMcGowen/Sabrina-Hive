"""Existing-browser Gmail reader restricted to configured customer threads."""
from __future__ import annotations

import os
import re
from urllib.parse import quote, unquote, urlparse

from tools.customer_engagement import load_customer_policy
from tools.customer_document_ingest import CustomerDocumentIndex, ingest_customer_document, rejected_document


class GmailCustomerReader:
    def __init__(self, cdp_url: str | None = None, policy=None, *, document_index_path=None, document_ingestor=None):
        self.policy = policy or load_customer_policy()
        self.cdp_url = cdp_url or os.environ.get("SABRINA_SOCIAL_CDP_URL") or self.policy.get("cdp_url", "http://localhost:9222")
        self.playwright = self.browser = self.context = self.page = None
        self.owns_page = False
        self.document_index_path = document_index_path
        self.document_ingestor = document_ingestor or ingest_customer_document

    def connect(self):
        from playwright.sync_api import sync_playwright
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.connect_over_cdp(self.cdp_url, timeout=15000)
        contexts = self.browser.contexts
        if len(contexts) != 1:
            raise RuntimeError("Expected one Sabrina browser context")
        self.context = contexts[0]
        # Work in a temporary tab so a user-owned Gmail tab cannot race the
        # automation or be left on a different search/thread when we detach.
        # Closing that user tab does not end the authenticated browser session.
        self.page = self.context.new_page()
        self.owns_page = True
        self.page.goto("https://mail.google.com/mail/u/0/", wait_until="domcontentloaded", timeout=30000)
        self._verify_account()
        return self

    def _verify_account(self):
        surface = urlparse(self.page.url)
        if surface.hostname != "mail.google.com" or not surface.path.startswith("/mail/u/0/"):
            raise RuntimeError("Gmail authentication is required in Sabrina's existing browser")
        account = self.page.locator('a[aria-label*="Google Account"]').first
        try:
            account.wait_for(state="attached", timeout=30000)
        except Exception:
            if urlparse(self.page.url).hostname != "mail.google.com":
                raise RuntimeError("Gmail authentication is required in Sabrina's existing browser") from None
            raise
        label = (account.get_attribute("aria-label") or "").lower()
        emails = re.findall(r"[\w.+-]+@[\w.-]+\.[a-z]{2,}", label)
        if self.policy["account_email"].lower() not in emails:
            raise RuntimeError("Gmail account identity does not match the configured customer account")

    def _contact(self, contact_id: str):
        rows = [row for row in self.policy["contacts"] if row["id"] == contact_id]
        if len(rows) != 1:
            raise ValueError("Contact is not uniquely allowlisted")
        return rows[0]

    def open_allowlisted_thread(self, contact_id: str):
        contact = self._contact(contact_id)
        query = f'to:{contact["email"]} OR from:{contact["email"]}'
        self.page.goto("https://mail.google.com/mail/u/0/#search/" + quote(query, safe=""), wait_until="domcontentloaded", timeout=30000)
        # Gmail's search view can take materially longer than DOMContentLoaded to hydrate when the
        # browser has been idle. Keep this bounded, but do not treat a normal 15-20 second render as
        # an operator problem.
        allowed = contact.get("subject_allowlist", [])
        if not allowed:
            raise RuntimeError("No allowlisted Gmail subject is configured")
        rows = self.page.locator("tr.zA:visible")
        # A hash navigation can leave inbox rows visible while search hydrates.
        # Wait for the requested subject, not merely any already-visible row.
        rows.filter(has_text=re.compile("|".join(re.escape(subject) for subject in allowed))).first.wait_for(
            state="visible", timeout=45000
        )
        matches = []
        for i in range(rows.count()):
            text = rows.nth(i).inner_text()
            # Gmail search already restricts the participant. The first outbound
            # message can render only "me", so the exact subject is the stable gate.
            if any(subject in text for subject in allowed):
                matches.append(rows.nth(i))
        if len(matches) != 1:
            raise RuntimeError(f"Expected one allowlisted Gmail thread, found {len(matches)}")
        matches[0].click()
        self.page.locator("div.adn.ads").first.wait_for(state="attached", timeout=45000)
        return self.read_open_thread(contact_id)

    def read_open_thread(self, contact_id: str):
        contact = self._contact(contact_id)
        subject = self.page.locator("h2.hP:visible").first.inner_text().strip()
        if subject not in contact.get("subject_allowlist", []):
            raise RuntimeError("Open Gmail subject is not allowlisted")
        raw_messages = self.page.locator("div.adn.ads").evaluate_all("""nodes => nodes.map(n => {
          const sender=n.querySelector('span.gD[email]');
          const body=n.querySelector('.a3s.aiL,.a3s');
          const stamp=n.querySelector('span.g3');
          const attachments=Array.from(n.querySelectorAll('.aQH')).map((a,index) => ({
            index,
            filename:(a.querySelector('.aV3')?.textContent||a.getAttribute('data-tooltip')||`attachment-${index}`).trim()
          }));
          return {id:n.getAttribute('data-message-id')||n.getAttribute('data-legacy-message-id')||n.closest('[data-message-id]')?.getAttribute('data-message-id')||'',
            email:sender?.getAttribute('email')||'',author:sender?.getAttribute('name')||sender?.textContent?.trim()||'',
            text:body?.innerText?.trim()||'',timestamp_text:stamp?.getAttribute('title')||stamp?.innerText?.trim()||'',
            attachments};
        })""")
        self._ingest_external_attachments(raw_messages, contact)
        thread_id = unquote(self.page.url.rstrip("/").split("/")[-1])
        return self.normalize(
            {"url": self.page.url, "subject": subject, "thread_id": thread_id, "messages": raw_messages},
            self.policy["account_email"], contact
        )

    def _ingest_external_attachments(self, raw_messages, contact):
        """Download only allowlisted external documents from the open Gmail thread."""
        index = CustomerDocumentIndex(self.document_index_path)
        try:
            message_nodes = self.page.locator("div.adn.ads")
            for message_position, message in enumerate(raw_messages):
                if str(message.get("email", "")).lower() != contact["email"].lower():
                    continue
                message_id = message.get("id", "")
                for attachment in message.get("attachments", []):
                    attachment_index = int(attachment.get("index", 0))
                    filename = attachment.get("filename", "document")
                    cached = index.get(message_id, attachment_index, filename)
                    if cached is not None:
                        attachment.update(cached)
                        continue
                    try:
                        node = message_nodes.nth(message_position).locator(".aQH").nth(attachment_index)
                        button = node.locator(
                            "a[download], [role='button'][aria-label*='Download'], "
                            "[role='button'][data-tooltip*='Download'], .aQy"
                        ).first
                        if button.count() != 1:
                            raise RuntimeError("Gmail attachment has no unique download control")
                        with self.page.expect_download(timeout=15000) as download_info:
                            button.click(force=True)
                        download = download_info.value
                        manifest = self.document_ingestor(download.path(), download.suggested_filename or filename)
                    except Exception as error:
                        manifest = rejected_document(filename, error)
                    index.save(message_id, attachment_index, filename, manifest)
                    attachment.update(manifest)
        finally:
            index.close()

    @staticmethod
    def normalize(raw, account_email: str, contact: dict):
        if not re.fullmatch(r"[A-Za-z0-9_-]{10,}", raw.get("thread_id", "")):
            raise ValueError("Missing stable Gmail thread ID")
        account_email = account_email.lower()
        contact_email = contact["email"].lower()
        messages = []
        reasons = []
        for item in raw.get("messages", []):
            email = item.get("email", "").lower()
            ownership = "self" if email == account_email else "external" if email == contact_email else "unknown"
            attachments = item.get("attachments", [])
            attachment_text = "\n\n".join(
                f"[Attachment: {attachment.get('filename', 'document')}]\n{attachment.get('extracted_text', '')}"
                for attachment in attachments
                if attachment.get("status") == "ingested"
            )
            body_text = item.get("text", "")
            combined_text = "\n\n".join(part for part in (body_text, attachment_text) if part).strip()
            messages.append({
                **item,
                "text": combined_text,
                "body_text": body_text,
                "attachments": attachments,
                "author_id": email,
                "ownership": ownership,
            })
        ids = [m["id"] for m in messages]
        if not messages:
            reasons.append("No messages captured")
        if any(not m["id"] or not m["text"] or not m["author_id"] or m["ownership"] == "unknown" for m in messages):
            reasons.append("Message identity, sender, or body is incomplete")
        if len(ids) != len(set(ids)):
            reasons.append("Duplicate Gmail message IDs")
        rejected = [
            attachment
            for message in messages if message["ownership"] == "external"
            for attachment in message.get("attachments", [])
            if attachment.get("status") != "ingested"
        ]
        if rejected:
            reasons.append("Customer attachment requires operator review: " + "; ".join(
                f"{attachment.get('filename', 'document')}: {attachment.get('reason', 'not ingested')}"
                for attachment in rejected
            ))
        return {
            "account_id": account_email,
            "thread_id": raw["thread_id"],
            "url": raw["url"],
            "subject": raw["subject"],
            "peer_name": contact["name"],
            "peer_id": contact_email,
            "contact_id": contact["id"],
            "messages": messages,
            "complete": not reasons,
            "completeness_reasons": reasons,
            "completeness_basis": "all rendered Gmail messages have stable IDs and allowlisted senders; supported customer documents are hashed and text-extracted without execution",
        }

    def read_saved_thread(self, expected):
        fresh = self.open_allowlisted_thread(expected["contact_id"])
        for key in ("account_id", "thread_id", "peer_id", "subject"):
            if fresh[key] != expected[key]:
                raise RuntimeError("Saved Gmail thread identity mismatch: " + key)
        return fresh

    def disconnect(self):
        try:
            if self.owns_page and self.page and not self.page.is_closed():
                self.page.close()
        finally:
            if self.playwright:
                self.playwright.stop()
            self.playwright = self.browser = self.context = self.page = None
