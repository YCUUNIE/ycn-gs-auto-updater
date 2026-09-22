import os
import re
import sys
import json
import time
import argparse
import urllib.request
import urllib.error

# Reuse the bot's own parser so "can the bot read this?" is checked with the
# same code the bot runs, not a copy.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core import js_parser  # noqa: E402


class _SiteArrayParser(js_parser.JsObjectLiteralParser):
    # The bot's parser only reads the plain number/string forms the
    # hand-written files use. The site is minified, so this subclass adds the
    # minified forms (.52, 3e3, !0/!1, void 0, \uXXXX escapes, and one
    # helper-built record). js_parser itself is untouched, and the files we
    # write use plain JSON, so the bot never sees any of these forms.

    _JS_NUMBER_RE = re.compile(r"-?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")

    def parse_value(self):
        self.skip_whitespace_and_comments()
        c = self.peek_char()
        # !0 -> true, !1 -> false, void 0 -> null
        if c == "!":
            nxt = self.text[self.position + 1:self.position + 2]
            if nxt == "0":
                self.position += 2
                return True
            if nxt == "1":
                self.position += 2
                return False
        if self.text.startswith("void", self.position):
            m = re.compile(r"void\s+0").match(self.text, self.position)
            if m:
                self.position = m.end()
                return None
        # Numbers, including leading-dot (.52) and exponent (3e3) forms the
        # base parser doesn't handle.
        if c == "-" or c == "." or c.isdigit():
            m = self._JS_NUMBER_RE.match(self.text, self.position)
            if m:
                self.position = m.end()
                text = m.group(0)
                if any(ch in text for ch in ".eE"):
                    return float(text)
                return int(text)
        # One unit is built imperatively as (Bb={...}, he(Bb,"stats",{...}), ..., Bb)
        # instead of a plain literal. Replay it to get the finished object.
        if c == "(":
            return self._parse_paren_sequence()
        return super().parse_value()

    def _parse_paren_sequence(self):
        # Walk a "(assign, helper(...), ..., ident)" sequence and return the
        # object it builds.
        self.position += 1  # consume '('
        bindings = {}
        result = None

        while True:
            self.skip_whitespace_and_comments()
            ch = self.peek_char()
            if ch == ")" or ch == "":
                if ch == ")":
                    self.position += 1
                break

            ident_m = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*").match(self.text, self.position)
            if ident_m:
                name = ident_m.group(0)
                after = ident_m.end()
                rest = self.text[after:]
                assign_m = re.match(r"\s*=\s*", rest)
                call_m = re.match(r"\s*\(", rest)
                if assign_m:
                    # ident = <value>
                    self.position = after + assign_m.end()
                    value = self.parse_value()
                    bindings[name] = value
                    result = value
                elif call_m:
                    # helper(target, "key", value) -> target[key] = value
                    self.position = after + call_m.end()
                    args = self._parse_call_args()
                    target = self._resolve(args[0], bindings) if args else None
                    if len(args) >= 3 and isinstance(target, dict) and isinstance(args[1], str):
                        target[args[1]] = args[2]
                        result = target
                    elif target is not None:
                        result = target
                else:
                    # bare identifier -> its bound value
                    self.position = after
                    result = bindings.get(name, result)
            else:
                result = self.parse_value()

            self.skip_whitespace_and_comments()
            if self.peek_char() == ",":
                self.position += 1
                continue
            if self.peek_char() == ")":
                self.position += 1
                break
            break  # unexpected - stop rather than loop

        return result

    @staticmethod
    def _resolve(value, bindings):
        # A bare identifier arg is a reference; resolve it to the bound object.
        if isinstance(value, str) and value in bindings:
            return bindings[value]
        return value

    def _parse_call_args(self) -> list:
        # Parse "arg, arg, ...)" after the call's opening '(' is consumed.
        args = []
        while True:
            self.skip_whitespace_and_comments()
            if self.peek_char() == ")":
                self.position += 1
                break
            if self.peek_char() == "":
                break
            args.append(self.parse_value())
            self.skip_whitespace_and_comments()
            if self.peek_char() == ",":
                self.position += 1
                continue
            if self.peek_char() == ")":
                self.position += 1
                break
            break
        return args

    def parse_string(self) -> str:
        # Like the base parser but decodes \uXXXX / surrogate pairs / \xXX,
        # which the minified data uses (e.g. "EDEN-type\u03a9" -> "EDEN-typeΩ").
        quote_char = self.peek_char()
        start = self.position + 1
        i = start
        n = self.length
        text = self.text
        out = []
        while i < n:
            ch = text[i]
            if ch == "\\" and i + 1 < n:
                esc = text[i + 1]
                if esc == "u":
                    if text[i + 2:i + 3] == "{":  # \u{XXXXX}
                        end = text.find("}", i + 3)
                        if end != -1:
                            out.append(chr(int(text[i + 3:end], 16)))
                            i = end + 1
                            continue
                    hexs = text[i + 2:i + 6]
                    if len(hexs) == 4:
                        cp = int(hexs, 16)
                        # Combine a high+low surrogate pair into one char, else
                        # each half is a lone surrogate UTF-8 can't encode.
                        if 0xD800 <= cp <= 0xDBFF and text[i + 6:i + 8] == "\\u":
                            lo = text[i + 8:i + 12]
                            if len(lo) == 4:
                                lo_cp = int(lo, 16)
                                if 0xDC00 <= lo_cp <= 0xDFFF:
                                    combined = 0x10000 + ((cp - 0xD800) << 10) + (lo_cp - 0xDC00)
                                    out.append(chr(combined))
                                    i += 12
                                    continue
                        out.append(chr(cp))
                        i += 6
                        continue
                elif esc == "x":
                    hexs = text[i + 2:i + 4]
                    if len(hexs) == 2:
                        out.append(chr(int(hexs, 16)))
                        i += 4
                        continue
                out.append(js_parser._STRING_ESCAPE_MAP.get(esc, esc))
                i += 2
                continue
            if ch == quote_char:
                self.position = i + 1
                return "".join(out).strip()
            out.append(ch)
            i += 1
        raise js_parser.JsDataParseError("Unterminated string literal")

SITE = "https://www.grandsummoners.info"
UA = "Mozilla/5.0 (GrandSummonersBot data updater; contact: bot maintainer)"

# (marker_keys, out_file, bot_const_name, min_expected)
#
# The bundle binds each array to a minified one- or two-letter name, and the
# minifier reassigns those names on every site rebuild - equips went from "au"
# to "su" in Sept 2026 and every run failed until this was changed. So we don't
# look for a name at all. Each target lists a few keys that only that dataset's
# records carry at the top level, and we match on those.
TARGETS = [
    (("name", "attribute", "tier"), "unitInfo.js", "UnitInformation", 400),
    (("name", "location", "star"), "equipInfo.js", "EquipInformation", 400),
]

# Refuse a fetch with fewer than this fraction of the current record count
# (catches partial downloads / the site dropping data).
MIN_RATIO_OF_CURRENT = 0.7

# How often `--loop` re-checks when no interval is given: once every 24 hours.
DEFAULT_LOOP_MINUTES = 1440


def _http_get(url: str, timeout: int = 60) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


def find_main_bundle_url(html: str) -> str:
    # The bundle name is content-hashed and changes on every site rebuild, so
    # read the current one from the HTML instead of hardcoding it.
    m = re.search(r'/static/js/main\.[0-9a-f]+\.js', html)
    if not m:
        raise RuntimeError("could not find main.<hash>.js in the site HTML - the site layout may have changed")
    return SITE + m.group(0)


# The real datasets are ~1.5MB each. The bundle's other array literals - nav
# links, tier labels, dropdown options - are a few hundred bytes, so anything
# this small is noise, not data.
_MIN_DATA_ARRAY_CHARS = 10000

# How far into a literal we look for the marker keys. Comfortably more than one
# record, so a record missing an optional key doesn't cost us the match.
_SIGNATURE_HEAD_CHARS = 4000

_ARRAY_START_RE = re.compile(r"\b([A-Za-z_$][A-Za-z0-9_$]*)=\[\{")


def _marker_key_re(key: str) -> "re.Pattern":
    # Match `key:` used as an object key - quoted or not, opening an object or
    # following a comma - so we don't match it as a substring of some other
    # identifier elsewhere in the minified soup.
    return re.compile(r"""[{,]\s*["']?""" + re.escape(key) + r"""["']?\s*:""")


def _extract_array_at(bundle: str, start: int):
    # Walk the balanced array literal beginning at `start` (which must be its
    # '['), respecting strings so a bracket inside a name doesn't fool us.
    # Returns the literal text, or None if it never closes.
    i = start
    depth = 0
    n = len(bundle)
    in_str = None
    while i < n:
        c = bundle[i]
        if in_str is not None:
            if c == "\\":
                i += 2
                continue
            if c == in_str:
                in_str = None
        else:
            if c in ("'", '"', "`"):
                in_str = c
            elif c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    return bundle[start:i + 1]
        i += 1
    return None


def find_array_literal(bundle: str, signature, label: str) -> str:
    # Find the dataset whose records carry every key in `signature`, whatever
    # the minifier happened to call the variable this week. If more than one
    # array qualifies, the largest wins - the real dataset dwarfs anything that
    # could plausibly look like it.
    markers = [_marker_key_re(k) for k in signature]
    best = None
    seen = []

    for m in _ARRAY_START_RE.finditer(bundle):
        start = m.end() - 2  # the opening '['
        head = bundle[start:start + _SIGNATURE_HEAD_CHARS]
        if not all(mk.search(head) for mk in markers):
            seen.append(m.group(1))
            continue
        literal = _extract_array_at(bundle, start)
        if literal is None:
            seen.append(m.group(1) + " (unterminated)")
            continue
        if len(literal) < _MIN_DATA_ARRAY_CHARS:
            seen.append(m.group(1) + f" ({len(literal)} chars, too small)")
            continue
        if best is None or len(literal) > len(best[1]):
            best = (m.group(1), literal)

    if best is None:
        # Name every array we found and rejected, so the next diagnosis starts
        # with evidence instead of a guess.
        raise RuntimeError(
            f"no array in the bundle has records with all of {list(signature)} - "
            f"the site's data shape may have changed. "
            f"Arrays found and rejected: {', '.join(seen) if seen else 'none'}"
        )
    return best[1]


def parse_array(literal: str) -> list:
    parser = _SiteArrayParser(literal)
    parser.position = 0
    parser.skip_whitespace_and_comments()
    return parser.parse_array()


def read_current_file(path: str, const_name: str):
    # What's already on disk: (record count, set of names). The two are not
    # interchangeable - names are deduplicated, and equips ship 14 entries
    # called "???" plus a repeated name, so the name set runs ~14 short of the
    # record count on an unchanged file. The count is what gets reported and
    # validated; the names are only for diffing which entries came and went.
    # (0, empty set) if the file is missing or unreadable.
    if not os.path.exists(path):
        return 0, set()
    try:
        recs = js_parser.parse_js_data_file(path, const_name)
    except Exception:
        return 0, set()
    names = {r["name"].strip() for r in recs
             if isinstance(r.get("name"), str) and r["name"].strip()}
    return len(recs), names


def validate(records, min_expected: int, current_count: int, label: str) -> None:
    # Raise with a reason if the data isn't safe to write.
    if not isinstance(records, list) or not records:
        raise RuntimeError(f"{label}: parsed data is empty or not a list")
    if not all(isinstance(r, dict) for r in records):
        raise RuntimeError(f"{label}: parsed data contains non-object entries")

    missing_id = sum(1 for r in records if not r.get("id") and r.get("id") != 0)
    # A name that isn't a non-empty string (number, object, missing) counts as
    # missing - and must not crash the check, since this runs on third-party
    # data we don't control.
    missing_name = sum(
        1 for r in records
        if not isinstance(r.get("name"), str) or not r["name"].strip()
    )
    if missing_id:
        raise RuntimeError(f"{label}: {missing_id} record(s) have no id")
    if missing_name:
        raise RuntimeError(f"{label}: {missing_name} record(s) have no name")

    if len(records) < min_expected:
        raise RuntimeError(
            f"{label}: only {len(records)} records, expected at least {min_expected} - refusing"
        )
    if current_count and len(records) < current_count * MIN_RATIO_OF_CURRENT:
        raise RuntimeError(
            f"{label}: {len(records)} records is a big drop from the current {current_count} "
            f"(< {MIN_RATIO_OF_CURRENT:.0%}) - looks like a partial download or a site change, refusing"
        )


def render_file(const_name: str, records: list) -> str:
    # Emit `const <name> = [...];` - the shape the bot's parser expects. JSON is
    # a subset of that syntax, so it round-trips cleanly.
    body = json.dumps(records, ensure_ascii=False, indent=1)
    return (
        f"// AUTO-GENERATED by update_game_data.py from {SITE}\n"
        f"// Do not edit by hand - re-run the updater instead.\n"
        f"const {const_name} = {body};\n"
    )


def verify_roundtrip(text: str, const_name: str, expected_count: int, label: str, target_dir: str) -> None:
    # Write to a temp file and read it back with the bot's parser, so a file the
    # bot couldn't load is never kept.
    tmp = os.path.join(target_dir, f".roundtrip_check_{const_name}.js")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        got = js_parser.parse_js_data_file(tmp, const_name)
        if len(got) != expected_count:
            raise RuntimeError(
                f"{label}: round-trip mismatch - wrote {expected_count} records, "
                f"bot parser read back {len(got)}"
            )
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def run_update(target_dir: str = "", dry_run: bool = False) -> dict:
    # Fetch, parse, validate, and (unless dry_run) write the data files.
    # Returns {"ok", "changed", "summary", "details", "error"} rather than
    # raising, so callers (CLI and the daily task) can handle failures cleanly.
    target_dir = target_dir or os.path.dirname(os.path.abspath(__file__))

    try:
        html = _http_get(SITE + "/")
        bundle_url = find_main_bundle_url(html)
        bundle = _http_get(bundle_url)
    except (urllib.error.URLError, urllib.error.HTTPError, RuntimeError, OSError) as e:
        return {"ok": False, "changed": False, "summary": f"Fetch failed: {e}",
                "details": [], "error": str(e)}

    # Parse and validate both files before writing either, so we never leave a
    # fresh unit file beside a stale equip file.
    details = []
    staged = []  # (out_path, text, const_name, count, current, old_names, new_names)
    for signature, out_file, const_name, min_expected in TARGETS:
        out_path = os.path.join(target_dir, out_file)
        try:
            records = parse_array(find_array_literal(bundle, signature, out_file))
        except (RuntimeError, js_parser.JsDataParseError, ValueError) as e:
            # JsDataParseError (and ValueError from a malformed \uXXXX escape)
            # are not RuntimeError subclasses, but mean exactly the same thing
            # here: the site's data can't be trusted, so refuse cleanly
            # instead of letting a traceback escape run_update's contract.
            return {"ok": False, "changed": False, "summary": f"{out_file}: parse failed",
                    "details": details, "error": f"{out_file}: {e}"}

        current, old_names = read_current_file(out_path, const_name)
        try:
            validate(records, min_expected, current, out_file)
        except RuntimeError as e:
            return {"ok": False, "changed": False, "summary": f"{out_file}: validation refused",
                    "details": details, "error": str(e)}

        text = render_file(const_name, records)
        try:
            verify_roundtrip(text, const_name, len(records), out_file, target_dir)
        except (RuntimeError, js_parser.JsDataParseError) as e:
            return {"ok": False, "changed": False, "summary": f"{out_file}: round-trip failed",
                    "details": details, "error": str(e)}

        new_names = {r["name"].strip() for r in records
                     if isinstance(r.get("name"), str) and r["name"].strip()}
        details.append(f"{out_file}: {len(records)} records (was {current})")
        staged.append((out_path, text, const_name, len(records), current, old_names, new_names))

    if dry_run:
        return {"ok": True, "changed": False,
                "summary": "Dry run: validated, wrote nothing.", "details": details,
                "changes": [], "error": None}

    # Write only files whose content actually changed. Each file is written
    # to a temp name and swapped in with os.replace, so a crash or disk-full
    # mid-write can never leave a truncated data file behind, and the window
    # where one file is updated but its sibling isn't shrinks to the instant
    # between the two renames. Any write failure returns an error dict
    # instead of raising, keeping run_update's never-raises contract.
    #
    # No .bak copies: every byte here is re-fetchable from the site on demand,
    # and a stale backup beside a live data file is a trap, not a safety net.
    written = []
    changes = []  # readable per-file "what changed" lines
    for out_path, text, const_name, count, current, old_names, new_names in staged:
        existing = None
        if os.path.exists(out_path):
            try:
                with open(out_path, "r", encoding="utf-8") as f:
                    existing = f.read()
            except OSError:
                existing = None
        if existing == text:
            continue

        tmp_path = out_path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp_path, out_path)
        except OSError as e:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            return {"ok": False, "changed": bool(written),
                    "summary": f"{os.path.basename(out_path)}: write failed",
                    "details": details, "changes": changes, "error": str(e)}

        cache_path = out_path + ".cache.json"
        if os.path.exists(cache_path):
            try:
                os.remove(cache_path)
            except OSError:
                pass
        written.append(os.path.basename(out_path))

        # Name the entries that were added or removed. old_names is empty on a
        # first-ever run, where there is nothing meaningful to diff against.
        kind = "unit" if const_name == "UnitInformation" else "equip"
        if old_names:
            added = sorted(new_names - old_names)
            removed = sorted(old_names - new_names)
            if added or removed:
                parts = []
                if added:
                    parts.append(f"added {len(added)} {kind}(s): {_name_list(added)}")
                if removed:
                    parts.append(f"removed {len(removed)} {kind}(s): {_name_list(removed)}")
                changes.append("; ".join(parts))
            else:
                # Content changed but the name set didn't - a stat or skill
                # tweak on entries that were already there.
                changes.append(f"{kind} data updated (stats/skills changed, no new names)")

    if not written:
        return {"ok": True, "changed": False,
                "summary": "Already up to date - site data matches current files.",
                "details": details, "changes": [], "error": None}
    return {"ok": True, "changed": True,
            "summary": f"Updated: {', '.join(written)}.",
            "details": details, "changes": changes, "error": None}


def _name_list(names, limit: int = 12) -> str:
    # A readable, comma-joined name list, capped so a huge first-run diff
    # doesn't flood the log.
    if len(names) <= limit:
        return ", ".join(names)
    return ", ".join(names[:limit]) + f", and {len(names) - limit} more"


def _now() -> str:
    """Timestamp for log lines: HH:MM (24h, local time)."""
    from datetime import datetime
    return datetime.now().strftime("%H:%M")


def main() -> int:
    ap = argparse.ArgumentParser(description="Refresh unit/equip data from the GS site.")
    ap.add_argument("--dir", default=os.path.dirname(os.path.abspath(__file__)),
                    help="directory holding unitInfo.js / equipInfo.js (default: repo root)")
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch, parse and validate but do not write any file")
    # Bare --loop means the default cadence, once a day. The site publishes new
    # units and equips in batches, not continuously, so checking more often than
    # that is just traffic - pass an explicit --loop N if you want it anyway.
    ap.add_argument("--loop", type=int, nargs="?", default=0, const=DEFAULT_LOOP_MINUTES,
                    metavar="MINUTES",
                    help=f"run forever: fetch, then re-check every MINUTES "
                         f"(bare --loop = every {DEFAULT_LOOP_MINUTES // 60}h; omit entirely to run once and exit)")
    args = ap.parse_args()

    try:
        while True:
            print(f"[{_now()}] Fetching from {SITE} ...")
            result = run_update(target_dir=args.dir, dry_run=args.dry_run)
            for line in result["details"]:
                print(f"[{_now()}] OK {line}")
            for line in result.get("changes") or []:
                print(f"[{_now()}]    - {line}")
            status = "DONE" if result["ok"] else "ERROR"
            print(f"[{_now()}] {status} {result['summary']}")
            if not result["ok"]:
                print(f"[{_now()}] ERROR: {result['error']}", file=sys.stderr)

            if not args.loop:
                if not result["ok"]:
                    return 2 if result["summary"].startswith("Fetch") else 1
                return 0
            nap = (f"{args.loop // 60}h" if args.loop >= 60 and args.loop % 60 == 0
                   else f"{args.loop} min")
            print(f"[{_now()}] Sleeping {nap} ...")
            time.sleep(args.loop * 60)
    except KeyboardInterrupt:
        print(f"[{_now()}] Stopped.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
