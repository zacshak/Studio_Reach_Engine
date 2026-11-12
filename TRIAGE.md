# Triage rules

These are the owner's standing rules for triaging Pending games. Any agent triaging
through the MCP server gets them with the `triage_games` prompt (count = how many games);
the same text is in `db-mcp/triage-rules.js`, and a test keeps both identical.

Goal: find indie Steam games whose small team could plausibly use another gameplay programmer.

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
- One short sentence saying what the screenshots show (or the date), and whether extra screenshots were pulled.
