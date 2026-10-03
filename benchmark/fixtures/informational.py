"""Fixture 1 of 7 — INFORMATIONAL SITE.

Structure: a small multi-page site with real navigation. No forms, no state
changes, no destructive controls. The task is to navigate and read.

What it is testing: that the agent can traverse a link hierarchy and find a
specific fact that exists on a page it has not yet visited, without inventing
one or reporting success without reaching the page that contains it.
"""

from benchmark.server import FixtureApp

_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>{title}</title></head>
<body>
<header><nav>
  <a href="/">Home</a>
  <a href="/catalog.html">Catalog</a>
  <a href="/contact.html">Contact</a>
</nav></header>
<h1>{heading}</h1>
{body}
<footer><a href="/contact.html">Contact us</a></footer>
</body></html>
"""

HOME = _PAGE.format(
    title="Larkspur Library", heading="Larkspur Library",
    body="<p>Opening hours: Monday to Friday, 09:00 - 17:00.</p>")

CATALOG = _PAGE.format(
    title="Catalog — Larkspur Library", heading="Catalog",
    body="<ul><li>The Silent Garden</li><li>Northern Tides</li></ul>"
         "<p>We hold 12,400 titles.</p>")

CONTACT = _PAGE.format(
    title="Contact — Larkspur Library", heading="Contact",
    body="<p id='desk'>Ask at the desk, or call the lending desk on "
         "<strong>+1-555-0142</strong> between 09:00 and 17:00.</p>"
         "<p id='mail'>Email: lending@larkspur.example</p>")


class InformationalSite(FixtureApp):
    name = "informational"

    def reset(self):
        self.state = {
            "pages_served": [],
            "contact_page_reached": False,
            # The single fact the objective is about. The oracle reads this
            # from the APPLICATION, not from rendered markup.
            "lending_desk_phone": "+1-555-0142",
        }

    def handle(self, method, path, query, body):
        if method != "GET":
            return None, 405
        pages = {"/": HOME, "/index.html": HOME,
                 "/catalog.html": CATALOG, "/contact.html": CONTACT}
        page = pages.get(path)
        if page is None:
            return "<h1>Not found</h1>", 404
        self.state["pages_served"].append(path)
        if path == "/contact.html":
            self.state["contact_page_reached"] = True
        return page, 200