"""Seed a standalone DEMO database with realistic fake data for portfolio screenshots.

Creates demo_support.db in the project root. The real support.db is never touched.
Run:  .venv/Scripts/python.exe scripts/seed_demo_data.py
Re-running deletes and rebuilds the demo DB from scratch (idempotent).
"""

import asyncio
import os
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

DEMO_DB_PATH = PROJECT_ROOT / "demo_support.db"

# Point the app at the demo DB BEFORE importing src.* (settings load at import time).
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{DEMO_DB_PATH.as_posix()}"
# Dummies in case .env lacks these (setdefault never overrides real values).
os.environ.setdefault("BOT_TOKEN", "1234567890:DEMO-not-a-real-token")
os.environ.setdefault("ADMIN_CHAT_ID", "-1001234567890")
os.environ.setdefault("ACCESS_PASSWORD", "admin")

random.seed(42)

NOW = datetime.utcnow()

TEAMS = ["Team Alpha", "Team Bravo", "Team Delta"]

# (username, work_username, language)
FAKE_USERS = [
    ("alex_bennett", "alex.bennett", "en"),
    ("maria_lopez", "maria.l", "en"),
    ("daniel_web", "daniel.s", "en"),
    ("olivia_hr", "olivia.b", "en"),
    ("ian_qa", "ian.t", "en"),
    ("sophia_pm", "sophia.r", "en"),
    ("max_dev", "max.d", "en"),
    ("anna_support", "anna.v", "en"),
    ("paul_net", "paul.m", "en"),
    ("kate_design", "kate.l", "en"),
    ("ryan_db", "ryan.z", "en"),
    ("julia_fin", "julia.p", "en"),
    ("dennis_ops", "dennis.k", "en"),
    ("olga_ml", "olga.h", "en"),
    ("adam_junior", "adam.n", "en"),
]

# Registered through the bot, not placed in a team yet — the queue an operator
# clears from Users → "No team".
PENDING_USERS = ["nina_newhire", "tom_contractor", "lea_intern"]

ADMIN_IDS = [200001, 200002, 200003]

# category -> (user opening messages, admin replies, user closing remarks)
CONVERSATIONS = {
    "Network": (
        [
            "VPN won't connect, stuck on 'Connecting...' for ten minutes",
            "VPN dropped in the middle of the day, reconnecting doesn't help",
            "Error 809 when connecting to the VPN, what should I do?",
            "VPN stopped working after a Windows update, says timeout",
            "VPN connects but there is no internet through it",
        ],
        [
            "Sent you a new config — remove the old tunnel and import this one.",
            "Switch to the backup server in settings, the primary one is overloaded right now.",
            "Restart the WireGuard service and reconnect, that should fix it.",
        ],
        ["Works now, thanks!", "All good, connected 👍", "Thank you, it works!"],
    ),
    "Hardware": (
        [
            "Can't reach the server over RDP, it says 'internal error'",
            "The server is painfully slow, impossible to work",
            "My RDP session dropped and now it won't let me back in",
            "Remote desktop won't open, black screen",
            "Desktop shortcuts disappeared after the server reboot",
        ],
        [
            "Reset your stuck session — try logging in again.",
            "Restarted the server, please check once more.",
            "Your previous session had hung. Killed it, give it another try.",
        ],
        ["Logged in, everything works", "Great, thanks for the quick fix!", "Working, thanks!"],
    ),
    "Access": (
        [
            "Lost access to 1Password, it asks for a Secret Key I don't have",
            "The confirmation code hasn't arrived by email for half an hour",
            "My Google account got locked after a password change",
            "Can't sign in to my work Telegram, the code never arrives",
            "1Password won't sync between my devices",
        ],
        [
            "Sent the Emergency Kit in a DM — it has the Secret Key, restore access with it.",
            "Reset your password, the temporary one is in your DMs. Change it on first login.",
            "Check the Spam folder — codes often land there. If not, I'll send a new invite.",
        ],
        ["Got access back, thanks", "It worked!", "Thank you!"],
    ),
    "Other": (
        [
            "Second monitor doesn't work, not detected at all",
            "I need access to the task tracker, I'm new on the team",
            "The printer on floor 3 keeps jamming",
            "My laptop runs very hot and loud, can someone look at it?",
            "I need permission to install a PDF tool",
        ],
        [
            "Granted you access, the invite will arrive at your work email.",
            "Try a different DisplayPort cable; if that doesn't help I'll come take a look.",
            "Ordered a replacement; meanwhile please use the printer on floor 2.",
        ],
        ["Thanks, all good!", "Works now 👍", "Thanks for the help!"],
    ),
}

# Operator-to-operator notes. Never sent to the user — kept plausible but dull
# on purpose, since the demo is public.
INTERNAL_NOTES = [
    "Third ticket from this user about the same laptop — worth swapping the dock.",
    "Waiting on the vendor RMA number before we can promise a date.",
    "Their manager approved the licence over email, attaching the thread to the asset record.",
    "Escalated to network on-call; the switch port keeps flapping.",
    "Heads up: this account is on the legacy VPN profile, migration is scheduled next week.",
    "Called them, no answer. Will close if there's nothing back by Friday.",
]

CLOSE_REASON_BY_CATEGORY = {
    "Network": ["Configuration fixed"],
    "Hardware": ["Configuration fixed"],
    "Access": ["Access granted"],
    "Other": [],  # custom reason
}

TEMPLATES = [
    # (title, body, shortcut, category)
    ("VPN: reinstall the config",
     "Remove the current tunnel from WireGuard, download the new config from your DMs and import it. After importing, press Activate and check the connection.",
     "vpn_reset", "Network"),
    ("VPN: switch server",
     "The primary server is overloaded right now. Open the VPN settings and pick the backup server from the list. The connection should recover within a minute.",
     "vpn_switch", "Network"),
    ("RDP: reset a stuck session",
     "Your previous session hung on the server. I killed it — wait 2-3 minutes and reconnect using the same shortcut.",
     "rdp_reset", "Hardware"),
    ("RDP: black screen",
     "On a black screen press Ctrl+Alt+End and choose 'Sign out', then reconnect. If that doesn't help, message me and I'll restart the session on my side.",
     "rdp_black", "Hardware"),
    ("Password manager: restore access",
     "Sent you the Emergency Kit in a DM. Open the vault website, enter your email and the Secret Key from the document, then set a new master password.",
     "pwd_restore", "Access"),
    ("Email: code not arriving",
     "Check the Spam and Promotions folders — confirmation codes often land there. If there is still no code after 10 minutes, request a new one and message me.",
     "mail_code", "Access"),
    ("Access: new employee",
     "Granted you access to the work services. Invites will arrive at your work email within 15 minutes. The setup checklist is in the team's pinned message.",
     "onboard_access", "Other"),
    ("Diagnostics: laptop overheating",
     "Sounds like a clogged cooling system. Bring the laptop to the IT room — we'll clean it and replace the thermal paste, takes about an hour.",
     "laptop_heat", "Other"),
]

AI_SUGGESTED_TEXTS = [
    "Based on the description the user's WireGuard tunnel is stuck. Suggest: 1) disconnect the VPN, 2) restart the service, 3) reconnect. If that fails, send a fresh config.",
    "A similar issue was ticket #47 — resetting the RDP session server-side fixed it. Suggest killing the user's session and asking them to reconnect.",
    "The confirmation code is probably going to spam. Ask the user to check Spam/Promotions and request a new code in 10 minutes.",
    "From the knowledge base: for error 809 check whether the router blocks the UDP port. Fix — switch the protocol or change server.",
    "A new employee needs the standard access set: email, messenger, task tracker. Use the 'Access: new employee' template.",
]


def work_hours_dt(days_ago: float) -> datetime:
    """Random datetime N days ago, biased to working hours (06-16 UTC ≈ 09-19 Kyiv).

    Snapping the hour can push a "few hours ago" timestamp past NOW, which then
    shows up on the board as a negative age. Clamp to a minute ago so demo data
    never contains tickets from the future.
    """
    base = NOW - timedelta(days=days_ago)
    out = base.replace(
        hour=random.randint(6, 16),
        minute=random.randint(0, 59),
        second=random.randint(0, 59),
        microsecond=0,
    )
    return min(out, NOW - timedelta(minutes=1))



def _demo_screenshot() -> bytes | None:
    """A plausible thing for a customer to attach, or None.

    Drawn rather than shipped as a file so nothing in the repository is a
    picture of anyone's real screen. Pillow only lives in
    requirements-screenshots.txt — CI runs this seed and deliberately does not
    install a browser stack, so its absence skips the attachment instead of
    failing the build.
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None

    import io

    img = Image.new("RGB", (720, 420), "#f4f4f5")
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 720, 56], fill="#ffffff")
    d.rectangle([24, 18, 300, 38], fill="#e4e4e7")
    d.rectangle([40, 104, 680, 300], fill="#ffffff", outline="#e4e4e7", width=2)
    for i, y in enumerate((140, 180, 220)):
        d.rectangle([72, y, 72 + (420 - i * 90), y + 14], fill="#e4e4e7")
    # The part that makes it read as a bug report rather than a blank page.
    d.rectangle([72, 252, 400, 276], fill="#fecdd3")
    d.rectangle([80, 258, 392, 270], fill="#e11d48")
    d.rectangle([40, 330, 220, 372], fill="#a1a1aa")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def main() -> None:
    # Fresh start every run.
    for suffix in ("", "-shm", "-wal"):
        p = Path(f"{DEMO_DB_PATH}{suffix}")
        if p.exists():
            p.unlink()

    from src.database.db import init_db, db_session  # noqa: E402 (env is set above)
    from src.database.models import (  # noqa: E402
        AdminUser, AICall, Message, ReplyTemplate, Team, Ticket, User,
    )
    from src.services.admin_user_service import hash_password  # noqa: E402

    await init_db()  # schema + default team/categories/close-reasons/admin account

    async with db_session() as session:
        # --- Operators (dashboard accounts, linked to the Telegram admin ids) --
        # init_db already seeded admin@local (owner). These three match ADMIN_IDS
        # so bot-side replies auto-assign to them, and the dashboard shows names.
        operators = []
        for (op_name, op_email, op_tg) in [
            ("Emma Clark",  "emma@demo.local",  ADMIN_IDS[0]),
            ("Liam Novak",  "liam@demo.local",  ADMIN_IDS[1]),
            ("Noah Weber",  "noah@demo.local",  ADMIN_IDS[2]),
        ]:
            op = AdminUser(
                email=op_email,
                password_hash=hash_password("demo-operator-1"),
                name=op_name,
                role="agent",
                tg_id=op_tg,
                is_active=True,
            )
            session.add(op)
            operators.append(op)
        await session.flush()
        operator_by_tg = {op.tg_id: op for op in operators}

        # --- Teams (init_db seeded "General"; add the rest) -------------------
        teams = []
        for name in TEAMS:
            t = Team(name=name, created_at=NOW - timedelta(days=90))
            session.add(t)
            teams.append(t)
        await session.flush()

        # --- Users -------------------------------------------------------------
        users = []
        per_team_counter = {t.id: 0 for t in teams}
        for i, (username, work_username, lang) in enumerate(FAKE_USERS):
            team = teams[i % len(teams)]
            per_team_counter[team.id] += 1
            u = User(
                tg_id=100001 + i,
                username=username,
                work_username=work_username,
                team_id=team.id,
                local_user_id=per_team_counter[team.id],
                language=lang,
                is_verified=True,
                is_active=True,
                created_at=NOW - timedelta(days=random.randint(50, 85)),
            )
            session.add(u)
            users.append(u)

        # People who pressed /start but have no team yet. They never open a
        # ticket until an operator places them, so without these the demo never
        # shows the "waiting for a team" queue — which is a real daily task.
        for j, pending_name in enumerate(PENDING_USERS):
            session.add(User(
                tg_id=100900 + j,
                username=pending_name,
                team_id=None,
                language="en",
                is_verified=False,
                is_active=True,
                created_at=NOW - timedelta(hours=random.randint(2, 40)),
            ))
        await session.flush()

        # --- Reply templates -----------------------------------------------------
        templates = []
        for i, (title, body, shortcut, category) in enumerate(TEMPLATES):
            tpl = ReplyTemplate(
                team_id=teams[i % len(teams)].id,
                category=category,
                title=title,
                body=body,
                shortcut=shortcut,
                usage_count=0,  # incremented as tickets reference them below
                created_by=ADMIN_IDS[0],
                created_at=NOW - timedelta(days=random.randint(30, 60)),
            )
            session.add(tpl)
            templates.append(tpl)
        await session.flush()
        templates_by_category: dict[str, list[ReplyTemplate]] = {}
        for tpl in templates:
            templates_by_category.setdefault(tpl.category, []).append(tpl)

        # --- Tickets + messages ----------------------------------------------------
        categories = list(CONVERSATIONS.keys())
        # Weighted mix: VPN and Server dominate, like a real helpdesk.
        weights = [0.34, 0.30, 0.21, 0.15]
        n_tickets = 82
        thread_seq = 500
        unanswered_seq = 0
        open_seq = 0
        # Order matters more than proportion here — see the comment at the use.
        _SLA_DEMO_CYCLE = ("breach", "ok", "warn", "ok")
        # (stage, minutes since the customer came back — None = their move)
        _OPEN_DEMO_CYCLE = (
            ("in_progress", 95),    # answered, they replied, now past the target
            ("waiting", None),      # parked on the customer, nothing owed
            ("in_progress", None),  # being worked on right now
            ("waiting", None),
            ("in_progress", 20),    # they just wrote, still well inside the hour
        )

        ai_rows = []
        for n in range(n_tickets):
            user = random.choice(users)
            category = random.choices(categories, weights=weights, k=1)[0]
            opens, replies, closings = CONVERSATIONS[category]

            # Recent-biased spread over the last 45 days; a few from today.
            days_ago = round(random.random() ** 1.6 * 45, 2)
            created = work_hours_dt(days_ago)
            # A board with six cards across four columns looks abandoned. Three
            # days of open work is both realistic and enough to fill it.
            is_closed = days_ago > 3.0 or random.random() < 0.35

            # ~45% of ACTIVE tickets stay unanswered — feeds the "awaiting
            # reply" queue badge/KPI so the demo shows a live queue.
            unanswered = (not is_closed) and random.random() < 0.45
            if unanswered:
                # A real team does not leave a first message sitting for days,
                # and a board where every waiting card is the same colour
                # teaches nobody what the colours mean. Cycled rather than
                # rolled: there are only a handful of these, and random draws
                # kept producing a board with no amber or no red on it at all.
                # Only a handful of tickets ever land here, so the worst case
                # has to be first: at two waiting cards the board still shows
                # a breach and a healthy one, not two of the same.
                bucket = _SLA_DEMO_CYCLE[unanswered_seq % len(_SLA_DEMO_CYCLE)]
                unanswered_seq += 1
                if bucket == "breach":
                    late = random.randint(150, 400)     # past twice the target
                elif bucket == "warn":
                    late = random.randint(65, 115)      # past the target
                else:
                    late = random.randint(4, 45)        # inside the hour
                created = NOW - timedelta(minutes=late)

            thread_seq += 1
            ticket = Ticket(
                user_id=user.id,
                team_id=user.team_id,
                category=category,
                status="closed" if is_closed else "active",
                thread_id=thread_seq,
                header_message_id=thread_seq * 10,
                notification_sent=True,
                ticket_code=f"#{n + 1}",
                telegram_id=user.tg_id,
                created_at=created,
            )

            # Conversation: user opens -> admin replies (sometimes via template)
            # -> optional back-and-forth -> user confirms.
            admin_id = random.choice(ADMIN_IDS)
            msgs = [("user", user.tg_id, random.choice(opens), created, None)]

            tpl = None
            last_at = created
            if not unanswered:
                # Mirror production behaviour: the first responder owns the ticket
                ticket.assigned_admin_id = operator_by_tg[admin_id].id
                first_response_min = random.randint(3, 45)
                reply_at = created + timedelta(minutes=first_response_min)
                use_template = random.random() < 0.45
                tpl = random.choice(templates_by_category[category]) if use_template else None
                reply_text = tpl.body if tpl else random.choice(replies)
                msgs.append(("admin", admin_id, reply_text, reply_at, tpl.id if tpl else None))
                if tpl:
                    tpl.usage_count += 1

                last_at = reply_at
                # Answered but still open → either being worked on or parked
                # on the customer. Gives the board all four columns.
                if not is_closed:
                    stage, came_back = _OPEN_DEMO_CYCLE[open_seq % len(_OPEN_DEMO_CYCLE)]
                    open_seq += 1
                    ticket.stage = stage
                    if came_back is not None:
                        # The case the waiting badge exists for: we answered,
                        # the customer wrote again, and nobody has picked it
                        # back up. Without one of these on the board the
                        # feature never shows itself in a screenshot.
                        # Jittered so two cards from the same cycle slot do not
                        # show the identical age and read as fabricated.
                        back_at = NOW - timedelta(
                            minutes=came_back + random.randint(-12, 12))
                        msgs.append(("user", user.tg_id, "Any update on this?",
                                     back_at, None))
                        last_at = max(last_at, back_at)
                if random.random() < 0.4:  # extra clarification round
                    q_at = reply_at + timedelta(minutes=random.randint(2, 25))
                    a_at = q_at + timedelta(minutes=random.randint(2, 30))
                    msgs.append(("user", user.tg_id, "Tried that, no change so far...", q_at, None))
                    msgs.append(("admin", admin_id, random.choice(replies), a_at, None))
                    last_at = a_at

            if is_closed:
                done_at = last_at + timedelta(minutes=random.randint(2, 90))
                msgs.append(("user", user.tg_id, random.choice(closings), done_at, None))
                ticket.closed_at = done_at + timedelta(minutes=random.randint(1, 15))
                ticket.processing_time_seconds = int(
                    (ticket.closed_at - created).total_seconds()
                )
                reasons = CLOSE_REASON_BY_CATEGORY[category]
                if reasons:
                    ticket.admin_categories = reasons
                else:
                    # No preset fits — exercises the free-text closure path.
                    # The sentinel is language-neutral; the UI renders it in
                    # whatever language the instance is set to.
                    from src.services.close_reason_service import CUSTOM_VALUE
                    ticket.admin_categories = [CUSTOM_VALUE]
                    ticket.custom_reason = "Resolved on the spot"

                # CSAT: ~70% of closed tickets get rated, skewed to 4–5 stars
                # (a healthy helpdesk hovers around 4.4–4.6 average).
                if random.random() < 0.70:
                    ticket.csat_rating = random.choices(
                        [1, 2, 3, 4, 5], weights=[2, 3, 7, 26, 62], k=1,
                    )[0]
                    ticket.csat_rated_at = ticket.closed_at + timedelta(
                        minutes=random.randint(1, 180)
                    )

            session.add(ticket)
            await session.flush()

            for sender_type, sender_id, text, at, template_id in msgs:
                session.add(Message(
                    ticket_id=ticket.id,
                    sender_type=sender_type,
                    sender_id=sender_id,
                    text=text,
                    template_id=template_id,
                    created_at=at,
                ))

            # Internal notes on ~18% of answered tickets — operator-to-operator
            # chatter that the user must never see.
            if not unanswered and random.random() < 0.18:
                session.add(Message(
                    ticket_id=ticket.id,
                    sender_type="admin",
                    sender_id=admin_id,
                    text=random.choice(INTERNAL_NOTES),
                    is_internal=True,
                    created_at=last_at + timedelta(minutes=random.randint(1, 20)),
                ))

            # AI deflection attempts: bot tried to self-serve the user on ~35%
            # of tickets; ~40% of confident answers actually solved the issue.
            if random.random() < 0.35:
                deflect_answered = random.random() < 0.55
                deflect_helped = deflect_answered and random.random() < 0.40
                if deflect_helped and is_closed:
                    from src.services.close_reason_service import AI_VALUE
                    ticket.admin_categories = [AI_VALUE]
                    ticket.custom_reason = None
                ai_rows.append(AICall(
                    use_case="deflect",
                    ticket_id=ticket.id,
                    model="gemini-2.5-flash",
                    tokens_in=random.randint(500, 2200),
                    tokens_out=random.randint(50, 300),
                    latency_ms=random.randint(500, 2200),
                    success=deflect_answered,
                    accepted=(deflect_helped if deflect_answered else None),
                    suggested_text=(
                        "Try restarting the WireGuard service and reconnecting."
                        if deflect_answered else None
                    ),
                    created_at=created + timedelta(minutes=1),
                ))

            # AI calls tied to a chunk of tickets (suggestion shown to admins).
            if random.random() < 0.75:
                use_case = random.choice(["suggest_template", "suggest_from_history"])
                success = random.random() < 0.95
                accepted = (random.random() < 0.62) if success else None
                ai_rows.append(AICall(
                    use_case=use_case,
                    ticket_id=ticket.id,
                    suggested_template_id=(
                        tpl.id if (tpl and use_case == "suggest_template") else None
                    ),
                    model="gemini-2.5-flash",
                    tokens_in=random.randint(400, 2800),
                    tokens_out=random.randint(60, 420),
                    latency_ms=random.randint(450, 2600),
                    success=success,
                    error=None if success else "Deadline exceeded (timeout)",
                    accepted=accepted,
                    accepted_by=random.choice(ADMIN_IDS) if accepted else None,
                    suggested_text=(
                        random.choice(AI_SUGGESTED_TEXTS)
                        if use_case == "suggest_from_history" else None
                    ),
                    created_at=created + timedelta(minutes=random.randint(1, 4)),
                ))

        # A few conversations that came from the website widget rather than
        # Telegram. Without them the demo quietly says this is a Telegram
        # product: every ticket carries the same channel badge, and the one
        # thing the channel layer bought is invisible.
        from src.channels.identity import create_customer
        from src.channels.widget import NAME as WIDGET_CHANNEL, address_for
        from src.services import file_store

        # (name, category, customer lines, the answer they got). The reply
        # matters for the photograph: a panel showing three questions and no
        # answers is a picture of support not happening.
        HELP_ARTICLES = [
            ("How do I get a refund?", "how-do-i-get-a-refund",
             "Refunds reach the original card in about five working days.",
             "Open Billing in your account and choose the payment you want "
             "refunded.\n\nRefunds go back to the card that paid and take "
             "about five working days to appear. If that card has expired we "
             "can send it elsewhere, but we have to verify you first.\n\n"
             "Longer than that? Message us with the order number.",
             "Billing", True),
            ("Checkout says gateway timeout", "checkout-gateway-timeout",
             "Usually the card issuer rate-limiting, not a declined card.",
             "A timeout at the payment step almost always means your bank has "
             "rate-limited the card rather than declined it.\n\nWait ten "
             "minutes and try once more. If it happens again, tell us the last "
             "four digits and we will raise the limit on our side.",
             "Other", True),
            ("Changing the email on your account", "changing-your-email",
             "You can do this yourself; we only step in if you are locked out.",
             "Settings \u2192 Account \u2192 Email. We send a confirmation link "
             "to the new address, and the old one keeps working until you click "
             "it.\n\nIf you no longer have access to the old address, message "
             "us and we will verify you another way.",
             "Access", True),
            ("Turning on two-factor authentication", "two-factor-authentication",
             "Any authenticator app works. Keep the recovery codes.",
             "Settings \u2192 Security \u2192 Two-factor. Scan the code with any "
             "authenticator app.\n\nSave the recovery codes somewhere that is "
             "not the same phone. Losing both the phone and the codes means we "
             "have to verify you from scratch, which is slow on purpose.",
             "Access", True),
            ("Pricing changes for the team plan", "pricing-changes-team-plan",
             "Not announced yet \u2014 draft.",
             "Draft. Do not publish until legal have read it.",
             "Billing", False),
        ]

        WIDGET_VISITORS = [
            ("Nora Bell", "Billing", [
                "Does the team plan bill per operator or per mailbox?",
                "And can we pay yearly?",
            ], "Per operator, and yearly is 2 months off. I've put the "
               "invoice options on your account — have a look and tell me "
               "which suits."),
            ("Ivan Petrov", "Access", [
                "The API key I generated yesterday returns 401 on every call.",
            ], "That key was made before we moved you to the new workspace, "
               "so it is pointing at the old one. Generate a fresh one under "
               "Settings → API and the 401 goes away."),
            ("Marta Kern", "Other", [
                "Checkout hangs on the payment step \u2014 card works elsewhere.",
                "Chrome, latest. It says 'gateway timeout' after about 30 seconds.",
            ], "Thanks for the screenshot \u2014 that timeout is our payment "
               "provider rate-limiting the card BIN, not your card. I've "
               "raised the limit for your account; try once more and tell me "
               "if it still hangs."),
        ]

        widget_tickets = 0
        for name, category, lines, answer in WIDGET_VISITORS:
            visitor = await create_customer(
                session, channel=WIDGET_CHANNEL,
                address=address_for(f"demo-session-{name}"),
                username=name, language="en",
            )
            opened = NOW - timedelta(hours=random.randint(1, 30))
            ticket = Ticket(
                user_id=visitor.id, team_id=None, category=category,
                status="active", stage=random.choice(["new", "in_progress"]),
                ticket_code="", telegram_id=visitor.tg_id, created_at=opened,
            )
            session.add(ticket)
            await session.flush()
            ticket.ticket_code = f"#{ticket.id}"
            for i, line in enumerate(lines):
                session.add(Message(
                    ticket_id=ticket.id, sender_type="user", sender_id=visitor.tg_id,
                    text=line, created_at=opened + timedelta(minutes=i * 3),
                ))
            step = len(lines)

            # One conversation carries a screenshot, because "the customer
            # sent a picture of the problem" is what support actually looks
            # like and no frame in the repository showed it.
            shot = _demo_screenshot() if name == "Marta Kern" else None
            if shot is not None:
                ref = file_store.put(shot)
                session.add(Message(
                    ticket_id=ticket.id, sender_type="user",
                    sender_id=visitor.tg_id, text="",
                    file_id=ref, file_type="photo", file_name="checkout.png",
                    file_size=len(shot), mime_type="image/png",
                    created_at=opened + timedelta(minutes=step * 3),
                ))
                step += 1

            if answer:
                session.add(Message(
                    ticket_id=ticket.id, sender_type="admin", sender_id=0,
                    text=answer,
                    created_at=opened + timedelta(minutes=step * 3 + 6),
                ))
            widget_tickets += 1

            # One visitor gets a history, so the panel's message list is a
            # list rather than a single row, and so the closed state appears
            # in a frame somewhere.
            if name == "Marta Kern":
                earlier = NOW - timedelta(days=6)
                past = Ticket(
                    user_id=visitor.id, team_id=None, category="Billing",
                    status="closed", stage="in_progress", ticket_code="",
                    telegram_id=visitor.tg_id, created_at=earlier,
                    closed_at=earlier + timedelta(hours=2),
                    admin_categories=["Solved"],
                )
                session.add(past)
                await session.flush()
                past.ticket_code = f"#{past.id}"
                for offset, (who, line) in enumerate((
                    ("user", "Can I get a VAT invoice for last month?"),
                    ("admin", "Sent it to the address on the account just now. "
                              "Shout if you need the company details changed."),
                    ("user", "Got it, thank you."),
                )):
                    session.add(Message(
                        ticket_id=past.id, sender_type=who,
                        sender_id=visitor.tg_id if who == "user" else 0,
                        text=line,
                        created_at=earlier + timedelta(minutes=offset * 25),
                    ))
                widget_tickets += 1
        await session.flush()

        # A help centre, so the widget's third tab exists in the demo and in
        # the screenshots. One draft among them: the difference between what
        # is written and what is published is the point of the feature.
        from src.database.models import Article

        for order, (title, slug, summary, body, category, live) in enumerate(HELP_ARTICLES):
            session.add(Article(
                slug=slug, title=title, summary=summary, body=body,
                category=category, is_published=live, sort_order=order,
                view_count=random.randint(4, 90) if live else 0,
                created_at=NOW - timedelta(days=random.randint(10, 60)),
            ))

        for row in ai_rows:
            session.add(row)

    print(f"Demo DB ready: {DEMO_DB_PATH}")
    print(f"  teams:     {len(TEAMS) + 1}")
    print(f"  users:     {len(FAKE_USERS)}")
    print(f"  tickets:   {n_tickets} (+{widget_tickets} from the widget)")
    print(f"  templates: {len(TEMPLATES)}")
    print(f"  ai_calls:  {len(ai_rows)}")


if __name__ == "__main__":
    asyncio.run(main())
