"""Fixture 2 of 7 — FORM-HEAVY SITE.

Structure: one page carrying several forms with server-side validation. The
application genuinely rejects bad input and accepts good input, and both
outcomes are recorded server-side.

What it is testing: that the agent can enter data, distinguish a REJECTED
submission from an ACCEPTED one, and that a validation error is not mistaken
for completion. The decoy here is a success banner that is present on the page
before anything is submitted — a naive text match would call this done at step 0.
"""

from urllib.parse import urlencode

from benchmark.server import FixtureApp

_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>{title}</title></head>
<body>
<h1>Kestrel Data Desk</h1>

<!-- Present from first paint. A verifier that matches page text without
     requiring a state change would call this whole task done immediately. -->
<div id="banner">Support portal: all systems operational. Requests accepted.</div>

<nav><a href="/">Home</a></nav>

<form id="signup" method="post" action="/signup">
  <label for="email">Email address</label>
  <input id="email" name="email" type="text" placeholder="name@example.com">
  <button type="submit" id="create">Create account</button>
</form>

<div id="feedback">{feedback}</div>
</body></html>
"""


def _page(feedback=""):
    return _PAGE.format(title="Kestrel Data Desk", feedback=feedback)


class FormHeavySite(FixtureApp):
    name = "form_heavy"

    # Deliberately simple and explicit. The agent has no site knowledge and
    # must not need any.
    VALID = "ada@example.com"
    INVALID = "not-an-email"

    def reset(self):
        self.state = {
            "submissions": [],
            "invalid_seen": False,
            "valid_seen": False,
            "account_created": False,
            "created_email": "",
            # The decoy text is on the page from the start; an oracle must not
            # treat its presence as evidence that anything happened.
            "banner_present_from_start": True,
        }

    def handle(self, method, path, query, body):
        if path == "/" and method == "GET":
            return _page(), 200
        if path == "/signup" and method == "POST":
            email = self.field(body, "email").strip()
            record = {"email": email, "accepted": False}
            if "@" not in email or "." not in email.split("@")[-1]:
                feedback = ("<p id='error'>That does not look like an email "
                            "address. Nothing was created.</p>")
            elif email == self.INVALID:
                feedback = ("<p id='error'>That address is already blocked. "
                            "Nothing was created.</p>")
            else:
                record["accepted"] = True
                self.state["account_created"] = True
                self.state["created_email"] = email
                self.state["valid_seen"] = True
                feedback = ("<p id='success'>Account created. A confirmation "
                            "was sent to the address you supplied.</p>")
            self.state["submissions"].append(record)
            if not record["accepted"]:
                self.state["invalid_seen"] = True
            # Post/redirect/get would hide the result from the very observer
            # that most needs to see it, so the response re-renders directly.
            return _page(feedback), 200
        return "<h1>Not found</h1>", 404


FORM_CONFIG_TEMPLATE = {
    "site_name": "Benchmark - Form Heavy",
    "credentials": {},
    "auth": {},
    "browser": {"headless": True},
    "victory_conditions": {},
    "form_autofill": [],
    "target_elements_query": ("button, a, [role='button'], input, select, "
                              "textarea"),
}