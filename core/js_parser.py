# js_parser.py
# unitInfo.js / equipInfo.js are hand-written JS (unquoted keys, backticks,
# comments, trailing commas) - this reads the `const X = [ ... ]` array
# literal directly, no separate JSON build step needed.

import os
import re
import json
import hashlib

class JsDataParseError(Exception):
    pass

# Compiled once at module load; matched with `pos=` so no substring slicing
# happens on every call (slicing a multi-MB string per token adds up fast).
_WHITESPACE_AND_COMMENTS_RE = re.compile(r"(?:[ \t\r\n]+|//[^\n]*|/\*.*?\*/)*", re.DOTALL)
_IDENTIFIER_RE = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
_NUMBER_RE = re.compile(r"-?\d+\.\d+|-?\d+")
_STRING_ESCAPE_MAP = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", "`": "`", '"': '"', "'": "'"}

class JsObjectLiteralParser:
    def __init__(self, source_text: str):
        self.text = source_text
        self.length = len(source_text)
        self.position = 0

    def peek_char(self) -> str:
        return self.text[self.position] if self.position < self.length else ""

    def skip_whitespace_and_comments(self) -> None:
        # One regex sweep instead of stepping one character at a time.
        self.position = _WHITESPACE_AND_COMMENTS_RE.match(self.text, self.position).end()

    def expect_char(self, expected_char: str) -> None:
        self.skip_whitespace_and_comments()
        if self.peek_char() != expected_char:
            snippet = self.text[max(0, self.position - 40):self.position + 40]
            raise JsDataParseError(
                f"Expected '{expected_char}' at position {self.position}, near: ...{snippet!r}..."
            )
        self.position += 1

    def parse_value(self):
        self.skip_whitespace_and_comments()
        current_char = self.peek_char()

        if current_char == "{":
            return self.parse_object()
        if current_char == "[":
            return self.parse_array()
        if current_char in ("`", '"', "'"):
            return self.parse_string()
        if current_char == "-" or current_char.isdigit():
            return self.parse_number()

        match = _IDENTIFIER_RE.match(self.text, self.position)
        if match:
            word = match.group(0)
            self.position = match.end()
            if word == "true":
                return True
            if word == "false":
                return False
            if word == "null":
                return None
            return word

        return None  # empty placeholder value (e.g. "id: ,")

    def parse_object(self) -> dict:
        self.expect_char("{")
        result: dict = {}
        while True:
            self.skip_whitespace_and_comments()
            if self.peek_char() == "}":
                self.position += 1
                break

            key = self.parse_key()
            self.skip_whitespace_and_comments()
            self.expect_char(":")
            value = self.parse_value()
            result[key] = value

            self.skip_whitespace_and_comments()
            if self.peek_char() == ",":
                self.position += 1
                continue
            if self.peek_char() == "}":
                self.position += 1
                break
            snippet = self.text[max(0, self.position - 40):self.position + 40]
            raise JsDataParseError(f"Expected ',' or '}}' at position {self.position}, near: ...{snippet!r}...")
        return result

    def parse_array(self) -> list:
        self.expect_char("[")
        result: list = []
        while True:
            self.skip_whitespace_and_comments()
            if self.peek_char() == "]":
                self.position += 1
                break
            result.append(self.parse_value())
            self.skip_whitespace_and_comments()
            if self.peek_char() == ",":
                self.position += 1
                continue
            if self.peek_char() == "]":
                self.position += 1
                break
            snippet = self.text[max(0, self.position - 40):self.position + 40]
            raise JsDataParseError(f"Expected ',' or ']' at position {self.position}, near: ...{snippet!r}...")
        return result

    def parse_key(self) -> str:
        self.skip_whitespace_and_comments()
        current_char = self.peek_char()
        if current_char in ("`", '"', "'"):
            return self.parse_string()
        match = _IDENTIFIER_RE.match(self.text, self.position)
        if not match:
            snippet = self.text[max(0, self.position - 40):self.position + 40]
            raise JsDataParseError(f"Expected object key at position {self.position}, near: ...{snippet!r}...")
        key = match.group(0)
        self.position = match.end()
        return key

    def parse_string(self) -> str:
        quote_char = self.peek_char()
        content_start = self.position + 1

        # Fast path: no backslash escapes at all in the actual data files,
        # so the vast majority of strings can be located with a single
        # C-speed str.find() instead of a per-character Python loop.
        close_index = self.text.find(quote_char, content_start)
        if close_index != -1:
            segment = self.text[content_start:close_index]
            if "\\" not in segment:
                self.position = close_index + 1
                return segment.strip()

        # Slow path fallback: handles backslash escapes correctly, in case
        # a future data update introduces one.
        self.position = content_start
        characters: list = []
        while True:
            if self.position >= self.length:
                raise JsDataParseError("Unterminated string literal")
            current_char = self.text[self.position]
            if current_char == "\\" and self.position + 1 < self.length:
                next_char = self.text[self.position + 1]
                characters.append(_STRING_ESCAPE_MAP.get(next_char, next_char))
                self.position += 2
                continue
            if current_char == quote_char:
                self.position += 1
                break
            characters.append(current_char)
            self.position += 1
        return "".join(characters).strip()

    def parse_number(self):
        match = _NUMBER_RE.match(self.text, self.position)
        if not match:
            raise JsDataParseError(f"Expected number at position {self.position}")
        number_text = match.group(0)
        self.position = match.end()
        return float(number_text) if "." in number_text else int(number_text)

def parse_js_data_file(file_path: str, variable_name: str) -> list:
    """Reads `const <variable_name> = [ ... ];` out of a .js data file and
    returns it as a list of plain Python dicts."""
    with open(file_path, "r", encoding="utf-8") as file:
        source_text = file.read()

    declaration_match = re.search(rf"const\s+{variable_name}\s*=\s*\[", source_text)
    if not declaration_match:
        raise JsDataParseError(f"Could not find declaration for '{variable_name}' in {file_path}")

    array_start = declaration_match.end() - 1  # position of the '['
    parser = JsObjectLiteralParser(source_text)
    parser.position = array_start
    return parser.parse_array()

def _file_content_hash(file_path: str) -> str:
    """Fast content hash used to decide whether the cache is still valid.
    Deliberately NOT mtime-based - a `git pull` deploy commonly resets file
    modification times even when the content is byte-identical, which was
    silently forcing a full multi-MB re-parse on every single deploy."""
    hasher = hashlib.md5()
    with open(file_path, "rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()

def load_js_data_with_cache(file_path: str, variable_name: str) -> list:
    """Loads a .js data file via a cached JSON copy when the .js source's
    content hasn't actually changed since it was cached (much cheaper than
    re-parsing raw JS every restart). Cache lives at "<file_path>.cache.json"."""
    cache_path = f"{file_path}.cache.json"
    source_hash = _file_content_hash(file_path)

    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as cache_file:
                cached = json.load(cache_file)
            if isinstance(cached, dict) and cached.get("_source_hash") == source_hash and "records" in cached:
                return cached["records"]
        except (json.JSONDecodeError, OSError, KeyError):
            pass  # corrupt/incompatible cache - fall through and re-parse

    parsed_records = parse_js_data_file(file_path, variable_name)
    try:
        with open(cache_path, "w", encoding="utf-8") as cache_file:
            json.dump({"_source_hash": source_hash, "records": parsed_records}, cache_file, ensure_ascii=False)
    except OSError:
        pass
    return parsed_records

def cleanup_orphaned_caches(directory: str = ".") -> int:
    """Deletes any '<name>.cache.json' file whose source file no longer
    exists in `directory` (e.g. after a data file gets renamed) - keeps
    stale cache files from silently accumulating over time. Returns how
    many were removed."""
    removed = 0
    try:
        entries = os.listdir(directory)
    except OSError:
        return 0
    for entry in entries:
        if not entry.endswith(".cache.json"):
            continue
        source_name = entry[: -len(".cache.json")]
        if source_name and not os.path.exists(os.path.join(directory, source_name)):
            try:
                os.remove(os.path.join(directory, entry))
                removed += 1
            except OSError:
                pass
    return removed
