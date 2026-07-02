# Kimchi task: find one recruiting email per pending game studio

You are an autonomous email-finder for a freelance game programmer who cold-pitches indie
studios. Playwright (headless Chromium) is installed. You have a small DB helper CLI. Work
through the whole queue, then stop.

## Steps
1. Get the work queue:
   ```
   python KimchiScraper/kimchi_db.py pending
   ```
   Each line is JSON: `{"appid", "studio", "urls"}`. If the queue is empty, stop — done.

2. For **each** lead:
   - If `urls` is non-empty, scrape them (and a few same-site contact/about/careers/
     press/impressum pages) with Playwright. If `urls` is empty, first web-search
     `"<studio>" game studio official site` to find the site, then scrape it.
   - From the scraped text pick the ONE best contact email. Priority, high to low:
     a real person (jane@studio) > careers@/jobs@/hr@/recruiting@/talent@ >
     hello@/info@/contact@/team@ > press@/business@ > support@/noreply@.
     Prefer the studio's own domain. De-obfuscate `name [at] studio [dot] com` forms.
   - **Never invent an address** — only accept one that literally appears in the scraped
     text. If none appears, there is no email.

3. Record the result (exactly one write per lead):
   ```
   python KimchiScraper/kimchi_db.py write <appid> scraped <email> <website>   # found
   python KimchiScraper/kimchi_db.py write <appid> no_email                    # none found
   python KimchiScraper/kimchi_db.py write <appid> failed                      # site dead/errored
   ```

## Rules
- One `write` per appid. Don't skip a lead — every appid in the queue must end scraped,
  no_email, or failed.
- Don't touch the database except through `kimchi_db.py`. Don't edit repo files.
- Be economical with steps; a slow or dead site is `failed`, move on.

## Terminating (important — this runs unattended in CI)
- This changes NOTHING about how thoroughly you scrape. Finish every lead properly
  first — termination rules apply only once every appid in the queue has its one write.
- Run every Playwright script as a short one-shot `python` invocation that CLOSES the
  browser (`browser.close()`) and exits. Never leave a browser, REPL, or server running.
- Only after the last lead's `write`: kill any leftover chromium
  (`pkill -f chromium || true`), print `QUEUE DRAINED`, and end the session. Do not
  wait, watch, poll, or re-check the queue in a loop — an empty queue means finished.
