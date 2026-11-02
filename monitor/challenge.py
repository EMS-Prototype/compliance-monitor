"""Detect CAPTCHA / OTP / login walls so the worker can STOP and hand over to a human.

This module only detects. It never reads, solves, or submits a challenge.
Detection is heuristic: it can miss a new challenge style or flag a page that merely
mentions one, which is why a flagged source goes to a human instead of being retried.
"""

_DETECT_JS = r"""
() => {
  const q = (s) => document.querySelector(s);
  const text = (document.body ? document.body.innerText : '').slice(0, 20000);

  if (q('.g-recaptcha, .h-captcha, .cf-turnstile, [data-sitekey], iframe[src*="captcha" i], iframe[src*="turnstile" i]')
      || /verify you are human|i'?m not a robot|enter the captcha|solve the captcha|security check/i.test(text)) {
    return {kind: 'captcha', detail: 'CAPTCHA widget or wording found on page'};
  }
  if (q('input[autocomplete="one-time-code"], input[name*="otp" i], input[id*="otp" i]')
      || /enter (the )?otp|one[- ]time (password|pin)|verification code sent/i.test(text)) {
    return {kind: 'otp', detail: 'OTP prompt found on page'};
  }
  if (q('input[type="password"]')) {
    return {kind: 'login', detail: 'Login form (password field) found on page'};
  }
  return null;
}
"""

BLOCKING_STATUSES = {401: "login", 403: "blocked_http", 429: "blocked_http"}


def detect_challenge(page, http_status):
    """Return (kind, detail) if the page is gated, else None."""
    found = page.evaluate(_DETECT_JS)
    if found:
        return found["kind"], found["detail"]
    for frame in page.frames[1:]:
        if "captcha" in (frame.url or "").lower():
            return "captcha", f"CAPTCHA iframe: {frame.url[:80]}"
    if http_status in BLOCKING_STATUSES:
        return BLOCKING_STATUSES[http_status], f"HTTP {http_status} from server"
    return None
