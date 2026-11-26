"""Read LinkedIn's visible conversation DOM. Missing identities are never guessed."""
import hashlib
import re
from urllib.parse import unquote, urlparse


def profile_id(url):
    parsed = urlparse(url)
    match = re.fullmatch(r"/in/([^/]+)/?", parsed.path)
    if parsed.hostname not in ("www.linkedin.com", "linkedin.com") or not match:
        return None
    return "https://www.linkedin.com/in/" + match[1] + "/"


def post_identity(url):
    parsed = urlparse(url)
    match = re.search(r"urn:li:(?:activity|ugcPost|share):\d+", unquote(parsed.path))
    if parsed.scheme != "https" or parsed.hostname not in ("www.linkedin.com", "linkedin.com") or not match:
        return None, None
    return match[0], "https://www.linkedin.com/feed/update/" + match[0] + "/"


def comment_identity(raw):
    match = re.search(r"urn:li:(?:fsd_comment:\d+|comment:\([^\s&#?]+?\))", unquote(raw or ""))
    return match[0] if match else None


CAPTURE_JS = r"""() => {
    const nodeID = e => e && (e.getAttribute('data-id') || e.getAttribute('data-urn') || e.getAttribute('componentkey') || e.id);
    const visible = e => !!e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden';
    const expandRE = /^(?:(?:load|show|see|view) (?:\d+ )?(?:more |previous |all )?(?:comments|replies)|(?:see|show|view) \d+ replies)$/i;
    const expanders = scope => Array.from(scope.querySelectorAll('button,[role="button"]')).filter(e=>visible(e) && expandRE.test(e.innerText.trim()));
    const collapsed = scope => Array.from(scope.querySelectorAll('button,[role="button"]')).some(e=>visible(e) && /^(?:…\s*|\.\.\.\s*)?(?:see )?more$/i.test(e.innerText.trim()));
    const canonicalPosts = Array.from(document.querySelectorAll('[data-urn^="urn:li:activity:"], .feed-shared-update-v2'));
    const headings = Array.from(document.querySelectorAll('h2')).filter(e => e.textContent.trim() === 'Feed post');
    const scopeFor = h => {
        let node = h.parentElement;
        for (let i=0;node && i<8;i++,node=node.parentElement) {
            if (Array.from(node.querySelectorAll('h2')).filter(e=>e.textContent.trim()==='Feed post').length>1) break;
            if (node.querySelector('[componentkey^="commentsSectionContainer"], [componentkey^="CommentComponentReference_"]')) return node;
        }
        return h.parentElement;
    };
    const cards = canonicalPosts.length ? canonicalPosts.filter(e =>
        !canonicalPosts.some(other => other !== e && other.contains(e))) : headings.map(scopeFor);
    return cards.map((card, index) => {
        const heading = Array.from(card.querySelectorAll('h2')).find(h => h.textContent.trim() === 'Feed post');
        const bodyCard = heading ? heading.parentElement : card;
        const key = bodyCard.getAttribute('componentkey');
        const paragraphs = Array.from(bodyCard.querySelectorAll('p')).filter(p => key && p.parentElement.getAttribute('componentkey') === key);
        const classic = card.querySelector('.feed-shared-update-v2__description, .update-components-text');
        const text = classic ? classic.innerText : paragraphs.map(p=>p.innerText).join('\n');
        const link = Array.from(card.querySelectorAll('a[href]')).find(a=>a.href.includes('/feed/update/urn:li:activity:'));
        const nodes = Array.from(card.querySelectorAll('[data-id*="urn:li:comment"], [data-urn*="urn:li:comment"], [data-id*="urn:li:fsd_comment"], [componentkey^="CommentComponentReference_urn:li:comment:"], .comments-comment-item, .comments-reply-item'))
            .filter((e,i,all) => !all.some(other => other!==e && e.contains(other) &&
                (other.getAttribute('data-id') || other.getAttribute('data-urn') || other.getAttribute('componentkey') || other.id) === (e.getAttribute('data-id') || e.getAttribute('data-urn') || e.getAttribute('componentkey') || e.id)));
        const comments = nodes.map((e, n) => {
            const id = e.getAttribute('data-id') || e.getAttribute('data-urn') || e.getAttribute('componentkey') || e.id;
            const anchor = Array.from(e.querySelectorAll('a[href]')).find(a=>a.href.includes('commentUrn='));
            const authors = Array.from(e.querySelectorAll('a[href*="/in/"]'));
            const avatar = authors[0];
            const author = authors.find(a=>a.innerText.trim()) || avatar;
            const body = e.querySelector('.comments-comment-item__main-content, .comments-comment-item-content-body, .comments-comment-item__inline-show-more-text');
            const bodies = body ? [body] : Array.from(e.querySelectorAll('p[componentkey]')).filter(p=>!p.closest('a') && p.closest('[componentkey^="CommentComponentReference_urn:li:comment:"]')===e);
            const mentions = bodies.flatMap(p=>Array.from(p.querySelectorAll('a[href*="/in/"]')).map(a=>a.href));
            const parent = nodes.find(other => other !== e && other.contains(e) && !nodes.some(mid=>mid!==e && mid!==other && other.contains(mid) && mid.contains(e)));
            const column = e.closest('[data-component-type="LazyColumn"][componentkey*="pagedCommentsContainer"]');
            e.setAttribute('data-sabrina-comment-index', index+'-'+n);
            return {raw_id:id || (anchor ? anchor.href : ''), parent_raw: e.getAttribute('data-parent-comment-urn') ||
                nodeID(parent) || '', parent_source: parent ? 'dom_nesting' : 'post',
                nested: !!parent, author_id:author ? author.href : '', author:author ? author.innerText.trim() : '',
                text:bodies.map(p=>p.innerText).join('\n').trim(), mentions,
                collapsed:collapsed(e), url:anchor ? anchor.href : '', dom_index:index+'-'+n,
                column:column ? nodeID(column) : '',
                indent:column && avatar && visible(avatar) ? avatar.getBoundingClientRect().x - e.getBoundingClientRect().x : null};
        });
        // LinkedIn's flat comment list represents reply depth by avatar indentation,
        // not DOM containment. Use only this identified layout; uncertain rows block capture.
        const stacks = new Map();
        for (const c of comments) {
            if (!c.column || c.parent_raw) continue;
            if (c.indent === null) { c.parent_unknown=true; continue; }
            const stack = stacks.get(c.column) || [];
            while (stack.length && c.indent <= stack.at(-1).indent + 4) stack.pop();
            if (stack.length) {
                const delta=c.indent-stack.at(-1).indent;
                if (delta < 20 || delta > 64) c.parent_unknown=true;
                else { c.parent_raw=stack.at(-1).raw_id; c.parent_source='visible_indentation'; c.nested=true; }
            } else if (c.indent > 24) c.parent_unknown=true;
            stack.push(c); stacks.set(c.column,stack);
        }
        const more = expanders(card);
        const collapsedPost = collapsed(bodyCard);
        return {text:text.trim(), card_text:card.innerText, post_url:link ? link.href : location.href,
            post_urn:card.getAttribute('data-urn'), comments, complete:!!text.trim() && !more.length && !collapsedPost && !comments.some(c=>c.collapsed || c.parent_unknown),
            missing_context:more.length ? 'More comments or replies are not loaded' : (collapsedPost ? 'Post is collapsed' : comments.some(c=>c.parent_unknown) ? 'Reply parent is ambiguous' : comments.some(c=>c.collapsed) ? 'Comment is collapsed' : '')};
    });
}"""


class LinkedInThreadBrowser:
    def __init__(self, manager):
        self.manager = manager

    def page(self, read_current=False):
        browser = self.manager.browser_manager
        if not browser.is_healthy():
            result = browser.connect_cdp(cdp_url=self.manager.cdp_url, timeout=5000)
            if result.get("status") != "success":
                raise RuntimeError("Existing browser unavailable; no new profile was launched")
        pages = [p for p in browser.context.pages if urlparse(p.url).hostname in ("www.linkedin.com", "linkedin.com")]
        if read_current:
            visible = [p for p in pages if p.evaluate("() => document.visibilityState") == "visible"]
            if len(visible) == 1:
                return visible[0]
            if len(pages) != 1:
                raise RuntimeError("Current LinkedIn tab is ambiguous")
            return pages[0]
        cached = getattr(browser, "_linkedin_followup_page", None)
        if cached and not cached.is_closed() and cached in browser.context.pages:
            browser._page = cached
            return cached
        owned = [p for p in browser.context.pages if p.evaluate("() => window.name") == "sabrina-linkedin-followups"]
        if len(owned) > 1:
            raise RuntimeError("Multiple follow-up tabs; cannot choose safely")
        if owned:
            page = owned[0]
        else:
            page = browser.context.new_page()
            page.evaluate("() => window.name = 'sabrina-linkedin-followups'")
        browser._page = page
        browser._linkedin_followup_page = page
        return page

    def verify_account(self, page, expected=None):
        if expected:
            account = profile_id(expected)
            if not account:
                raise ValueError("Invalid account profile URL")
            if page.url.rstrip('/') != account.rstrip('/'):
                result = self.manager.stealth.safe_navigate(page, account)
                if result.get("status") != "success":
                    raise RuntimeError(str(result))
        links = page.locator('a[href*="/edit/intro/"]')
        links.first.wait_for(state="attached", timeout=10000)
        accounts = {profile_id(re.sub(r"edit/intro/.*$", "", links.nth(i).get_attribute("href") or ""))
                    for i in range(links.count())}
        accounts.discard(None)
        if len(accounts) != 1:
            raise RuntimeError("Cannot verify account ownership; open your own profile first")
        actual = accounts.pop()
        if expected and actual != profile_id(expected):
            raise RuntimeError("Browser account changed")
        self.manager.browser_manager._linkedin_account_id = actual
        if page is getattr(self.manager.browser_manager, "_linkedin_followup_page", None):
            page.evaluate("() => window.name = 'sabrina-linkedin-followups'")
        return actual

    def capture(self, page, account_id, root_id=None, own_comment=None):
        results = []
        for raw in page.evaluate(CAPTURE_JS):
            post_id, post_url = post_identity(raw.get("post_url", ""))
            if not post_id:
                urn = raw.get("post_urn", "") or ""
                if re.fullmatch(r"urn:li:activity:\d+", urn):
                    post_id, post_url = post_identity("https://www.linkedin.com/feed/update/" + urn + "/")
            if not post_id:
                resources = set()
                for c in raw.get("comments", []):
                    match = re.search(r"urn:li:comment:\(((?:ugcPost|activity|share):\d+),", c.get("raw_id", ""))
                    if match:
                        resources.add(match[1])
                if len(resources) == 1:
                    post_id, post_url = post_identity("https://www.linkedin.com/feed/update/urn:li:" + resources.pop() + "/")
            messages = []
            missing = False
            for c in raw.get("comments", []):
                cid = comment_identity(c.get("raw_id")) or comment_identity(c.get("url"))
                author = profile_id(c.get("author_id", ""))
                parent = None if c.get('parent_unknown') else comment_identity(c.get("parent_raw")) if c.get("nested") or c.get("parent_raw") else post_id
                if not cid or not author or not c.get("text") or not parent:
                    missing = True
                    continue
                messages.append({"id": cid, "parent_id": parent, "author_id": author,
                                 "author": c.get("author", ""), "text": c["text"], "url": c.get("url") or post_url,
                                 "mentions": sorted({profile_id(u) for u in c.get('mentions', []) if profile_id(u)}),
                                 "parent_source": c.get('parent_source', 'dom')})
                marker = hashlib.sha256(cid.encode()).hexdigest()
                page.locator('[data-sabrina-comment-index="' + c["dom_index"] + '"]').evaluate(
                    "(e,key)=>e.setAttribute('data-sabrina-comment-id',key)", marker)
            roots = [m for m in messages if m["author_id"] == account_id and
                     ((root_id and m["id"] == root_id) or (own_comment and m["text"] == own_comment))]
            if not roots:
                if own_comment and own_comment in raw.get("card_text", ""):
                    results.append({"account_id": account_id, "post_id": post_id, "post_url": post_url,
                                    "post_text": raw.get("text", ""), "root_id": None,
                                    "own_comment": own_comment, "messages": messages, "complete": False,
                                    "missing_context": "Own comment visible, but stable comment identity could not be recovered"})
                continue
            for root in roots:
                results.append({"account_id": account_id, "post_id": post_id, "post_url": post_url,
                                "post_text": raw.get("text", ""), "root_id": root["id"],
                                "own_comment": root["text"], "messages": messages,
                                "complete": bool(raw.get("complete") and not missing and post_id),
                                "missing_context": raw.get("missing_context") or ("Missing stable message identities" if missing else "")})
        return results

    def load(self, thread):
        page = self.page()
        self.verify_account(page, thread["account_id"])
        url = thread["snapshot"].get("post_url")
        if not post_identity(url or "")[0]:
            raise RuntimeError("Thread has no stable LinkedIn post URL")
        nav = self.manager.stealth.safe_navigate(page, url)
        if nav.get("status") != "success":
            raise RuntimeError(str(nav))
        try:
            page.wait_for_function("root => Array.from(document.querySelectorAll('[componentkey],[data-id],[data-urn]')).some(e => [e.getAttribute('componentkey'),e.getAttribute('data-id'),e.getAttribute('data-urn')].some(v => v && v.includes(root)))", arg=thread["snapshot"]["root_id"], timeout=10000)
        except Exception:
            button = page.get_by_role("button", name="Comment", exact=True)
            if button.count() == 1:
                button.click(timeout=5000)
            page.wait_for_function("root => Array.from(document.querySelectorAll('[componentkey],[data-id],[data-urn]')).some(e => [e.getAttribute('componentkey'),e.getAttribute('data-id'),e.getAttribute('data-urn')].some(v => v && v.includes(root)))", arg=thread["snapshot"]["root_id"], timeout=10000)
        more = page.locator('button').filter(has_text=re.compile(r"^…\s*more$", re.I))
        for i in range(min(more.count(), 3)):
            if more.nth(i).is_visible():
                more.nth(i).click(timeout=3000)
                break
        # Expand only reading controls. Bounded expansion never clicks Like/Reply/Submit.
        for _ in range(12):
            controls = page.get_by_role("button", name=re.compile(
                r"^(?:(?:Load|Show|See|View) (?:\d+ )?more (?:comments|replies)|See \d+ replies)$", re.I))
            visible = [controls.nth(i) for i in range(controls.count()) if controls.nth(i).is_visible()]
            if not visible:
                break
            visible[0].click(timeout=3000)
            page.wait_for_timeout(200)
        matches = self.capture(page, thread["account_id"], root_id=thread["snapshot"]["root_id"])
        matches = [s for s in matches if s["post_id"] == thread["snapshot"]["post_id"]]
        if len(matches) != 1:
            raise RuntimeError("Original own comment not uniquely located; thread may be unloaded or deleted")
        return matches[0]

    def submit(self, thread, decision):
        from tools.linkedin_conversations import digest
        page = self.page()
        target = decision["target_id"]
        marker = hashlib.sha256(target.encode()).hexdigest()
        card = page.locator('[data-sabrina-comment-id="' + marker + '"]')
        if card.count() != 1:
            raise RuntimeError("Target comment not unique")
        if self.manager.stealth.check_checkpoint(page)[0]:
            raise RuntimeError("Authentication challenge")
        def direct(locators):
            return [locators.nth(i) for i in range(locators.count()) if locators.nth(i).is_visible()
                    and locators.nth(i).evaluate("(e,key)=>e.closest('[data-sabrina-comment-id]')?.getAttribute('data-sabrina-comment-id')===key", marker)]
        editors = direct(card.locator('[contenteditable="true"][role="textbox"]'))
        if not editors:
            buttons = direct(card.get_by_role("button", name="Reply", exact=True))
            if len(buttons) != 1:
                raise RuntimeError("Exact-parent Reply button is ambiguous")
            buttons[0].click(timeout=5000)
            card.locator('[contenteditable="true"][role="textbox"]').first.wait_for(state="visible", timeout=5000)
            editors = direct(card.locator('[contenteditable="true"][role="textbox"]'))
        if len(editors) != 1 or editors[0].inner_text().strip():
            raise RuntimeError("Ambiguous editor or existing human draft; preserved")
        editors[0].fill(decision["comment"])
        buttons = direct(card.get_by_role("button", name=re.compile(r"^(Post reply|Reply|Post)$", re.I)))
        buttons = [button for button in buttons if button.is_enabled() and button.inner_text().strip()]
        if len(buttons) != 1:
            raise RuntimeError("Exact-parent submit button is ambiguous")
        current = self.capture(page, thread["account_id"], root_id=thread["snapshot"]["root_id"])
        if len(current) != 1 or digest(current[0]) != thread["version"]:
            raise RuntimeError("Conversation changed immediately before submission")
        buttons[0].click(timeout=5000)
        # Re-capture platform identity, not just an emptied composer. Never resend on timeout.
        for _ in range(10):
            page.wait_for_timeout(500)
            snapshots = self.capture(page, thread["account_id"], root_id=thread["snapshot"]["root_id"])
            matches = [m for s in snapshots for m in s["messages"] if m["author_id"] == thread["account_id"]
                       and m["parent_id"] == target and m["text"] == decision["comment"] and m.get("url")
                       and m["id"] not in {old["id"] for old in thread["snapshot"]["messages"]}]
            if len(matches) == 1:
                m = matches[0]
                return {"status": "sent", "reply_id": m["id"], "parent_id": target,
                        "reply_url": m["url"], "reply_text": m["text"], "author_id": m["author_id"]}
        return {"status": "uncertain", "reason": "No uniquely verified reply identity; no automatic resend"}
