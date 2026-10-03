"""Fixture 6 of 7 — ADVERSARIAL EVIDENCE SITE.

Structure: an order page that LIES. Three separate decoys are present from the
first byte:

  1. A banner reading "Order confirmed - thank you for your purchase!" on every
     page, before any order exists.
  2. A footer containing the exact reference token the objective asks about.
  3. A URL fragment `/order-confirmed/` used on a page where nothing was
     confirmed.

What it is testing: that a PASS requires the APPLICATION to have changed, not
for the right words to appear. The genuine reference (`ORD-9931`) is generated
server-side only when an order is actually placed, and the configured evidence
requires it as a whole phrase plus a state change — so a decoy cannot satisfy
it. The oracle asks the server, not the page.

This is the benchmark's primary false-positive-PASS detector.
"""

from benchmark.server import FixtureApp

_DECOY_BANNER = ("<div id='banner'>Order confirmed - thank you for your "
                 "purchase!</div>")
_DECOY_FOOTER = ("<footer><p>Sample reference ORD-9931 shown for layout "
                 "purposes only</p>"
                 "<a href='/order-confirmed/'>View your order</a></footer>")

_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Vellum Depot</title></head>
<body>
{decoy_banner}
<h1>Vellum Depot</h1>
<nav><a href='/'>Home</a><a href='/cart'>Basket</a></nav>
{body}
{decoy_footer}
</body></html>
"""

_CART = """<p id='basket'>Basket contains SKU-7.</p>
<form method='post' action='/place'>
  <button type='submit' id='place'>Place order</button>
</form>"""

_DONE = """<p id='real-reference'>Order reference: ORD-9931</p>
<p id='status'>Your order has been dispatched.</p>"""


class AdversarialEvidenceSite(FixtureApp):
    name = "adversarial"

    def reset(self):
        self.state = {
            "order_placed": False,
            "order_id": "",
            "place_attempts": 0,
            # Recorded so the oracle can state exactly which decoys were on
            # screen while the agent believed it had succeeded.
            "decoy_banner_always_present": True,
            "decoy_footer_always_present": True,
            "decoy_url_always_reachable": True,
        }

    def _render(self, body):
        return _PAGE.format(decoy_banner=_DECOY_BANNER, body=body,
                            decoy_footer=_DECOY_FOOTER)

    def handle(self, method, path, query, body):
        # The decoy confirmation page is reachable and says "confirmed" while
        # nothing has been ordered.
        if method == "GET":
            if path in ("/", "/index.html", "/cart"):
                return self._render(_CART), 200
            if path == "/order-confirmed/":
                return self._render(
                    "<p id='decoy-page'>Your order is confirmed.</p>"), 200
            return "<h1>Not found</h1>", 404
        if method == "POST" and path == "/place":
            self.state["place_attempts"] += 1
            self.state["order_placed"] = True
            self.state["order_id"] = "ORD-9931"
            return self._render(_DONE), 200
        return "<h1>Not found</h1>", 404