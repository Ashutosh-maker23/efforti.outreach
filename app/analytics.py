"""Real, first-party analytics computed from the database.

Every number here is derived from actual records — messages we sent, reply
events our IMAP poller detected, bounces, suppressions. Nothing is simulated.
Scopes to a single mailbox when one is active, else all mailboxes combined.
"""
from datetime import timedelta

from .models import Event, Lead, Message, Suppression, utcnow


def fmt_delta(td) -> str:
    if td is None:
        return "—"
    s = int(td.total_seconds())
    if s < 0:
        return "—"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h {(s % 3600) // 60}m"
    return f"{s // 86400}d {(s % 86400) // 3600}h"


def _reply_times(db):
    """email -> first reply timestamp, parsed from reply events."""
    out = {}
    for e in db.query(Event).filter(Event.kind == "reply").all():
        det = e.detail or ""
        email = ""
        if "from " in det:
            email = det.split("from ", 1)[1].split(" ")[0].strip().lower().rstrip("—,.")
        if email:
            if email not in out or e.created_at < out[email]:
                out[email] = e.created_at
    return out


def compute(db, mailbox=None, leads=None):
    """Return a dict of metrics + per-lead engagement rows.

    `mailbox`  — restrict sends/replies to this mailbox (None = all).
    `leads`    — the (already scoped) lead list to build the engagement table.
    """
    msg_q = db.query(Message).filter(Message.status == "sent")
    if mailbox:
        msg_q = msg_q.filter(Message.mailbox_email == mailbox.email)
    msgs = msg_q.all()

    # Aggregate per lead from messages
    per_lead = {}          # email -> {mails, followups, first_sent, opened, clicked}
    step_counts = {}
    opens_total = clicks_total = tracked_sent = 0
    for m in msgs:
        step_counts[m.step_index] = step_counts.get(m.step_index, 0) + 1
        d = per_lead.setdefault(m.lead_email,
                                {"mails": 0, "followups": 0, "first_sent": None,
                                 "opened": False, "clicked": False,
                                 "tracked": False})
        d["mails"] += 1
        if m.step_index > 0:
            d["followups"] += 1
        if d["first_sent"] is None or m.sent_at < d["first_sent"]:
            d["first_sent"] = m.sent_at
        # Engagement tracking (opens/clicks). track_token is only set on sends
        # from a tracking-enabled mailbox, so tracked_sent is the honest
        # denominator for open/click rates — untracked sends never drag it down.
        if getattr(m, "track_token", None):
            tracked_sent += 1
            d["tracked"] = True
        opens_total += (getattr(m, "open_count", 0) or 0)
        clicks_total += (getattr(m, "click_count", 0) or 0)
        if getattr(m, "opened_at", None):
            d["opened"] = True
        if getattr(m, "clicked_at", None):
            d["clicked"] = True

    replies = _reply_times(db)

    contacted = len(per_lead)
    replied_emails = [e for e in per_lead if e in replies]
    replied = len(replied_emails)
    sent_total = len(msgs)
    followups = sum(1 for m in msgs if m.step_index > 0)

    # Engagement rates, people-based (parallel to reply_rate) and scoped to the
    # people who were actually tracked, so a mostly-untracked history doesn't
    # read as "0% open rate". tracked_people is the denominator.
    tracked_people = sum(1 for d in per_lead.values() if d.get("tracked"))
    openers = sum(1 for d in per_lead.values() if d["opened"])
    clickers = sum(1 for d in per_lead.values() if d["clicked"])
    open_rate = round(openers / tracked_people * 100, 1) if tracked_people else 0
    ctr = round(clickers / tracked_people * 100, 1) if tracked_people else 0
    # Click-to-open: of the people who opened, how many clicked (intent signal).
    click_to_open = round(clickers / openers * 100, 1) if openers else 0

    # Average first-response time
    deltas = []
    for e in replied_emails:
        fs = per_lead[e]["first_sent"]
        if fs and replies[e] >= fs:
            deltas.append(replies[e] - fs)
    avg_resp = (sum(deltas, timedelta()) / len(deltas)) if deltas else None

    # Bounces / unsubs (scoped by mailbox via the reply/bounce events if scoped)
    if mailbox:
        bounced = db.query(Event).filter(
            Event.kind == "bounce", Event.detail.contains(mailbox.email)).count()
        unsub = db.query(Event).filter(Event.kind == "unsub").count()
    else:
        bounced = db.query(Lead).filter(Lead.status == "bounced").count()
        unsub = db.query(Suppression).filter(
            Suppression.reason == "unsubscribed").count()

    # Sends over the last 14 days
    today = utcnow().date()
    buckets = {today - timedelta(days=i): 0 for i in range(13, -1, -1)}
    for m in msgs:
        d = m.sent_at.date()
        if d in buckets:
            buckets[d] += 1
    peak = max(buckets.values()) if buckets else 0
    timeline = [{"label": d.strftime("%d %b"), "count": c,
                 "pct": (c / peak * 100) if peak else 0}
                for d, c in sorted(buckets.items())]

    # Per-lead engagement rows
    rows = []
    for l in (leads or []):
        pl = per_lead.get(l.email, {})
        fs = pl.get("first_sent")
        rt = replies.get(l.email)
        rows.append({
            "name": f"{l.first_name} {l.last_name}".strip() or "—",
            "company": l.company or "—",
            "title": l.title or "—",
            "status": l.status,
            "mails": pl.get("mails", 0),
            "followups": pl.get("followups", 0),
            "contacted_at": fs,
            "opened": pl.get("opened", False),
            "clicked": pl.get("clicked", False),
            "response": fmt_delta(rt - fs) if (rt and fs and rt >= fs) else None,
        })
    # Hot leads first: clicked > opened > most touches > replied.
    rows.sort(key=lambda r: (r["clicked"], r["opened"], r["mails"],
                             r["response"] is not None), reverse=True)

    return {
        "sent_total": sent_total,
        "followups": followups,
        "contacted": contacted,
        "replied": replied,
        "reply_rate": round(replied / contacted * 100, 1) if contacted else 0,
        "tracked_sent": tracked_sent,
        "tracked_people": tracked_people,
        "open_rate": open_rate,
        "ctr": ctr,
        "click_to_open": click_to_open,
        "openers": openers,
        "clickers": clickers,
        "opens_total": opens_total,
        "clicks_total": clicks_total,
        "bounced": bounced,
        "bounce_rate": round(bounced / sent_total * 100, 1) if sent_total else 0,
        "unsub": unsub,
        "step_counts": step_counts,
        "avg_response": fmt_delta(avg_resp),
        "timeline": timeline,
        "rows": rows[:200],
        "apollo_pulled": db.query(Lead).filter(Lead.source == "apollo").count(),
    }
