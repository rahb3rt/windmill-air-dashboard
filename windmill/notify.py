"""Tell someone about the things this tool cannot fix by itself.

The watchdog already resyncs what a resync can reach. What is left over needs a
person: a unit that has dropped its cloud session can only be recovered by
pulling its power, a dirty filter has to be physically changed, and a unit still
overrunning after the attempt limit is a fault a power cycle does not cure.
Those three, and nothing else -- an alert that fires for things already being
handled automatically is one you learn to ignore.

Delivery is whatever is configured, all standard library:

    WINDMILL_ALERT_WEBHOOK   POST the text to a URL (ntfy, Slack, Discord)
    WINDMILL_ALERT_EMAIL     send to an address via WINDMILL_SMTP_*
    WINDMILL_ALERT_DESKTOP   macOS notification via osascript

Each alert is sent once per occurrence, not once per check. State lives in the
database so a restart -- which auto-reload does on every code change -- cannot
turn a standing problem into a fresh alert every few minutes.
"""
import json
import smtplib
import subprocess
import urllib.parse
import urllib.request
from email.message import EmailMessage

from . import config, store


#: Alerts are keyed so the same standing problem is only announced once. The
#: key changes when the situation does, which is what re-arms it.
KINDS = ("dropped", "filter", "gave-up")

RESEND_AFTER = 24 * 3600     # re-announce a problem still present after a day


def _webhook(url, title, body):
    data = body.encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Title", title)                 # ntfy reads these
    req.add_header("Content-Type", "text/plain; charset=utf-8")
    if "slack.com" in url or "discord" in url:
        payload = {"text": f"*{title}*\n{body}"}
        if "discord" in url:
            payload = {"content": f"**{title}**\n{body}"}
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(), method="POST",
            headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return 200 <= r.status < 300


def _email(to, title, body):
    host, port = config.SMTP_HOST, config.SMTP_PORT
    if not host:
        return False
    msg = EmailMessage()
    msg["Subject"] = title
    msg["From"] = config.SMTP_FROM or to
    msg["To"] = to
    msg.set_content(body)
    server = (smtplib.SMTP_SSL if port == 465 else smtplib.SMTP)(host, port, timeout=20)
    try:
        if port != 465:
            try:
                server.starttls()
            except smtplib.SMTPException:
                pass
        if config.SMTP_USER:
            server.login(config.SMTP_USER, config.SMTP_PASS)
        server.send_message(msg)
        return True
    finally:
        server.quit()


def _desktop(title, body):
    """macOS notification. Quotes are stripped rather than escaped -- this is
    passed to osascript, and a message is not worth an injection surface."""
    clean = lambda s: s.replace('"', "").replace("\\", "")
    subprocess.run(
        ["osascript", "-e",
         f'display notification "{clean(body)}" with title "{clean(title)}"'],
        check=True, capture_output=True, timeout=15)
    return True


def channels():
    """Which delivery routes are configured, for the banner and the docs."""
    out = []
    if config.ALERT_WEBHOOK:
        out.append("webhook")
    if config.ALERT_EMAIL:
        out.append(f"email to {config.ALERT_EMAIL}")
    if config.ALERT_DESKTOP:
        out.append("desktop")
    return out


def send(title, body):
    """Deliver to every configured channel. Returns [(channel, ok, error)]."""
    results = []
    if config.ALERT_WEBHOOK:
        try:
            results.append(("webhook", _webhook(config.ALERT_WEBHOOK, title, body), None))
        except Exception as exc:
            results.append(("webhook", False, str(exc)))
    if config.ALERT_EMAIL:
        try:
            results.append(("email", _email(config.ALERT_EMAIL, title, body), None))
        except Exception as exc:
            results.append(("email", False, str(exc)))
    if config.ALERT_DESKTOP:
        try:
            results.append(("desktop", _desktop(title, body), None))
        except Exception as exc:
            results.append(("desktop", False, str(exc)))
    return results


def pending(con, sync_states, filter_report, gave_up, now):
    """The problems worth a person's attention right now.

    Takes what the watchdog already computed rather than recomputing it, so an
    alert can never disagree with what the page is showing.
    """
    out = []
    for unit, st in sync_states.items():
        if st.get("state") == "dropped":
            out.append(("dropped", unit,
                        f"{unit} has dropped its cloud session",
                        "Nothing sent can reach it. It needs its power pulled "
                        "at the wall."))
    for unit, f in (filter_report or {}).items():
        if f.get("verdict") == "due":
            out.append(("filter", unit, f"{unit} needs its air filter changed",
                        f.get("detail") or ""))
    for unit, detail in (gave_up or {}).items():
        out.append(("gave-up", unit, f"{unit} is still overrunning",
                    detail or "Resyncs have not stopped it; this is not a fault "
                              "a power cycle fixes."))
    return out


def dispatch(con, items, now, record=None):
    """Send anything not already announced. Returns what was sent."""
    if not channels():
        return []
    sent = []
    for kind, unit, title, body in items:
        key = f"alert:{kind}:{unit}"
        try:
            last = float(store.get_meta(con, key, 0) or 0)
        except (TypeError, ValueError):
            last = 0
        if now - last < RESEND_AFTER:
            continue
        results = send(title, body)
        ok = any(r[1] for r in results)
        store.set_meta(con, key, int(now) if ok else 0)
        sent.append((kind, unit, title, ok, results))
        if record:
            failed = [f"{c}: {e}" for c, good, e in results if not good]
            record(unit, f"alert sent ({kind})",
                   ", ".join(f"{c}" for c, good, _ in results if good)
                   or "; ".join(failed) or "no channel", ok)
    return sent


def clear(con, kind, unit):
    """Forget an announced problem, so it alerts again if it comes back."""
    store.set_meta(con, f"alert:{kind}:{unit}", 0)
