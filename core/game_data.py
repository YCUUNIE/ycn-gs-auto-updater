# game_data.py
# Unit/equip data loading, lookups, and the text/formatting helpers shared
# across embeds.

import re
import sys
import difflib
from typing import Dict, List, Optional

from discord import app_commands

from . import config
from .js_parser import load_js_data_with_cache

# =========================
# GLOBALS / CACHE
# =========================

UNITS: List[dict] = []
UNITS_BY_NAME: Dict[str, dict] = {}
UNIT_NAMES: List[str] = []         # original casing
UNIT_NAMES_LOWER: List[str] = []   # lowercase for fast search

EQUIPS: List[dict] = []
EQUIPS_BY_NAME: Dict[str, dict] = {}
EQUIPS_BY_TRANSLATE: Dict[str, dict] = {}  # for equips whose `name` is Japanese but have an English `translate`
EQUIP_NAMES: List[str] = []        # original casing
EQUIP_NAMES_LOWER: List[str] = []  # lowercase for fast search

# Bumped every time the rosters are re-parsed (startup, /reloaddata, and the
# 15-minute data_reload_checker). Modules that build their own derived index
# over the name lists watch this to know when to rebuild - the lists are
# repopulated in place, so identity comparison can't detect a reload.
DATA_VERSION = 0

# =========================
# TEXT HELPERS
# =========================

def clean_text(value) -> str:
    if value is None:
        return "-"
    text = str(value).strip()
    return text if text else "-"

def to_int(value) -> Optional[int]:
    """Parse ints from values like 6, 6.0, '6', '6★', '6☆'."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        match = re.search(r"\d+", value)
        return int(match.group(0)) if match else None
    return None

def slot_type_label(slot_type) -> str:
    """Convert a slot/equip 'type' string or icon path into a readable label."""
    normalized = (slot_type or "").lower()
    if "phys" in normalized:                            return "Physical"
    if "mag" in normalized:                              return "Magic"
    if "supp" in normalized or "support" in normalized:  return "Support"
    if "def" in normalized:                              return "Defense"
    if "heal" in normalized:                             return "Heal"
    return "Unknown"

def type_label_with_emoji(slot_type) -> str:
    """Custom type emoji (e.g. ⚔️ for Physical); falls back to plain text
    only if unidentified. Don't use inside ```-fenced values - emoji don't
    render in code blocks."""
    label = slot_type_label(slot_type)
    return config.TYPE_EMOJI.get(label, label)

def format_skill(skill_text: Optional[str], break_value: Optional[int]) -> str:
    if not skill_text:
        return "-"
    cleaned = str(skill_text).strip()
    if not cleaned:
        return "-"
    if break_value:
        return f"[BREAK {break_value}] {cleaned}"
    return cleaned

# Units don't all have the same active skills (some have Cross Arts,
# Phantom Bullet, a Super Equip, split True Arts I/II, Revelation, etc.) -
# build_skill_fields() shows whichever of these are actually present.
KNOWN_SKILL_SLOTS = [
    ("skill",         "skillbreak",         "Skill"),
    ("arts",          "artsbreak",          "Arts"),
    ("truearts",      "trueartsbreak",      "True Arts"),
    ("truearts1",     "trueartsbreak1",     "True Arts I"),
    ("truearts2",     "trueartsbreak2",     "True Arts II"),
    ("superarts",     "superartsbreak",     "Super Arts"),
    ("crossarts",     "crossartsbreak",     "Cross Arts"),
    ("exarts",        "exartsbreak",        "EX Arts"),
    ("phantombullet", "phantombulletbreak", "Phantom Bullet"),
    ("superequip",    "superequipbreak",    "Super Equip"),
    ("revelation",    "revelationbreak",    "Revelation"),
]

def humanize_skill_key(key: str) -> str:
    """Fallback label for a skill key we don't already know about."""
    spaced = re.sub(r"(\d+)$", r" \1", key)               # trailing number -> " N"
    spaced = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", spaced)   # camelCase -> spaced
    return spaced.replace("_", " ").strip().title()

def format_revelation_skillset(revelation_data: dict) -> str:
    """Revelation is its own mini skillset (skill1/2/3, megaskill, megaarts,
    each with a matching *break key), rather than a single skill string."""
    revelation_slot_labels = [
        ("skill1", "skill1break", "Skill I"),
        ("skill2", "skill2break", "Skill II"),
        ("skill3", "skill3break", "Skill III"),
        ("megaskill", "megaskillbreak", "Mega Skill"),
        ("megaarts", "megaartsbreak", "Mega Arts"),
    ]
    lines: List[str] = []
    consumed_keys = set()
    for base_key, break_key, label in revelation_slot_labels:
        consumed_keys.update({base_key, break_key})
        text = revelation_data.get(base_key)
        if text:
            lines.append(f"**{label}**\n{format_skill(text, to_int(revelation_data.get(break_key)))}")
    for key, value in revelation_data.items():
        if key in consumed_keys or key.endswith("break") or not value:
            continue
        lines.append(f"**{humanize_skill_key(key)}**\n{format_skill(value, to_int(revelation_data.get(f'{key}break')))}")
    return "\n\n".join(lines) if lines else "-"

def build_skill_fields(skillset: dict) -> List[tuple]:
    """Returns a list of (label, value) pairs for every skill present in a
    unit's skillset, covering both the known slots and anything unexpected
    in the data so nothing silently gets left off the embed."""
    if not isinstance(skillset, dict):
        return []

    fields: List[tuple] = []
    consumed_keys = set()

    for base_key, break_key, label in KNOWN_SKILL_SLOTS:
        consumed_keys.add(base_key)
        consumed_keys.add(break_key)
        skill_text = skillset.get(base_key)
        if not skill_text:
            continue

        if base_key == "revelation" and isinstance(skill_text, dict):
            fields.append((label, format_revelation_skillset(skill_text)))
            continue

        value = format_skill(skill_text, to_int(skillset.get(break_key)))
        if base_key == "superequip":
            equip_name = skillset.get("superequipname")
            equip_type = skillset.get("superequiptype")
            consumed_keys.update({"superequipname", "superequiptype"})
            if equip_name or equip_type:
                extra = " / ".join(part for part in (clean_text(equip_name), type_label_with_emoji(equip_type)) if part and part != "-")
                if extra:
                    value = f"{value}\n*({extra})*"
        fields.append((label, value))

    # Anything left over (new/unlisted skill types) still gets shown, using
    # a best-effort label and BREAK lookup, instead of being dropped.
    for key, value in skillset.items():
        if key in consumed_keys or key.endswith("break") or not value:
            continue
        break_value = to_int(skillset.get(f"{key}break"))
        fields.append((humanize_skill_key(key), format_skill(value, break_value)))

    return fields

def format_passives(passive_data) -> str:
    if not isinstance(passive_data, dict):
        return "-"
    lines = [
        f"• {str(passive_value).strip()}"
        for passive_value in passive_data.values()
        if passive_value and str(passive_value).strip()
    ]
    return "\n".join(lines) if lines else "-"

def format_slots(slots) -> str:
    """
    Supports:
      slot1 + slot1type
      slot2 + slot2type
      slot3 + slot3type or split with slot31type / slot32type
    """
    if not isinstance(slots, dict):
        return "-"

    output: List[str] = []

    slot1_stars = to_int(slots.get("slot1"))
    slot1_type  = slots.get("slot1type") or slots.get("slottype1")
    if slot1_stars:
        output.append(f"{slot1_stars}★ {type_label_with_emoji(slot1_type)}")

    slot2_stars = to_int(slots.get("slot2"))
    slot2_type  = slots.get("slot2type") or slots.get("slottype2")
    if slot2_stars:
        output.append(f"{slot2_stars}★ {type_label_with_emoji(slot2_type)}")

    slot3_stars  = to_int(slots.get("slot3"))
    slot3_type_a = slots.get("slot31type") or slots.get("slot3_1type") or slots.get("slot3a")
    slot3_type_b = slots.get("slot32type") or slots.get("slot3_2type") or slots.get("slot3b")
    slot3_type   = slots.get("slot3type")  or slots.get("slottype3")

    if slot3_stars:
        slot3_labels: List[str] = []
        if slot3_type_a: slot3_labels.append(type_label_with_emoji(slot3_type_a))
        if slot3_type_b: slot3_labels.append(type_label_with_emoji(slot3_type_b))
        if not slot3_labels and slot3_type: slot3_labels.append(type_label_with_emoji(slot3_type))

        if len(slot3_labels) == 2:
            output.append(f"{slot3_stars}★ {slot3_labels[0]} / {slot3_labels[1]}")
        elif len(slot3_labels) == 1:
            output.append(f"{slot3_stars}★ {slot3_labels[0]}")
        else:
            output.append(f"{slot3_stars}★ Unknown")

    return "\n".join(output) if output else "-"

def resolve_image_url(relative_url: Optional[str]) -> Optional[str]:
    if not relative_url:
        return None
    relative_url = str(relative_url).strip()
    if not relative_url:
        return None
    if relative_url.startswith("http://") or relative_url.startswith("https://"):
        return relative_url
    return config.BASE_IMAGE_URL + relative_url if relative_url.startswith("/") else f"{config.BASE_IMAGE_URL}/{relative_url}"

def get_unit_icon(unit: dict) -> Optional[str]:
    """Picks the most-evolved available unit thumbnail."""
    image_data = unit.get("image") or {}
    # Ordered from most-evolved / most-recent art down to the base awakened art.
    thumb_priority = (
        "thumbspecial2", "thumbspecial", "thumbsuper",
        "thumb5", "thumb4", "thumb3", "thumb2", "thumb1", "thumbawk",
    )
    for thumb_key in thumb_priority:
        resolved_url = resolve_image_url(image_data.get(thumb_key))
        if resolved_url:
            return resolved_url
    return None

def get_unit_detail_image(unit: dict) -> Optional[str]:
    """Most-evolved unit DETAIL art (not the small thumb) - matches the size
    class of the True Weapon's image, since Discord shows images at native
    size rather than upscaling."""
    image_data = unit.get("image") or {}
    detail_priority = (
        "detailspecial2", "detailspecial", "detailsuper",
        "detail5", "detail4", "detail3", "detail2", "detail1", "detailawk",
    )
    for detail_key in detail_priority:
        resolved_url = resolve_image_url(image_data.get(detail_key))
        if resolved_url:
            return resolved_url
    return get_unit_icon(unit)  # fall back to the thumb if no detail art exists

def get_equip_icon(equip: dict) -> Optional[str]:
    """Picks the max-refined equip thumbnail, falling back to the base one."""
    image_data = equip.get("image") or {}
    for thumb_key in ("thumbmax", "thumb"):
        resolved_url = resolve_image_url(image_data.get(thumb_key))
        if resolved_url:
            return resolved_url
    return None

def get_equip_detail_image(equip: dict) -> Optional[str]:
    """Picks the max-refined equip DETAIL art (large), falling back to the base detail."""
    image_data = equip.get("image") or {}
    for detail_key in ("detailmax", "detail"):
        resolved_url = resolve_image_url(image_data.get(detail_key))
        if resolved_url:
            return resolved_url
    return None

def get_equip_break_value(skillset: dict) -> Optional[int]:
    """Equip data uses either 'break' or (inconsistently) 'skillbreak' for BREAK value."""
    if not isinstance(skillset, dict):
        return None
    if skillset.get("break") is not None:
        return to_int(skillset.get("break"))
    return to_int(skillset.get("skillbreak"))

# =========================
# DATA LOADING
# =========================

def _trim(records: List[dict]) -> List[dict]:
    """Drops the fields nothing renders, and interns the keys that remain.

    Both are about memory rather than tidiness. Each record is a dict of
    short strings repeated across thousands of records, so parsing leaves
    thousands of separate copies of the same handful of key strings;
    interning collapses them to one. See config.DROPPED_DATA_FIELDS for
    what goes and why it's safe."""
    dropped = getattr(config, "DROPPED_DATA_FIELDS", frozenset())
    for record in records:
        for field in dropped & record.keys():
            del record[field]
        for key in list(record):
            interned = sys.intern(key)
            if interned is not key:
                record[interned] = record.pop(key)
    return records

def load_units() -> None:
    global UNITS

    UNITS = _trim(load_js_data_with_cache(config.UNIT_DATA_FILE, config.UNIT_DATA_VARIABLE))

    UNITS_BY_NAME.clear()
    UNIT_NAMES.clear()
    UNIT_NAMES_LOWER.clear()

    for unit in UNITS:
        unit_name = (unit.get("name") or "").strip()
        if not unit_name:
            continue
        unit_key = unit_name.lower()
        UNITS_BY_NAME[unit_key] = unit
        UNIT_NAMES.append(unit_name)
        UNIT_NAMES_LOWER.append(unit_key)

    global DATA_VERSION
    DATA_VERSION += 1

def load_equips() -> None:
    global EQUIPS

    EQUIPS = _trim(load_js_data_with_cache(config.EQUIP_DATA_FILE, config.EQUIP_DATA_VARIABLE))

    EQUIPS_BY_NAME.clear()
    EQUIPS_BY_TRANSLATE.clear()
    EQUIP_NAMES.clear()
    EQUIP_NAMES_LOWER.clear()

    for equip in EQUIPS:
        equip_name = (equip.get("name") or "").strip()
        if not equip_name:
            continue
        equip_key = equip_name.lower()
        EQUIPS_BY_NAME[equip_key] = equip  # duplicate names: last one wins
        EQUIP_NAMES.append(equip_name)
        EQUIP_NAMES_LOWER.append(equip_key)

        equip_translate = (equip.get("translate") or "").strip()
        if equip_translate:
            EQUIPS_BY_TRANSLATE[equip_translate.lower()] = equip

    global DATA_VERSION
    DATA_VERSION += 1

def find_unit(name: str) -> Optional[dict]:
    if not name:
        return None
    return UNITS_BY_NAME.get(name.strip().lower())

def find_equip(name: str) -> Optional[dict]:
    if not name:
        return None
    return EQUIPS_BY_NAME.get(name.strip().lower())

def find_equip_by_translate(name: str) -> Optional[dict]:
    """Looks up an equip by its English `translate` field."""
    if not name:
        return None
    return EQUIPS_BY_TRANSLATE.get(name.strip().lower())

def find_equip_any_name(name: str) -> Optional[dict]:
    """Tries the primary name first, then the `translate` field."""
    return find_equip(name) or find_equip_by_translate(name)

# =========================
# SHARED AUTOCOMPLETE
# =========================

def build_name_autocomplete(current: str, names: List[str], names_lower: List[str]) -> List[app_commands.Choice]:
    current_input = (current or "").strip().lower()

    if not current_input:
        picks = names[:25]
    else:
        starts:   List[str] = []
        contains: List[str] = []

        for original_name, lower_name in zip(names, names_lower):
            if lower_name.startswith(current_input):
                starts.append(original_name)
            elif current_input in lower_name:
                contains.append(original_name)
            if len(starts) >= 25:
                break  # can't need more than 25 total, and starts always wins over contains

        picks = starts + contains

    return [app_commands.Choice(name=choice_name, value=choice_name) for choice_name in picks[:25]]

# The word -> names index each fuzzy match needs, built once per roster
# rather than rebuilt on every call. Keyed by the list itself plus
# DATA_VERSION, so a reload (which refills the lists in place, leaving
# their identity unchanged) still invalidates it.
_word_index_cache: Dict[int, tuple] = {}

def _word_index(names: List[str]) -> tuple:
    cached = _word_index_cache.get(id(names))
    if cached and cached[0] == DATA_VERSION:
        return cached[1], cached[2]

    word_to_names: Dict[str, List[str]] = {}
    for name in names:
        for word in re.split(r"\s+", name.strip()):
            word_lower = word.lower()
            if len(word_lower) >= 3:
                word_to_names.setdefault(word_lower, []).append(name)
    vocabulary = list(word_to_names.keys())

    _word_index_cache[id(names)] = (DATA_VERSION, word_to_names, vocabulary)
    return word_to_names, vocabulary

def fuzzy_match_names(query_words: List[str], names: List[str], cutoff: float = 0.82, limit: int = 5) -> List[str]:
    """Typo-tolerant name matching for chat_reply.py's natural-language
    /unit and /equip lookups. Fuzzy-matches each query word against every
    individual word of every name - not the full name string, which
    fuzzy-matches poorly for a multi-word name like "Summer Juno" against
    a bare typed "juno" or a misspelled "jouno" - and returns the owning
    full names, most relevant first. Deliberately a fallback only: callers
    should try exact/substring matching first (see
    ask_data._find_matching_names(), the fast/precise path this
    complements) since it can't produce false-typo matches the way this
    can."""
    word_to_names, vocabulary = _word_index(names)

    matched_names: List[str] = []
    for query_word in query_words:
        for close_word in difflib.get_close_matches(query_word, vocabulary, n=limit, cutoff=cutoff):
            for name in word_to_names[close_word]:
                if name not in matched_names:
                    matched_names.append(name)
    return matched_names[:limit]
