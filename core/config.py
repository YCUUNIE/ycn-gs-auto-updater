# =========================
# DATA FILES
# =========================

UNIT_DATA_FILE = "unitInfo.js"           # created by update_game_data.py
UNIT_DATA_VARIABLE = "UnitInformation"   # the `const X = [...]` name inside UNIT_DATA_FILE
EQUIP_DATA_FILE = "equipInfo.js"         # created by update_game_data.py
EQUIP_DATA_VARIABLE = "EquipInformation" # the `const X = [...]` name inside EQUIP_DATA_FILE
BASE_IMAGE_URL = "https://www.grandsummoners.info"

# Damage/support type emoji shown next to types in embeds. These are custom
# Discord application emoji IDs tied to one server - replace the values with
# your own (Developer Portal > Your App > Emoji) or plain text like "PHY".
TYPE_EMOJI = {
    "Physical": "<:Phy:1456081469658370223>",
    "Magic":    "<:Magic:1524654206131245096>",
    "Support":  "<:Sup:1524654207238406154>",
    "Defense":  "<:Def:1456081470937501718>",
    "Heal":     "<:Heal:1456081473021939886>",
}

# Record fields the embed builders never render, filtered out before
# lookups to keep the in-memory data small.
DROPPED_DATA_FIELDS = frozenset({
    "evolution",    # evolution material icon paths
    "tags",         # numeric category ids
    "tier",         # numeric tier scores
    "slotsJP", "statsJP", "dreamGL", "lb7require",
    "id",           # internal game id, not shown anywhere
})
