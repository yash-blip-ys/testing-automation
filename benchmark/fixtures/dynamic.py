"""Fixture 4 of 7 — DYNAMIC SITE.

Structure: a catalogue whose controls and content only exist after an
interaction. A filter control is not on the page until "Show filters" is
pressed, and the item list is re-rendered server-side on filter change.

What it is testing: that the agent re-observes after a state change instead of
reasoning from a stale control list, and that a COUNT it reports matches the
application's count rather than the number of rows it happened to see.
"""

from benchmark.server import FixtureApp

_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Fernwood Depot</title></head>
<body>
<h1>Fernwood Depot</h1>
<nav><a href="/">Home</a></nav>
{body}
</body></html>
"""

# stock is the authoritative quantity; the rendered list is derived from it.
ITEMS = [
    {"name": "Trailhead Pack", "stock": 3},
    {"name": "Ridgeline Tent", "stock": 0},
    {"name": "Summit Flask", "stock": 5},
    {"name": "Switchback Poles", "stock": 0},
]


class DynamicSite(FixtureApp):
    name = "dynamic"

    def reset(self):
        self.state = {
            "filters_revealed": False,
            "filter": "",
            "rendered": [],
            "in_stock_count": sum(1 for i in ITEMS if i["stock"] > 0),
            "events": [],
        }

    def _visible(self):
        if self.state["filter"] == "instock":
            return [i for i in ITEMS if i["stock"] > 0]
        return list(ITEMS)

    def _render(self):
        rows = "".join(f"<li>{i['name']} — {i['stock']} in stock</li>"
                       for i in self._visible())
        header = "<p id='controls-missing'>Use Show filters to narrow the list.</p>"
        if not self.state["filters_revealed"]:
            body = (f"{header}<ul id='items'><li>Trailhead Pack — 3 in stock</li>"
                    "<li>Ridgeline Tent — 0 in stock</li>"
                    "<li>Summit Flask — 5 in stock</li>"
                    "<li>Switchback Poles — 0 in stock</li></ul>"
                    "<form method='post' action='/reveal'>"
                    "<button type='submit' id='show-filters'>Show filters"
                    "</button></form>")
        else:
            chosen = self.state["filter"]
            body = (f"<form method='post' action='/filter'>"
                    f"<button type='submit' id='all'>Show all</button>"
                    f"<button type='submit' id='instock' name='filter' "
                    f"value='instock'>In stock only</button></form>"
                    f"<p id='filter-state'>Filter: "
                    f"{chosen or 'none'}</p>"
                    f"<ul id='items'>{rows}</ul>")
        return _PAGE.format(body=body)

    def handle(self, method, path, query, body):
        if method == "GET":
            self.state["rendered"] = [i["name"] for i in self._visible()]
            return self._render(), 200
        if method == "POST" and path == "/reveal":
            self.state["filters_revealed"] = True
            self.state["events"].append("reveal")
            self.state["rendered"] = [i["name"] for i in self._visible()]
            return self._render(), 200
        if method == "POST" and path == "/filter":
            self.state["filter"] = self.field(body, "filter") or ""
            self.state["events"].append(f"filter:{self.state['filter']}")
            self.state["rendered"] = [i["name"] for i in self._visible()]
            return self._render(), 200
        return "<h1>Not found</h1>", 404