"""Read only fresh, service-scoped verification mail in an existing Gmail session.

No mail sending, inbox export, cookie copying, credential entry or attachments.
Gmail query dates: https://developers.google.com/workspace/gmail/api/guides/filtering
"""
import time
from urllib.parse import quote, urlsplit


class GmailVerificationReader:
    def __init__(self, client):self.client = client

    def read(self, account, site, sent_at):
        from playwright.sync_api import sync_playwright
        from dateutil.parser import parse
        domains = account.get('sender_domains', {}).get(site, [urlsplit(site).hostname])
        query = 'to:' + account['email'] + ' {' + ' '.join('from:@' + d for d in domains) + '} after:' + str(int(sent_at)) + ' -in:trash -in:spam'
        # Same resource used by existing social work; never borrow a busy profile.
        with self.client.lease('browser:linkedin-primary', wait_seconds=5):
            runtime = sync_playwright().start(); browser = page = None
            try:
                browser = runtime.chromium.connect_over_cdp('http://127.0.0.1:9222', timeout=5000)
                if len(browser.contexts) != 1:raise RuntimeError('Mailbox profile ambiguous')
                page = browser.contexts[0].new_page()
                page.goto('https://mail.google.com/mail/u/0/#search/' + quote(query, safe=''), wait_until='domcontentloaded', timeout=30000)
                page.wait_for_timeout(3500)
                if urlsplit(page.url).hostname != 'mail.google.com':raise RuntimeError('Authorized Gmail session requires operator sign-in')
                labels = page.locator('a[aria-label*="Google Account"]').evaluate_all('(nodes)=>nodes.map(n=>n.getAttribute("aria-label"))')
                if not any(account['email'].lower() in label.lower() for label in labels):
                    raise RuntimeError('Authorized Gmail account identity not verified')
                rows = page.locator('tr.zA:visible')
                if rows.count() == 0:return []
                if rows.count() > 3:raise RuntimeError('Verification search ambiguous; inspect manually')
                messages = []
                # Read each exact filtered thread in this task-owned tab.
                for index in range(rows.count()):
                    page.locator('tr.zA:visible').nth(index).click(timeout=5000)
                    page.locator('div.adn.ads').first.wait_for(state='attached', timeout=15000)
                    subject = page.locator('h2.hP:visible').first.inner_text()
                    raw = page.locator('div.adn.ads').evaluate_all('''nodes=>nodes.map(n=>({
                      id:n.getAttribute('data-message-id')||n.getAttribute('data-legacy-message-id')||'',
                      sender:n.querySelector('span.gD[email]')?.getAttribute('email')||'',
                      recipients:[...n.querySelectorAll('span[email]')].filter(e=>!e.matches('span.gD')).map(e=>e.getAttribute('email')),
                      stamp:n.querySelector('span.g3')?.getAttribute('title')||'',
                      text:n.querySelector('.a3s.aiL,.a3s')?.innerText||'',
                      links:[...n.querySelectorAll('.a3s a[href]')].map(a=>({label:a.innerText,url:a.href}))
                    }))''')
                    for m in raw:
                        try:timestamp = parse(m['stamp']).timestamp()
                        except (ValueError, TypeError, OverflowError):continue
                        if account['email'] not in m['recipients']:continue
                        messages.append(dict(m, subject=subject, timestamp=timestamp, recipient=account['email']))
                    page.goto('https://mail.google.com/mail/u/0/#search/' + quote(query, safe=''), wait_until='domcontentloaded', timeout=15000)
                    page.wait_for_timeout(1000)
                return messages
            except RuntimeError:raise
            except Exception as exc:
                # Playwright diagnostics can embed verification text/URLs.
                raise RuntimeError('Verification mailbox unavailable: ' + type(exc).__name__) from None
            finally:
                if page and not page.is_closed():page.close()
                runtime.stop()  # Detach; never close the shared browser.
