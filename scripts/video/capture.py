"""Film the narrated walkthrough's screens from the real static demo (English).

    python scripts/video/capture.py                                  # serves docs/ itself
    python scripts/video/capture.py --base http://localhost:8020     # or film a server you run

Needs Playwright (requirements-screenshots.txt, then `playwright install
chromium`), not the voice environment. Writes build/captures/.

One continuous session, so the story carries over: #61 is answered and noted,
appears under Waiting on the board, drops out of the assistant's list of
tickets that still need a reply. Each capture records the boxes of the elements
the cursor visits, in capture pixels, for render.py.
"""
import argparse
import asyncio
import functools
import json
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.async_api import async_playwright

HERE = Path(__file__).resolve().parent
DOCS = HERE.parents[1] / "docs"
OUT = HERE / "build" / "captures"
VIEW_W, VIEW_H, SCALE = 1280, 720, 2

REPLY = ("Hi Olivia, that means all of your traffic is going through the office "
         "network. Turn on split tunnelling under Settings → VPN and reconnect.")
NOTE = "Olivia is on the Paris office network. Can someone check its VPN profile?"
ASK = "Find active tickets that still need a reply."
PROPOSE = "Move #59 to in progress"


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def serve_docs():
    """docs/ on a free local port, as `python -m http.server -d docs` would."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(DOCS)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


async def main(base: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.glob("*.png"):
        old.unlink()
    manifest = []
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": VIEW_W, "height": VIEW_H + 36},
                                      device_scale_factor=SCALE)
        await page.goto(f"{base}/demo/?lang=en")
        await page.wait_for_timeout(2500)
        bar = await page.evaluate("() => document.querySelector('iframe').getBoundingClientRect().top")

        def fr():
            return page.frames[1]

        async def go(route):
            await page.evaluate(f"() => location.hash = {json.dumps('#' + route.replace('/', '%2F'))}")
            await page.wait_for_timeout(1800)

        async def box(locator):
            b = await locator.bounding_box()
            return {k: round((b[k] - (bar if k == "y" else 0)) * SCALE) for k in ("x", "y", "width", "height")}

        async def shot(name, **targets):
            await page.mouse.move(1, 1)
            await page.wait_for_timeout(250)
            await page.locator("iframe").screenshot(path=str(OUT / f"{name}.png"))
            manifest.append({"name": name, "size": [VIEW_W * SCALE, VIEW_H * SCALE], "targets": targets})
            print("captured", name, list(targets))

        async def dismiss_toasts():
            """The demo labels sample analytics with a toast; it covers the charts."""
            await fr().evaluate("""() => document.querySelectorAll('[role=status] button, .toast button, [class*=toast] button')
                .forEach(b => /×|close|dismiss/i.test(b.innerText + (b.getAttribute('aria-label')||'')) && b.click())""")
            await page.wait_for_timeout(400)

        async def typing(prefix, field, text, every=2, **targets):
            """Fill a field a few words at a time, one capture per step."""
            words = text.split(" ")
            steps = list(range(every, len(words), every))
            for n, k in enumerate(steps, 1):
                await field.fill(" ".join(words[:k]))
                await shot(f"{prefix}-{n:02d}", **targets)

        # 02 — the queue: the filters, then a search that narrows it
        await go("/tickets")
        search = fr().locator("input[type=search], input[placeholder^='Search']").first
        apply = fr().locator("button", has_text="Apply").first
        filters = {k: await box(fr().locator(f"select[name={k}]").first)
                   for k in ("stage", "team_id", "category", "assigned")}
        await shot("02-queue", search=await box(search), apply=await box(apply), **filters)
        await search.fill("vpn")
        await shot("02b-search", search=await box(search), apply=await box(apply))
        await apply.click()
        await page.wait_for_timeout(1800)
        await shot("02c-filtered", row61=await box(fr().locator("tbody tr:not([hidden]) a", has_text="#61").first))

        # 03 — open #61, type, send
        await go("/tickets/61")
        composer = fr().locator("textarea").first
        send = fr().locator("button", has_text="Send").first
        await shot("03a-ticket", composer=await box(composer), send=await box(send))
        await typing("03t", composer, REPLY)
        await composer.fill(REPLY)
        await page.wait_for_timeout(300)
        await shot("03c-reply", composer=await box(composer), send=await box(send))
        await send.click()
        await page.wait_for_timeout(1600)
        note_tab = fr().locator("button", has_text="Note").first
        await shot("03d-sent", send=await box(send), tab=await box(note_tab))

        # 04 — an internal note on the same ticket
        await note_tab.click()
        await page.wait_for_timeout(500)
        await shot("04t-00")
        await typing("04t", composer, NOTE)
        await composer.fill(NOTE)
        await page.wait_for_timeout(300)
        submit = fr().locator("button", has_text="Add note").first
        await shot("04a-note", tab=await box(note_tab), submit=await box(submit))
        await submit.click()
        await page.wait_for_timeout(1600)
        await shot("04b-noted")

        # 05 — the board, #61 now under Waiting for customer
        await go("/board")
        columns = {}
        for key, title in (("new", "New"), ("progress", "In progress"), ("waiting", "Waiting for customer")):
            columns[key] = await box(fr().locator("h2", has_text=title).first)
        await shot("05-board", card=await box(fr().locator("a, div", has_text="#61").last), **columns)

        # 06 — templates: the list, then one click in a ticket
        await go("/manage/templates")
        await shot("06a-templates")
        await go("/tickets/62")
        picker = fr().locator("button", has_text="Templates").first
        await picker.click()
        await page.wait_for_timeout(1200)
        choice = fr().locator("text=Access: new employee").first
        await shot("06b-picker", picker=await box(picker), choice=await box(choice))
        await choice.click()
        await page.wait_for_timeout(1000)
        await shot("06c-inserted", composer=await box(fr().locator("textarea").first))

        # 07 — help centre: the list with its public links, then a public page
        await go("/manage/articles")
        await shot("07a-articles", link=await box(fr().locator("a", has_text="/help/how-do-i-get-a-refund").first))
        await go("/help/how-do-i-get-a-refund")
        await shot("07b-article")

        # 08 — integrations and the connection checklist
        await go("/manage/integrations")
        await shot("08-integrations", checklist=await box(fr().locator("text=Connection checklist").first),
                   first_tab=await box(fr().locator("button, a", has_text="Telegram").first),
                   last_tab=await box(fr().locator("button, a", has_text="Website widget").first))

        # 09 — the assistant. 09b and 09c are cross-faded; confirming shortens
        # the proposal card and the chat re-pins to the bottom, so measure the
        # loss on a throwaway page first and film both at the same scroll.
        scratch = await (await browser.new_context(viewport={"width": VIEW_W, "height": VIEW_H + 36})).new_page()
        await scratch.goto(f"{base}/demo/?lang=en#%2Fassistant")
        await scratch.wait_for_timeout(2500)
        sf = scratch.frames[1]
        await sf.locator("textarea").first.fill(PROPOSE)
        await sf.locator("textarea").first.press("Enter")
        await scratch.wait_for_timeout(2200)
        max_scroll = "() => { const c = document.getElementById('assistant-chat'); return c.scrollHeight - c.clientHeight; }"
        before = await sf.evaluate(max_scroll)
        await sf.locator("button", has_text="Confirm").last.click()
        await scratch.wait_for_timeout(1800)
        shrink = before - await sf.evaluate(max_scroll)
        await scratch.close()

        await go("/assistant")
        prompt = fr().locator("textarea").first
        await prompt.fill(ASK)
        await shot("09t-typed", prompt=await box(prompt))
        await prompt.press("Enter")
        await page.wait_for_timeout(2200)
        to_question = """(q) => { const c = document.getElementById('assistant-chat');
            const el = [...c.querySelectorAll('.assistant-message-text')].find(n => n.textContent.trim() === q);
            c.scrollTop += el.getBoundingClientRect().top - c.getBoundingClientRect().top - 16; return c.scrollTop; }"""
        await fr().evaluate(to_question, ASK)
        await page.wait_for_timeout(900)
        await fr().evaluate(to_question, ASK)
        await page.wait_for_timeout(200)
        await shot("09a-ask")
        await prompt.fill(PROPOSE)
        await prompt.press("Enter")
        await page.wait_for_timeout(2200)
        confirm = fr().locator("button", has_text="Confirm").last
        hold = "(t) => { const c = document.getElementById('assistant-chat'); c.scrollTop = t; return c.scrollTop; }"
        target = await fr().evaluate(max_scroll) - shrink
        for _ in range(2):
            await fr().evaluate(hold, target)
            await page.wait_for_timeout(700)
        await shot("09b-propose", confirm=await box(confirm))
        await confirm.click()
        await page.wait_for_timeout(1800)
        for _ in range(2):
            await fr().evaluate(hold, target)
            await page.wait_for_timeout(700)
        await shot("09c-confirmed")

        # 10 — AI settings: the customer agent
        await go("/manage/ai")
        await fr().evaluate("""() => { const h = [...document.querySelectorAll('h2,h3,div,span')]
            .find(n => n.children.length === 0 && n.textContent.trim() === 'Customer AI agent');
            h.scrollIntoView({block: 'start'}); window.scrollBy(0, -24); }""")
        await page.wait_for_timeout(600)
        mode = fr().locator("select[name=mode], select").filter(has_text="One reply").first
        await shot("10a-agent", mode=await box(mode))
        limits = fr().locator("label", has_text="Maximum AI reply attempts").first
        stops = fr().locator("p, div", has_text="AI stops when a human takes ownership").last
        await fr().evaluate("""() => { const h = [...document.querySelectorAll('label')]
            .find(n => /^Maximum AI reply attempts/.test(n.textContent.trim()));
            h.scrollIntoView({block: 'start'});
            let c = h.parentElement; while (c && c.scrollHeight <= c.clientHeight) c = c.parentElement;
            (c || document.scrollingElement).scrollTop -= 160; }""")
        await page.wait_for_timeout(600)
        await shot("10b-limits", limits=await box(limits), stops=await box(stops))

        # 11 — overview and the period report
        await go("/overview")
        await dismiss_toasts()
        await shot("11a-overview")
        await go("/reports")
        await dismiss_toasts()
        # The Operators and Teams tables sit side by side. The page scrolls
        # inside its main column, not the window, so scroll that container.
        await fr().evaluate("""() => { const h = [...document.querySelectorAll('h2,h3,h4')]
            .find(n => /^Operators/.test(n.innerText.trim()));
            h.scrollIntoView({block: 'start'});
            let c = h.parentElement; while (c && c.scrollHeight <= c.clientHeight) c = c.parentElement;
            if (c) c.scrollTop -= 90; }""")
        await page.wait_for_timeout(600)
        await shot("11b-reports")

        await browser.close()
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", help="film this site instead of serving docs/, e.g. http://localhost:8020")
    base = ap.parse_args().base
    if base:
        asyncio.run(main(base.rstrip("/")))
    else:
        server = serve_docs()
        try:
            asyncio.run(main(f"http://127.0.0.1:{server.server_address[1]}"))
        finally:
            server.shutdown()
