import { z } from 'zod';

// The owner's standing triage rules. TRIAGE.md (repo root) repeats this text verbatim;
// a test keeps the two identical. Change both together.
export const TRIAGE_RULES = `Goal: find indie Steam games whose small team could plausibly use another gameplay programmer.

Evidence
- Every game's sprite sheet MUST be looked at (get_game_media). No decision is made from the description alone, and description claims ("deep systems", "online PvP") count only when the screenshots show them.
- If the sheet leaves any doubt, pull 3 to 5 more screenshots with get_game_image before deciding. Doubt means "pull more images", not "flag".
- Look at one game at a time, at full size. Do not batch several sheets into one downscaled grid.
- Missing media, or screenshots that clearly do not match the game: submit nothing for it and report it.

Pass
- 3D or well made games with real time gameplay and visible production, including co-op sims, vehicle or delivery games, crafting plus combat, and shooters.
- Genre labels like "simulator", "job" or "management" do not count against a game; judge what the screenshots show.
- Examples the owner wants kept: Dead End Delivery (first person zombie horde shooter with a bus), Fast Food Together (realistic 3D co-op restaurant sim), Delivery Simulator Demo (3D city driving and delivery), Blacksmithkin (first person forging plus arena combat), Destroy Dracula (3D squad action).

Flag only clear misses
- UI, menu or grid dominated games; card, idle or visual novel games; tiny 2D or puzzle games; prototypes or empty scenes; and the casual sorting sim type like All That Junk.
- Examples the owner rejected: All That Junk (scrap sorting sim), Badger Tactics (pixel grid tactics with gacha menus), Coalateral Damage (2D mine management with heavy UI), Dirtborn (simple top down 2D ant colony), Drift Signal (space station builder that is mostly UI panels), Gladiatoria (pixel arena management with menus).

Timing
- Flag games already released, or releasing on or before the cutoff date (today plus 3 months). "Coming soon", "To be announced", an empty date, or a window after the cutoff are fine on timing.

Reasons
- One short sentence saying what the screenshots show (or the date), and whether extra screenshots were pulled.`;

export function triageCutoff(today = new Date()) {
  const d = new Date(Date.UTC(today.getUTCFullYear(), today.getUTCMonth() + 3, today.getUTCDate()));
  return d.toISOString().slice(0, 10);
}

export function triagePrompt(count, today = new Date()) {
  const date = today.toISOString().slice(0, 10);
  return `Triage ${count} Pending games with the sre-database MCP tools. Today is ${date}; the release cutoff is ${triageCutoff(today)}.

1. Find untriaged games: query "SELECT appid, game_name, release_date FROM scrape_tracker WHERE Mail_status='Pending' AND triage_decision IS NULL ORDER BY appid LIMIT ${count}".
2. For each game, one at a time:
   a. get_game_media(appid) and look at the sprite sheet. Always, even when the date alone decides it.
   b. If anything is unclear, get_game_image for 3 to 5 more of its available_images before deciding.
   c. Decide pass or flag with the rules below.
3. After every 20 decisions, call submit_triage with criteria = the rules below and the results. Games in its skipped list were not saved; report them.
4. Do not delete, accept or send anything.
5. Finish with a report: counts of pass, flag and skipped; every passed game (appid, name, why); every game where you pulled extra screenshots; skipped games with reasons; borderline flags worth a human look.

${TRIAGE_RULES}`;
}

export function registerTriagePrompt(server) {
  server.registerPrompt('triage_games', {
    title: 'Triage Pending games',
    description: 'Triage N Pending games with this agent using the owner\'s standing rules: every sprite sheet looked at, 3 to 5 more screenshots on any doubt, pass real time games with visible production. Saves pass/flag only; nothing is deleted or sent.',
    argsSchema: { count: z.string().regex(/^\d+$/).describe('How many games to triage, e.g. 100') }
  }, ({ count }) => ({ messages: [{ role: 'user', content: { type: 'text', text: triagePrompt(Number(count)) } }] }));
}
