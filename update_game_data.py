import os
import re
import sys
import json
import time
import shutil
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

# (site_var, out_file, bot_const_name, min_expected)
TARGETS = [
    ("Nb", "unitInfo.js", "UnitInformation", 400),
    ("au", "equipInfo.js", "EquipInformation", 400),
]

# Refuse a fetch with fewer than this fraction of the current record count
# (catches partial downloads / the site dropping data).
MIN_RATIO_OF_CURRENT = 0.7


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


def extract_array_literal(bundle: str, var_name: str) -> str:
    # Return the raw '[ ... ]' text of `<var_name>=[{...}]`, matched by walking
    # brackets so nested brackets and brackets inside strings are handled.
    m = re.search(r'\b' + re.escape(var_name) + r'=\[\{', bundle)
    if not m:
        raise RuntimeError(f"could not find array '{var_name}=[{{' in the bundle")

    start = m.end() - 2  # the opening '['
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
    raise RuntimeError(f"unterminated array literal for '{var_name}'")


def parse_array(literal: str) -> list:
    parser = _SiteArrayParser(literal)
    parser.position = 0
    parser.skip_whitespace_and_comments()
    return parser.parse_array()


def current_record_count(path: str, const_name: str) -> int:
    # Records in the existing file (0 if missing/unreadable).
    if not os.path.exists(path):
        return 0
    try:
        return len(js_parser.parse_js_data_file(path, const_name))
    except Exception:
        return 0


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
    staged = []  # (out_path, bak_path, text, const_name, count, current)
    for var_name, out_file, const_name, min_expected in TARGETS:
        out_path = os.path.join(target_dir, out_file)
        try:
            records = parse_array(extract_array_literal(bundle, var_name))
        except (RuntimeError, js_parser.JsDataParseError, ValueError) as e:
            # JsDataParseError (and ValueError from a malformed \uXXXX escape)
            # are not RuntimeError subclasses, but mean exactly the same thing
            # here: the site's data can't be trusted, so refuse cleanly
            # instead of letting a traceback escape run_update's contract.
            return {"ok": False, "changed": False, "summary": f"{out_file}: parse failed",
                    "details": details, "error": f"{out_file}: {e}"}

        current = current_record_count(out_path, const_name)
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

        details.append(f"{out_file}: {len(records)} records (was {current})")
        staged.append((out_path, out_path + ".bak", text, const_name, len(records), current))

    if dry_run:
        return {"ok": True, "changed": False,
                "summary": "Dry run: validated, wrote nothing.", "details": details, "error": None}

    # Write only files whose content actually changed. Each file is written
    # to a temp name and swapped in with os.replace, so a crash or disk-full
    # mid-write can never leave a truncated data file behind, and the window
    # where one file is updated but its sibling isn't shrinks to the instant
    # between the two renames. Any write failure returns an error dict
    # instead of raising, keeping run_update's never-raises contract.
    written = []
    for out_path, bak_path, text, const_name, count, current in staged:
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
            if os.path.exists(out_path):
                shutil.copy2(out_path, bak_path)
            os.replace(tmp_path, out_path)
        except OSError as e:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            return {"ok": False, "changed": bool(written),
                    "summary": f"{os.path.basename(out_path)}: write failed",
                    "details": details, "error": str(e)}

        cache_path = out_path + ".cache.json"
        if os.path.exists(cache_path):
            try:
                os.remove(cache_path)
            except OSError:
                pass
        written.append(os.path.basename(out_path))

    if not written:
        return {"ok": True, "changed": False,
                "summary": "Already up to date - site data matches current files.",
                "details": details, "error": None}
    return {"ok": True, "changed": True,
            "summary": f"Updated: {', '.join(written)}.",
            "details": details, "error": None}


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
    ap.add_argument("--loop", type=int, default=0, metavar="MINUTES",
                    help="run forever: fetch, then re-check every MINUTES (0 = run once and exit)")
    args = ap.parse_args()

    try:
        while True:
            print(f"[{_now()}] Fetching from {SITE} ...")
            result = run_update(target_dir=args.dir, dry_run=args.dry_run)
            for line in result["details"]:
                print(f"[{_now()}] OK {line}")
            status = "DONE" if result["ok"] else "ERROR"
            print(f"[{_now()}] {status} {result['summary']}")
            if not result["ok"]:
                print(f"[{_now()}] ERROR: {result['error']}", file=sys.stderr)

            if not args.loop:
                if not result["ok"]:
                    return 2 if result["summary"].startswith("Fetch") else 1
                return 0
            print(f"[{_now()}] Sleeping {args.loop} min ...")
            time.sleep(args.loop * 60)
    except KeyboardInterrupt:
        print(f"[{_now()}] Stopped.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
