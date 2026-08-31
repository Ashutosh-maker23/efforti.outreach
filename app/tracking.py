"""Who fetched a tracking pixel, and whether that counts as a human open.

This module keeps two things strictly apart, because conflating them is how
tracking starts lying to you:

    WHAT IS OBSERVED  — the user-agent, the requester's address, and how long
                        after the send the fetch arrived. Facts. Recorded for
                        every single hit in the `track_hits` table, always,
                        whatever we go on to conclude.

    WHAT IS CONCLUDED — whether to call that fetch an "open". A judgement, made
                        by count_policy() below, out in the open, with the
                        reason written next to the row it produced.

THE ONE THING THAT IS ACTUALLY UNKNOWABLE
    Gmail serves every image through GoogleImageProxy, and it fetches images
    BOTH when a message is delivered (to cache them) and when a person opens it.
    Both arrive from the same servers with the same user-agent. So for a Gmail
    recipient the first fetch is genuinely ambiguous: nothing in the request
    says which of the two it was. That is a property of how Gmail works, not a
    gap in this code, and no tracking product resolves it — they each just pick
    a side and stay quiet about it.

    Timing is a CLUE to that ambiguity, not a proof. A fetch two seconds after
    the send is usually delivery caching — but someone sitting on their inbox
    really can open a mail in two seconds, and if they do, timing calls them a
    machine and is simply wrong.

WHAT IS SOLID, AND WHAT IS NOT
    solid   a user-agent naming a scanner, crawler or script (curl, Proofpoint,
            SafeLinks) is not the recipient. Never count it.
    solid   an ordinary mail-client or browser user-agent is the recipient's own
            device rendering the message. Count it, at any delay — including two
            seconds, because that is a real person who really was quick.
    unsure  a provider proxy (Gmail, Yahoo, Apple). Could be delivery caching,
            could be a real open. OPEN_POLICY decides, and says which.

OPEN_POLICY (env, default "proxy_delayed") — what to do with the unsure case:

    all             count every proxy fetch. Nothing is lost and nothing is
                    invented, but delivery caching inflates opens — this is what
                    produced the "opened 2 seconds after send" line.
    proxy_delayed   count a proxy fetch when it is either not the first one for
                    that message, or arrived more than OPEN_MIN_SECONDS after the
                    send. Catches delivery caching; costs you a genuine open by
                    someone who replied instantly and never looked again.
    never           count only real client user-agents. Zero false opens, and
                    zero opens from Gmail recipients, which is most of a cold
                    list. Honest, and nearly useless as a rate.

    No setting makes the ambiguity go away. Whichever you pick, `track_hits`
    holds every raw fetch, so you can re-derive the other answers at any time.

OPEN_MIN_SECONDS (env, default 30) — only used by the "proxy_delayed" policy.
"""
import os
import re

OPEN_POLICY = os.environ.get("OPEN_POLICY", "proxy_delayed").strip().lower()
OPEN_MIN_SECONDS = int(os.environ.get("OPEN_MIN_SECONDS", "30"))

# Never the recipient, at any delay: security scanners that read mail before it
# is delivered, crawlers, link unfurlers, and scripted HTTP clients. Extend this
# when a new one turns up in track_hits.user_agent.
_MACHINE_UA_RE = re.compile(
    r"proofpoint|mimecast|barracuda|forcepoint|ironport|zscaler|fireeye"
    r"|symantec|trendmicro|sophos|bitdefender|kaspersky|mcafee|cloudmark"
    r"|safelinks|urldefense|linkprotect|emailsecurity|messagelabs"
    r"|virustotal|urlscan"
    r"|bot\b|crawler|spider|scraper|scanner|monitor|healthcheck"
    r"|facebookexternalhit|slackbot|twitterbot|linkedinbot|discordbot"
    r"|telegrambot|whatsapp|skypeuripreview|embedly|preview"
    r"|curl/|wget|python-requests|python-urllib|go-http-client|java/"
    r"|okhttp|libwww|guzzle|axios|node-fetch|httpclient|headlesschrome"
    r"|phantomjs|puppeteer|playwright",
    re.I)

# The recipient's mail provider fetching on their behalf. NOT a machine — a real
# open reaches us looking exactly like this — but not proof of one either.
_PROXY_UA_RE = re.compile(
    r"googleimageproxy|via ggpht|googleusercontent|google-read-aloud"
    r"|yahoomailproxy|yandexmail|mail\.ru|proxy\.mail",
    re.I)


def classify_source(user_agent: str) -> str:
    """What the requester IS, from its user-agent alone. Pure observation:
    "machine" | "proxy" | "client". No timing, no policy, no guessing."""
    ua = (user_agent or "").strip()
    if not ua:
        return "machine"                    # real clients always send one
    if _MACHINE_UA_RE.search(ua):
        return "machine"
    if _PROXY_UA_RE.search(ua):
        return "proxy"
    return "client"


def count_policy(source: str, delay_seconds, prior_hits: int) -> tuple:
    """Should this fetch be counted as a genuine open/click? Returns
    (counted: bool, reason: str) — the reason is stored next to the hit so any
    number the app reports can be traced back to why.

    `delay_seconds` may be None (no send time recorded); `prior_hits` is how many
    fetches this message already had, which is what lets a repeat proxy fetch
    count even under the strict policy — Gmail re-fetching a message it already
    cached means something rendered it.
    """
    if source == "machine":
        return False, "scanner or script, never a reader"
    if source == "client":
        # A real mail client asked for the image. That IS the recipient, however
        # fast they were. No delay test — being quick is not being a robot.
        return True, "recipient's own mail client"
    # source == "proxy": the genuinely ambiguous case.
    if OPEN_POLICY == "all":
        return True, "provider proxy (policy: count all)"
    if OPEN_POLICY == "never":
        return False, "provider proxy (policy: never count proxies)"
    if prior_hits > 0:
        return True, "provider re-fetched an already-cached image"
    if delay_seconds is None:
        return True, "provider proxy, send time unknown"
    if delay_seconds >= OPEN_MIN_SECONDS:
        return True, f"provider proxy, {delay_seconds:.0f}s after send"
    return False, (f"provider proxy, only {delay_seconds:.0f}s after send — "
                   f"likely delivery caching, not a read")


def short_agent(user_agent: str, limit: int = 300) -> str:
    """The user-agent trimmed to what a column and a table cell can hold."""
    return (user_agent or "").strip()[:limit]
