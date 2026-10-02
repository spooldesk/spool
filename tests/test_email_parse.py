"""Reading email, which is where a mail channel is actually won or lost.

Fetching is plumbing. What decides whether an operator can work is whether
the ticket contains the sentence the customer wrote, or that sentence buried
under four screens of their own quoted history in an encoding nobody chose.

Every message below is built the way a real client builds one — multipart,
encoded headers, base64 bodies — rather than by handing the parser a string
it would never see.
"""

from email.message import EmailMessage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import pytest

from src.channels import email_parse as P


def _mail(*, subject="Help", frm="Ann Fowler <ann@shop.example>",
          text=None, html=None, headers=None) -> EmailMessage:
    m = EmailMessage()
    m["Subject"] = subject
    m["From"] = frm
    m["To"] = "support@ours.example"
    for key, value in (headers or {}).items():
        m[key] = value
    if text is not None and html is not None:
        m.set_content(text)
        m.add_alternative(html, subtype="html")
    elif html is not None:
        m.set_content(html, subtype="html")
    else:
        m.set_content(text or "")
    return m


# ------------------------------------------------------------------ headers


def test_encoded_headers_come_out_as_words():
    """`=?UTF-8?B?…?=` is an ordinary Subject line, and putting it in a queue
    as written is how a ticket list fills with mojibake."""
    m = _mail(subject="=?UTF-8?B?0J/RgNC40LLQtdGC?=",
              frm="=?UTF-8?B?0JDQvdC90LA=?= <anna@shop.example>", text="hi")
    parsed = P.parse(m)
    assert parsed.subject == "Привет"
    assert parsed.from_name == "Анна"
    assert parsed.from_address == "anna@shop.example"


def test_reply_to_wins_over_from():
    """That is what the header is for — a sender saying "answer elsewhere".
    Ignoring it means replying into the void."""
    m = _mail(frm="noreply@shop.example",
              headers={"Reply-To": "Ann <ann@shop.example>"}, text="hi")
    parsed = P.parse(m)
    assert parsed.from_address == "ann@shop.example"


def test_the_address_is_lowercased():
    """Or the same person becomes two customers depending on how their client
    felt about capitals that morning."""
    assert P.parse(_mail(frm="Ann <Ann@Shop.Example>", text="hi")).from_address \
        == "ann@shop.example"


def test_references_are_newest_first_and_deduplicated():
    m = _mail(text="hi", headers={
        "In-Reply-To": "<parent@ours.example>",
        "References": "<root@ours.example> <parent@ours.example>",
    })
    assert P.parse(m).references == ["<parent@ours.example>", "<root@ours.example>"]


# --------------------------------------------------------------------- body


def test_a_plain_part_is_preferred_over_html():
    m = _mail(text="the plain one", html="<p>the html one</p>")
    assert "plain one" in P.parse(m).text
    assert "html one" not in P.parse(m).text


def test_html_only_mail_is_still_readable():
    """Plenty of mail has no plain part at all, and a ticket saying nothing is
    not an acceptable outcome."""
    m = _mail(html="<p>The card was <b>declined</b>.</p><p>Order 4417.</p>")
    text = P.parse(m).text
    assert "The card was declined." in text
    assert "Order 4417." in text
    assert "<" not in text


def test_script_and_style_contents_are_dropped_not_escaped():
    """Their text is never something an operator wants to read, and the only
    reason a support email contains a script is one worth not pasting."""
    m = _mail(html="<style>.x{color:red}</style><script>alert(1)</script><p>Hello</p>")
    text = P.parse(m).text
    assert text == "Hello"
    assert "alert" not in text and "color:red" not in text


def test_a_cyrillic_windows_1251_body_is_decoded():
    """The charset the sender's decade preferred, not ours."""
    outer = MIMEMultipart()
    outer["Subject"] = "Проблема"
    outer["From"] = "ivan@shop.example"
    body = "Не приходит письмо".encode("cp1251")
    part = MIMEText(body, "plain", "windows-1251")
    del part["Content-Transfer-Encoding"]
    part.set_payload(body)
    part["Content-Transfer-Encoding"] = "8bit"
    outer.attach(part)
    assert "Не приходит письмо" in P.parse(outer).text


# ------------------------------------------------------------------- quotes


def test_the_quoted_history_is_cut_off():
    """The whole reason this module exists. Without it the third message in a
    thread is four screens of our own words with "yes, thanks" on top."""
    m = _mail(text=(
        "Yes, that worked — thank you!\n"
        "\n"
        "On Tue, 3 Jun 2025 at 14:02, Support <support@ours.example> wrote:\n"
        "> Have you tried a different card?\n"
        ">\n"
        "> > The payment failed again\n"
    ))
    parsed = P.parse(m)
    assert parsed.text == "Yes, that worked — thank you!"
    # And the original survives, because the trimming is a heuristic and the
    # day it eats something important the evidence has to still exist.
    assert "Have you tried a different card?" in parsed.full_text


@pytest.mark.parametrize("marker", [
    "> quoted",
    "-----Original Message-----",
    "----- Forwarded message -----",
    "________________________",
    "On Mon, 2 Jun 2025, Ann wrote:",
    "Am 02.06.2025 schrieb Ann:",
    "В вт, 3 июн. 2025 г. Ann написал:",
    "From: support@ours.example",
])
def test_every_client_says_it_differently(marker):
    m = _mail(text=f"My actual question.\n\n{marker}\nold stuff here\n")
    assert P.parse(m).text == "My actual question."


def test_a_signature_is_dropped():
    m = _mail(text="Please cancel it.\n\n-- \nAnn Fowler\nHead of Nothing\n")
    assert P.parse(m).text == "Please cancel it."


def test_a_message_that_is_only_quotes_is_flagged_empty():
    """An autoresponder bouncing our own text back. Opening a ticket for it
    would be the helpdesk talking to itself."""
    m = _mail(text="> we replied\n> with something\n")
    parsed = P.parse(m)
    assert parsed.text == ""
    assert parsed.is_empty is True


def test_but_a_quote_carrying_a_file_is_not_empty():
    """"See attached" with the "see attached" trimmed away is still a customer
    sending you the thing you asked for."""
    m = _mail(text="> you asked for the invoice\n")
    m.add_attachment(b"%PDF-1.4", maintype="application", subtype="pdf",
                     filename="invoice.pdf")
    parsed = P.parse(m)
    assert parsed.is_empty is False
    assert [a.filename for a in parsed.attachments] == ["invoice.pdf"]


# --------------------------------------------------------------- attachments


def test_attachments_come_through_with_their_names():
    m = _mail(text="Screenshot attached.")
    m.add_attachment(b"\x89PNG\r\n\x1a\n", maintype="image", subtype="png",
                     filename="=?UTF-8?B?0YHQutGA0LjQvS5wbmc=?=")
    got = P.parse(m).attachments
    assert len(got) == 1
    assert got[0].filename == "скрин.png"
    assert got[0].mime_type == "image/png"
    assert got[0].content.startswith(b"\x89PNG")


def test_an_oversized_attachment_refuses_processing_without_losing_input(monkeypatch):
    """The inbox retains raw mail for retry; the parser must not drop a part."""
    monkeypatch.setattr(P, "MAX_ATTACHMENT_BYTES", 32)
    m = _mail(text="here you go")
    m.add_attachment(b"x" * 4096, maintype="application", subtype="octet-stream",
                     filename="big.bin")
    with pytest.raises(ValueError, match="attachment exceeds"):
        P.parse(m)


def test_a_very_long_body_is_capped(monkeypatch):
    monkeypatch.setattr(P, "MAX_BODY_CHARS", 100)
    m = _mail(text="word " * 500)
    assert len(P.parse(m).full_text) <= 100


# -------------------------------------------------------------------- misc


def test_a_message_with_nothing_in_it_does_not_explode():
    """Malformed mail is not rare, and a parser that raises takes the polling
    loop down with it."""
    bare = EmailMessage()
    parsed = P.parse(bare)
    assert parsed.from_address == ""
    assert parsed.is_empty is True
