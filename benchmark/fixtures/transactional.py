"""Fixture 3 of 7 — MULTI-STEP TRANSACTIONAL SITE.

Structure: a four-screen checkout driven entirely server-side. The browser is
just a renderer; the step index lives in the application state.

What it is testing: the exact confusion Checkpoint 6.1 names — reaching a
meaningful INTERMEDIATE state and reporting it as completion. Every screen
before the last carries a success-sounding line ("Step 2 of 4 complete",
"Shipping saved"), and only the final screen carries the order reference. The
order is placed server-side on the final POST, and re-posting it is recorded so
a double-submit is detectable.
"""

from benchmark.server import FixtureApp

_STYLE = ("<style>body{font-family:sans-serif;margin:2rem;max-width:40rem}"
          "li.done{text-decoration:line-through;color:#888}</style>")

_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>{title}</title>{style}</head>
<body>
<h1>Ridge Outfitters</h1>
<nav><a href="/">Home</a><a href="/cart">Cart</a></nav>
{body}
</body></html>
"""

_STEPS = ["Cart", "Shipping", "Payment", "Review"]


def _stepper(index):
    items = []
    for i, name in enumerate(_STEPS):
        cls = "done" if i < index else ""
        items.append(f"<li class='{cls}'>{i + 1}. {name}</li>")
    return "<ol id='steps'>" + "".join(items) + "</ol>"


class TransactionalSite(FixtureApp):
    name = "transactional"

    ORDER_ID = "ORD-4417"

    def reset(self):
        self.state = {
            "step": 0,
            "cart_item": None,
            "shipping_saved": False,
            "payment_saved": False,
            "order_placed": False,
            "order_id": "",
            "orders_placed": [],
            "screens_shown": [],
        }

    # -- rendering ----------------------------------------------------------

    def _render(self):
        step = self.state["step"]
        progress = _stepper(step)
        if step == 0:
            body = (f"{progress}<p id='cart-empty'>Your cart is empty.</p>"
                    "<form method='post' action='/cart'>"
                    "<input name='sku' type='text' value='SKU-1'>"
                    "<button type='submit' id='add'>Add Ridge Tent</button>"
                    "</form>")
        elif step == 1:
            body = (f"{progress}<p id='cart'>Cart: {self.state['cart_item']}</p>"
                    "<form method='post' action='/shipping'>"
                    "<input name='city' type='text' placeholder='City'>"
                    "<button type='submit' id='save-shipping'>Save shipping"
                    "</button></form>")
        elif step == 2:
            body = (f"{progress}<p id='shipping'>Shipping to "
                    f"{self.state.get('city', '')}. Saved.</p>"
                    "<form method='post' action='/payment'>"
                    "<input name='method' type='text' value='invoice'>"
                    "<button type='submit' id='save-payment'>Save payment"
                    "</button></form>")
        else:
            body = (f"{progress}<p id='review'>Review and confirm.</p>"
                    "<form method='post' action='/place'>"
                    "<button type='submit' id='place'>Place order</button>"
                    "</form>")
        if self.state["order_placed"]:
            body += (f"<p id='confirmation'>Order confirmed. "
                     f"Order reference: {self.state['order_id']}</p>")
        return _PAGE.format(title="Ridge Outfitters", style=_STYLE, body=body)

    def _render_confirmation(self):
        body = ("<p id='confirmation'>Order confirmed. "
                f"Order reference: {self.state['order_id']}</p>"
                "<p>Thank you. A receipt has been sent.</p>")
        return _PAGE.format(title="Ridge Outfitters", style=_STYLE, body=body)

    # -- routing -----------------------------------------------------------

    def handle(self, method, path, query, body):
        self.state["screens_shown"].append(path)
        if method == "GET":
            if path in ("/", "/index.html", "/cart", "/shipping", "/payment",
                        "/review"):
                return self._render(), 200
            if path == "/confirmation":
                return self._render_confirmation(), 200
            return "<h1>Not found</h1>", 404

        if method == "POST":
            if path == "/cart":
                self.state["cart_item"] = self.field(body, "sku") or "SKU-1"
                self.state["step"] = 1
                return self._render(), 200
            if path == "/shipping":
                self.state["city"] = self.field(body, "city")
                self.state["shipping_saved"] = True
                self.state["step"] = 2
                return self._render(), 200
            if path == "/payment":
                self.state["method"] = self.field(body, "method")
                self.state["payment_saved"] = True
                self.state["step"] = 3
                return self._render(), 200
            if path == "/place":
                # The commit happens HERE and only here. A second POST is
                # recorded separately so an oracle can detect a double order.
                if self.state["order_placed"]:
                    self.state["duplicate_place_attempts"] = \
                        self.state.get("duplicate_place_attempts", 0) + 1
                else:
                    self.state["order_placed"] = True
                    self.state["order_id"] = self.ORDER_ID
                    self.state["orders_placed"].append(self.ORDER_ID)
                return self._render_confirmation(), 200
            return "<h1>Not found</h1>", 404
        return "<h1>Method not allowed</h1>", 405