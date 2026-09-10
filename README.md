# YCN's auto updater script

- **`update_game_data.py`** — downloads the site's JavaScript data bundle and extracts it into clean `unitInfo.js` / `equipInfo.js` data files, with validation, round-trip checks against the parser, and atomic writes. It adds new data — new units and equips get added on top of what you already have, it doesn't replace the whole dataset.

## How it works

Curious what `update_game_data.py` actually does? Here's the whole pipeline, start to finish.

### The core idea

The GS database site is a React app. When you open it, your browser downloads one big JavaScript file that has **the entire unit and equip database baked inside it** as two arrays.

### Step by step

**1. Find the current bundle**

The bundle's filename has a hash in it — `main.24d01070.js` — and that hash **changes every time the site updates** (which is exactly when you'd want fresh data). So the script can't hardcode the URL. It first fetches the site's homepage HTML, reads the *current* `main.<hash>.js` filename out of it, then downloads that. Always points at the live bundle, never a stale one.

**2. Extract the two arrays**

Inside the ~3.9MB bundle, units live in a variable `Nb=[...]` and equips in `au=[...]`. The script finds each one and walks bracket-by-bracket (respecting strings, so a `]` inside a name doesn't fool it) to grab the exact `[ ... ]` block.

**3. Parse it**

This is the tricky part. The site's code is **minified** — compressed in ways hand-written files never are:

- Floats written as `.52` instead of `0.52`
- Numbers like `3e3` instead of `3000`
- `!0` / `!1` instead of `true` / `false`
- Unicode as `\u03a9` (that's the `Ω` in "EDEN-typeΩ"), including split emoji
- One unit even built piece-by-piece with a helper function instead of written plainly

The script reuses **the parser in this repo** (`core/js_parser.py`) and teaches it to understand all those minified forms — without changing the parser itself. So whatever the script can read, anything using `core/game_data.py` can too.

**4. Validate before touching anything — this is the safety net**

Nothing gets written until the fresh data passes every check:

- Both arrays parsed to non-empty lists
- Every record has an id and a name
- The count is within 70% of what's already on disk (a sudden collapse = a partial download or the site broke → refuse)
- The generated file is **round-tripped back through the parser** to confirm the same record count — if it couldn't be loaded again, it's rejected

**5. Write — carefully**

Only *then* does it:

- Write the new `unitInfo.js` / `equipInfo.js`
- Clear the parse cache so a running bot re-reads fresh
- And it **only writes if the data actually changed** — an identical day does nothing

If *anything* fails at any step, your existing files are left exactly as they were.

### Running it on a schedule

The code runs 24/7, checks every 60 minutes, forever — it only writes when the data actually changed.

---

**The one-sentence version:** it downloads the site's public JavaScript bundle, pulls the unit/equip arrays out of it, checks the data is sane in three different ways, and only then swaps it into the data files — backing up the old ones first.

### See it yourself in DevTools

Want to look at the exact file the script reads? It's just a normal request in your browser's network tab:

1. Open [grandsummoners.info](https://www.grandsummoners.info) in your browser.
2. Press **F12** (or right-click the page → **Inspect**) to open DevTools, then click the **Network** tab.
3. Refresh the page so the requests appear, and find **`main.<hash>.js`** in the list — e.g. `main.24d01070.js`. (Clicking the **JS** filter button makes it easier to spot.)
4. Click that row and open the **Response** tab — that's the full bundle, the same one the script downloads. It's minified (one long line of compressed code), which is why it's hard to read directly. Search it (`Ctrl+F`) for `Nb=` and you'll land right on the unit data array; `au=` is the equips.

The **Headers** tab shows the same request URL the script builds from the homepage HTML: `https://www.grandsummoners.info/static/js/main.<hash>.js`.
