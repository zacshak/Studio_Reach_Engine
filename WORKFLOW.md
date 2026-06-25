# Steam SRE — Studio Reach Engine: daily workflow

All steps run through the one launcher at repo root: `python SRE.py <command>`.

---

### 1. Discover — `SRE --discover`
Runs the discovery engine (`run_daily.py`):
- Poll Steam for all games, diff against the previous `known_comingsoon`, store the new
  appids in a txt file, and update `known_comingsoon` with the full current set.
- Fetch details for every game in the new txt file and store them in the `newly_added` table.

Side effects:
- `scrape_tracker` table is updated and seeded (`seeded` if Steam gave an email, else `pending`).
- `Approval_Pending_Games/` (seeded) and `No_Mail_Games/` (pending) folders are filled with
  each game's data + screenshots + a 2×2 sprite sheet.

### 2. Triage with a Coding Agent
Use a coding agent to scan the game folders and return a list of irrelevant game appids.
-open a claude code instance in path "A:\Game_Job_Research\Leads_Reviewer\Studios_To_Review"
-prompt to inject =
{
Leads Reviewer
context : 
The only folders u will access to are  No_Mail_Games and Approval_Pending_Games.
I'm looking for good games to pitch my game programming skill as a freelance/contract service.  Act as a Games Reviewer/filterer. 
 The folders No_Mail_Games and Approval_Pending_Games contain many games data. I want you to review all the subfolders , JSON files and all the 
 Spritesheet png image files(!!Important,  Spritesheet png Image gives the best context of a game so analyzing all Spritesheet image per game  is a MUST), after each 
 review put the game in either allowed or reject bucket. 
- Reject bucket is games that are desktop apps/utilities, games that comes under Dating Sim / Romance , Visual Novel / Interactive Fiction , 2d games ,
 games which can be built by a single developer and AI slops.  
-  Allowed bucket : Is all other games who might benefit from hiring a developer.
-At last  I want you to return the rejected games appid in a list. The output format must be like this  : Rejected Games = [4858620, 4819850...]  
}


### 3. Review-before-delete — `SRE --delete [1231,1321,..]`
Opens a GUI showing only the appids you listed. Review each, then delete — removes the
rows from both tables and the media folder in whichever store it lives.

### 4. Game-Approval review — `SRE --review`
Opens the reviewer GUI. In the **Game-Approval** section, manually review games:
- Accept → `mail_status = 'writing'`
- Reject → deleted

### 5. No-Mail review — `SRE --review`
Same GUI, **No-Mail** section. Reject-only (no email to send to):
- Reject → deleted

### 6. List no-seed websites — `SRE --noseed-urls`
Prints the website URLs of games with `scrape_status == 'pending'` that have a website —
the sites to scrape for an email.

### 7. Scrape + ingest emails — `SRE --ingest-mailids '...'`
Use a coding agent to scrape those sites and fetch the email, then feed the results back:
```
python SRE.py --ingest-mailids '{"url": "eleosgames.ca", "email": "support@eleosgames.com"}, {"url": "www.ki-nodes.com/games", "email": "n@gmail.com"}'
```
Only items with a filled email are applied: writes the email, flips `pending → seeded`, and
moves the folder `No_Mail_Games → Approval_Pending_Games`.

### 8. Draft cold mails with a Coding Agent
Use a coding agent to scan the game data in `Approval_Pending_Games/` and write a cold mail
for each game from the 4 predefined templates (txt file).

### 9. Mail-Approval review — `SRE --review`
Same GUI, **Mail-Approval** section. Manually review the drafted mails:
- Accept → `mail_status = 'scheduled'`
- Reject → deleted

### 10. Send — `SRE --send-mails`
Sends the scheduled mails with a randomized time interval between each. On every successful
send the status flips to `'sent'` and the local media data is deleted.
(`--dry-run` to preview, `--limit N` to cap the batch.)

### 11. Review replies — `SRE --review-mails`
Checks every `Sent` lead against your Gmail inbox (IMAP, same App Password as sending). If a
reply from that lead's address is found, its status flips `sent → replied`. Read-only on
Gmail; only the DB status changes. Safe to re-run anytime.
