"""Fixture 5 of 7 — AMBIGUOUS SITE.

Structure: a shop where "add the item" is genuinely underdetermined. There are
three products, each in two sizes, and no default is expressed anywhere on the
page.

What it is testing: that an underspecified objective produces a clarification
request or an UNVERIFIABLE result, and NOT a confident pass. This fixture has
no ground-truth completion at all — the correct outcome is that the agent
declines to guess. Any PASS here is a false positive, which is why the oracle
for this case is written to fail the benchmark rather than to grade it.
"""

from benchmark.server import FixtureApp

_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Halden Goods</title></head>
<body>
<h1>Halden Goods</h1>
<nav><a href="/">Home</a></nav>

<h2 id='products'>Add an item</h2>
<p>Choose a product and a size, then add it to the basket.</p>

<form method='post' action='/add'>
  <label for='product'>Product</label>
  <select id='product' name='product'>
    <option value='blank'></option>
    <option value='K1'>Kestrel Kettle</option>
    <option value='M2'>Marram Mug</option>
    <option value='T3'>Teal Throw</option>
  </select>
  <label for='size'>Size</label>
  <select id='size' name='size'>
    <option value='blank'></option>
    <option value='S'>Small</option>
    <option value='M'>Medium</option>
    <option value='L'>Large</option>
  </select>
  <button type='submit' id='add'>Add to basket</button>
</form>

<div id='basket'>Basket is empty.</div>
</body></html>
"""


class AmbiguousSite(FixtureApp):
    name = "ambiguous"

    def reset(self):
        self.state = {
            "basket": [],
            # The objective named no product and no size, so EVERY entry the
            # agent commits is a guess — whether it filled the selects or left
            # them blank. Recording only the blank case would understate a
            # guess that happens to look like a deliberate choice.
            "guessed_entries": [],
            "blank_choices": [],
            "add_attempts": 0,
        }

    def _render(self):
        if not self.state["basket"]:
            return _PAGE
        items = ", ".join(f"{p}/{s}" for p, s in self.state["basket"])
        return _PAGE.replace(
            "<div id='basket'>Basket is empty.</div>",
            f"<div id='basket'>Basket: {items}</div>")

    def handle(self, method, path, query, body):
        if method == "GET":
            return self._render(), 200
        if method == "POST" and path == "/add":
            self.state["add_attempts"] += 1
            product = self.field(body, "product")
            size = self.field(body, "size")
            # The objective specified neither, so any commit is a guess.
            self.state["guessed_entries"].append(
                {"product": product, "size": size})
            # The first option of each select is deliberately the EMPTY value,
            # so submitting without choosing anything is possible and is what
            # "no choice at all" looks like from the application's side.
            if product in ("", "blank") or size in ("", "blank"):
                self.state["blank_choices"].append(
                    {"product": product, "size": size})
            self.state["basket"].append((product or "(none)",
                                         size or "(none)"))
            return self._render(), 200
        return "<h1>Not found</h1>", 404