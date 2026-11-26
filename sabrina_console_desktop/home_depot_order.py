"""Prepare a Home Depot order draft for the compact outdoor dog-house plan."""

from __future__ import annotations

import re
from dataclasses import dataclass
from html import escape


MODE = "home_depot_order_draft"
REPLAY_PREFIX = "Offline cart replay: "
LOCAL_MODEL = "Built-in materials catalog"
PLAN_URL = "https://www.homedepot.com/c/ah/how-to-build-a-diy-dog-house/9ba683603be9fa5395fab90ee228d32"
ROOF_GUIDE_URL = "https://images.thdstatic.com/catalog/pdfImages/ef/efe10529-c771-4c8f-89c2-7d98cbd3353d.pdf"
LISTS_URL = "https://www.homedepot.com/list/view/summary"
QUOTE_URL = "https://www.homedepot.com/c/create-a-quote"
CART_URL = "https://www.homedepot.com/cart"
STORE_LOCATOR_URL = "https://www.homedepot.com/l/store-locator"
PLAN_NOTE = (
    "This draft assumes identical, uninsulated outdoor dog houses with a 23 × 26 in. floor. "
    "Confirm that the floor and doorway fit each dog. It assumes you have the tools in the build plan."
)
ROOF_NOTE = (
    "This draft adds 2 × 3 roof supports for the Suntuf panels. Fasten the roof screws into those supports "
    "and check for exposed screw tips inside each house."
)
STORE_NOTE = "Choose your store on Home Depot to check current stock, prices, and pickup or delivery options."

_REQUEST = re.compile(
    r"^(?:sabrina[,:]?\s+)?(?:please\s+)?(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?)?"
    r"(?:create|draft|prepare|build|put together|make|set up|start)\s+"
    r"(?:(?:me\s+)?(?:a|an|my|the)\s+)?(?:the\s+)?home[ -]?depot\s+"
    r"(?:order(?:\s+draft)?|(?:shopping\s+)?cart|materials?\s+list|quote\s+request)\s+(?P<project>.+)$",
    re.IGNORECASE,
)
_DOG_HOUSE = re.compile(r"\bdog[ -]?houses?\b", re.IGNORECASE)
_QUANTITY_WORDS = {
    "a": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}
_PROJECT = re.compile(
    r"^(?:to build|for building|for materials to build|for)\s+"
    rf"(?P<count>[0-9]{{1,6}}|{'|'.join(_QUANTITY_WORDS)})\s+"
    r"dog[ -]?houses?(?:[,]?\s+please)?[\s.!?]*$",
    re.IGNORECASE,
)


def matches_goal(goal: str) -> bool:
    match = _REQUEST.fullmatch(" ".join(goal.removeprefix(REPLAY_PREFIX).split()))
    return bool(match and _DOG_HOUSE.search(match["project"]))


def parse_quantity(goal: str) -> int:
    request = _REQUEST.fullmatch(" ".join(goal.removeprefix(REPLAY_PREFIX).split()))
    project = _PROJECT.fullmatch(request["project"]) if request else None
    if project is None:
        raise ValueError(
            "The Home Depot draft supports a standard compact dog house. "
            "To use that plan, specify the count, such as ‘create a Home Depot order to build 3 dog houses’."
        )
    value = project["count"].lower()
    quantity = int(value) if value.isdecimal() else _QUANTITY_WORDS[value]
    if quantity < 1:
        raise ValueError("Enter a positive number of dog houses.")
    return quantity


@dataclass(frozen=True)
class Product:
    internet_number: str
    name: str
    unit: str
    required_per_house: int
    units_per_purchase: int
    use: str

    @property
    def url(self) -> str:
        return f"https://www.homedepot.com/p/{self.internet_number}"


@dataclass(frozen=True)
class OrderLine:
    product: Product
    quantity: int

    @property
    def display_unit(self) -> str:
        unit = self.product.unit
        if self.quantity == 1:
            return unit
        return unit + ("es" if unit.endswith("box") else "s")


# Home Depot US product pages checked on 2026-11-25. The linked build plan provides
# the base quantities. Suntuf's installation guide governs the roof materials.
_PRODUCTS = (
    Product("100061386", "Plytanium 23/32 in. × 4 ft. × 8 ft. BC plywood", "sheet", 1, 1,
            "Floor and roof decking. Seal exposed edges."),
    Product("206935184", "WeatherShield 2 × 4 × 8 ft. ground-contact treated lumber", "board", 2, 1,
            "Raised base."),
    Product("302778103", "2 × 3 × 8 ft. select framing lumber", "board", 11, 1,
            "Eight boards for framing and three for six roof supports per house."),
    Product("203429564", "1 × 3 × 8 ft. cedar board", "board", 1, 1,
            "Door trim."),
    Product("338307861", "1 × 8 × 8 ft. western red cedar board", "board", 5, 1,
            "Shiplap siding."),
    Product("100087758", "Suntuf 8 ft. white opal corrugated roof panel", "panel", 1, 1,
            "Roof covering."),
    Product("100067957", "Suntuf 24 in. horizontal closure strips", "6-pack", 12, 6,
            "Six 35 in. rows per house, joined from two strips per row."),
    Product("100050433", "Suntuf 24 in. vertical closure strips", "6-pack", 4, 6,
            "Four roof side edges per house. Cut to fit between supports."),
    Product("100660853", "Suntuf 4 ft. white opal ridge cap", "cap", 1, 1,
            "Matching ridge flashing."),
    Product("100095071", "Woodtite 2 in. roofing screws with EPDM washers", "50-pack", 50, 50,
            "Roof panels and ridge cap. Fasten into the roof supports."),
    Product("202038072", "Polymershapes clear silicone for plastic sheets", "2.8 oz. tube", 1, 1,
            "Seal the side laps of the polycarbonate roofing."),
    Product("305418743", "Deckmate #9 × 3 in. exterior wood screws", "1 lb. box", 2, 1,
            "Framing and roof decking."),
    Product("305418725", "Deckmate #10 × 3-1/2 in. exterior wood screws", "1 lb. box", 70, 51,
            "Base, wall attachment and roof supports. 51 screws per box."),
    Product("342161357", "Foreverbolt #8 × 1-1/4 in. stainless wood deck screws", "100-pack", 12, 100,
            "Floor attachment, with spares."),
    Product("202308602", "Grip-Rite 4d galvanized 1-1/2 in. finish nails", "1 lb. box", 1, 1,
            "Cedar siding and trim."),
    Product("206667343", "DAP Plastic Wood white latex wood filler", "6 oz. tube", 1, 1,
            "Roof decking edges. Coat with exterior paint after curing."),
    Product("310948305", "Behr Premium Plus Original White flat exterior paint and primer", "1 qt. can", 1, 1,
            "Coating for the plywood surfaces specified in the plan."),
    Product("100085823", "3M ScotchBlue 0.94 in. × 60 yd. painter’s tape", "roll", 240, 2160,
            "Temporarily hold roof closures. Shared across builds."),
)


def build_lines(house_count: int) -> tuple[OrderLine, ...]:
    lines = []
    for product in _PRODUCTS:
        required = house_count * product.required_per_house
        pack = product.units_per_purchase
        lines.append(OrderLine(product, (required + pack - 1) // pack))
    return tuple(lines)


def verify_lines(house_count: int, lines: tuple[OrderLine, ...]) -> None:
    if house_count < 1:
        raise ValueError("A draft needs at least one house.")
    if {line.product for line in lines} != set(_PRODUCTS) or len({
        line.product.internet_number for line in lines
    }) != len(lines):
        raise ValueError("The materials draft contains missing, duplicate, or unrecognized products.")
    for line in lines:
        product = line.product
        required = house_count * product.required_per_house
        pack = product.units_per_purchase
        if (type(line.quantity) is not int or not re.fullmatch(r"[0-9]{9}", product.internet_number)
                or not (line.quantity - 1) * pack < required <= line.quantity * pack):
            raise ValueError(f"Invalid purchase quantity or catalog number for {product.name}.")


def serialize_lines(lines: tuple[OrderLine, ...]) -> list[dict[str, str | int]]:
    return [
        {"internet_number": line.product.internet_number, "name": line.product.name,
         "url": line.product.url, "unit": line.product.unit, "quantity": line.quantity}
        for line in lines
    ]


def project_name(house_count: int) -> str:
    return f"{house_count} dog {'house' if house_count == 1 else 'houses'}"


def render_report(house_count: int, lines: tuple[OrderLine, ...]) -> str:
    materials = "\n".join(
        f"- {line.quantity} {line.display_unit} — {line.product.name} (Internet #{line.product.internet_number})"
        for line in lines
    )
    return (
        f"# Home Depot order draft for {project_name(house_count)}\n\n"
        "Open the order draft to review the materials. Use Copy Pro Desk request to ask Home Depot "
        "for an itemized quote, or use the product links to build a cart.\n\n"
        f"{PLAN_NOTE}\n\n{ROOF_NOTE}\n\n{STORE_NOTE}\n\n{materials}\n"
    )


def render_review(house_count: int, lines: tuple[OrderLine, ...]) -> str:
    project = project_name(house_count)
    material_text = "\n".join(
        f"{line.quantity} {line.display_unit} | {line.product.name} | Internet # {line.product.internet_number}"
        for line in lines
    )
    quote_request = "\n\n".join([
        "Hi Home Depot Pro Desk,",
        f"Could you prepare an itemized quote for the materials below to build {project}? "
        "If available, please send a cart link or quote number I can use to review and order.",
        "Please confirm stock, pickup timing, and delivery options and fees. "
        "Include the total with tax and flag any proposed substitutions with their product numbers. "
        "I’ll review the quote before placing an order.",
        material_text,
        "Thank you.",
    ])
    rows = "\n".join(
        f'<tr><td class="qty">{line.quantity} <span>{escape(line.display_unit)}</span></td>'
        f'<td><a href="{line.product.url}" target="_blank" rel="noopener noreferrer">{escape(line.product.name)}</a>'
        f'<small>Internet # {line.product.internet_number}</small></td><td>{escape(line.product.use)}</td></tr>'
        for line in lines
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Home Depot order draft for {project} | Sabrina Console</title>
<style>{_STYLE}</style>
</head>
<body>
<main>
  <header>
    <p class="eyebrow">Sabrina Console / Home Depot</p>
    <h1>Order draft for {project}</h1>
    <p>{escape(PLAN_NOTE)} <a href="{PLAN_URL}" target="_blank" rel="noopener noreferrer">View build plan</a>.</p>
  </header>
  <section class="handoff" aria-labelledby="pro-desk">
    <h2 id="pro-desk">Ask the Pro Desk for a quote</h2>
    <p>Copy the request with all {len(lines)} product numbers and quantities. Use Home Depot’s store locator
       to find the Pro Desk number for your store and ask where to share the list.</p>
    <div class="actions">
      <button id="copy-quote" data-copy-confirmation="Pro Desk request copied." type="button">Copy Pro Desk request</button>
      <a href="{STORE_LOCATOR_URL}" target="_blank" rel="noopener noreferrer">Find a Pro Desk</a>
    </div>
    <p id="quote-copy-status" role="status"></p>
    <details id="quote-request-text">
      <summary>View quote request</summary>
      <textarea readonly aria-label="Pro Desk quote request" id="quote-request">{escape(quote_request)}</textarea>
    </details>
  </section>
  <section class="handoff" aria-labelledby="handoff">
    <h2 id="handoff">Build your own cart</h2>
    <p>{escape(STORE_NOTE)}</p>
    <p>With Pro Xtra, open Lists, select <strong>Add Materials</strong> and paste the list into the
       <strong>Material List Builder</strong>. Match the Internet numbers and purchase quantities below
       before adding products to your cart or requesting a quote.</p>
    <div class="actions">
      <button id="copy" data-copy-confirmation="Materials copied." type="button">Copy materials list</button>
      <a href="{LISTS_URL}" target="_blank" rel="noopener noreferrer">Open Home Depot Lists</a>
      <a href="{QUOTE_URL}" target="_blank" rel="noopener noreferrer">How to request a quote</a>
    </div>
    <p id="copy-status" role="status"></p>
    <details id="material-text">
      <summary>Show text for pasting</summary>
      <textarea readonly aria-label="Materials list for pasting" id="materials">{escape(material_text)}</textarea>
    </details>
    <p class="manual">You can also add each item from its product page, then
       <a href="{CART_URL}" target="_blank" rel="noopener noreferrer">review your Home Depot cart</a>.</p>
  </section>
  <section aria-labelledby="products">
    <h2 id="products">Materials</h2>
    <p>{escape(ROOF_NOTE)} <a href="{ROOF_GUIDE_URL}" target="_blank" rel="noopener noreferrer">View Suntuf installation guide</a>.</p>
    <div class="table-wrap" role="region" aria-labelledby="products" tabindex="0">
      <table>
        <thead><tr><th scope="col">Buy</th><th scope="col">Product</th><th scope="col">Use</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>
  </section>
</main>
<script>{_COPY_SCRIPT}</script>
</body>
</html>
"""


_STYLE = """
:root { font: 16px/1.5 system-ui, sans-serif; color: #172b2b; background: #f4f5ee; }
body { margin: 0; }
main { max-width: 65rem; margin: auto; padding: clamp(1rem, 5vw, 3.5rem); }
header { border-bottom: 2px solid #1e5146; padding-bottom: 1.125rem; }
h1 { font-size: clamp(2rem, 5vw, 3.65rem); line-height: 1.06; letter-spacing: -.04em; margin: .25em 0; }
h2 { margin: 0 0 .625rem; font-size: 1.3rem; }
p { max-width: 72ch; margin: .5rem 0; }
.eyebrow { color: #416459; font-weight: 650; font-size: .875rem; }
section { margin-top: 1.75rem; }
.handoff { padding: 1.25rem; border: 1px solid #ced9ce; background: #fff; border-radius: .625rem; }
a { color: #185845; text-underline-offset: .18em; }
a:hover { color: #093629; }
.actions { display: flex; flex-wrap: wrap; align-items: center; gap: .75rem 1rem; margin-top: 1rem; }
button { padding: .65rem .9rem; background: #1e5146; color: white; font: inherit; font-weight: 650;
         border: 1px solid transparent; border-radius: .35rem; cursor: pointer; }
button:hover { background: #123b32; }
:focus-visible { outline: 3px solid #2a7660; outline-offset: 2px; }
summary { cursor: pointer; font-size: .9rem; }
textarea { box-sizing: border-box; width: 100%; min-height: 10rem; margin-top: .5rem; font: .875rem/1.45 monospace; }
.manual, small, [role="status"] { color: #4f625d; font-size: .9rem; }
.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; background: #fff; }
th, td { text-align: left; vertical-align: top; padding: .65rem .75rem; border-bottom: 1px solid #d7dfd6; }
th { background: #e2ebe2; }
.qty { white-space: nowrap; font-weight: 700; font-variant-numeric: tabular-nums; }
.qty span { font-weight: 400; }
td small { display: block; }
@media print {
  :root, .handoff, table { background: #fff; }
  main { padding: 0; }
  .actions, details, .manual, [role="status"] { display: none; }
  tr { break-inside: avoid; }
}
"""

_COPY_SCRIPT = """
for (const button of document.querySelectorAll("[data-copy-confirmation]")) {
  const handoff = button.closest(".handoff");
  const details = handoff.querySelector("details");
  const text = details.querySelector("textarea");
  const status = handoff.querySelector('[role="status"]');
  button.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(text.value);
      status.textContent = button.dataset.copyConfirmation;
    } catch {
      details.open = true;
      text.focus();
      text.select();
      status.textContent = "Copy the selected text with Ctrl+C or Command+C.";
    }
  });
}
"""
