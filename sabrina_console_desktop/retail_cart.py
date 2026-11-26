"""Reconcile browser cart actions after a lost acknowledgement."""

import json
from collections import Counter
from threading import Event


def add_and_reconcile(read_cart, add, variant, quantity, emit):
    before = read_cart()
    expected = Counter(before["quantities"])
    expected[variant] += quantity
    timed_out = False
    try:
        add(variant, quantity)
    except TimeoutError:
        timed_out = True
        emit("CART_TIMEOUT", f"Add receipt timed out for {variant}. Reading cart before any retry.")
    after = read_cart()
    if after["quantities"] != dict(expected):
        emit("CART_BLOCKED", f"Cart quantities could not be reconciled for {variant}. Add was not replayed.")
        raise RuntimeError(f"Cart quantities could not be reconciled for {variant}.")
    if timed_out:
        emit("CART_RECOVERED", f"Verified {variant} × {expected[variant]} in cart. No duplicate Add click.")
    return after


def replay_catalog(lines, emit, cancel_event: Event):
    """Run the catalog through an isolated browser cart with synthetic prices."""
    from playwright.sync_api import sync_playwright

    expected = {line.product.internet_number: line.quantity for line in lines}
    prices = {variant: 100 * (i + 1) for i, variant in enumerate(expected)}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", timeout=15000)
        try:
            context = browser.new_context(service_workers="block", offline=True)
            context.route("**/*", lambda route: route.abort())
            page = context.new_page()
            page.set_content('''<button id="add">Add</button><output id="cart"></output>
                <script>
                const cart = document.querySelector('#cart');
                cart.textContent = '{"quantities":{},"subtotal_cents":0,"clicks":0}';
                const button = document.querySelector('#add');
                button.onclick = () => {
                    const state = JSON.parse(cart.textContent);
                    const {variant, quantity, cents} = button.dataset;
                    state.quantities[variant] = (state.quantities[variant] || 0) + Number(quantity);
                    state.subtotal_cents += Number(quantity) * Number(cents);
                    state.clicks++;
                    cart.textContent = JSON.stringify(state);
                };
                </script>''', timeout=5000)

            button = page.locator("#add")
            def read_cart():
                return json.loads(page.locator("#cart").inner_text(timeout=5000))

            def add(variant, quantity):
                button.evaluate("(button, data) => Object.assign(button.dataset, data)",
                                {"variant": variant, "quantity": quantity, "cents": prices[variant]},
                                timeout=5000)
                button.click(timeout=5000)
                if variant == next(iter(expected)):
                    raise TimeoutError("Injected lost acknowledgement after Add committed")

            for variant, quantity in expected.items():
                if cancel_event.is_set():
                    raise RuntimeError("Cart replay cancelled.")
                add_and_reconcile(read_cart, add, variant, quantity, emit)
            observed = read_cart()
            if (observed["quantities"] != expected or observed["clicks"] != len(expected)
                    or observed["subtotal_cents"] != sum(prices[k] * q for k, q in expected.items())):
                raise RuntimeError("Browser cart totals or Add click count did not verify.")
            emit("CART_VERIFIED", f"Offline browser verified {len(expected)} variants and exactly "
                 f"{observed['clicks']} Add clicks. Checkout disabled.")
            return {**observed, "backend": "Edge offline fixture", "prices_synthetic": True,
                    "checkout_enabled": False, "execution_authorized": False}
        finally:
            browser.close()
