# embeds.py
# Discord embed builders for Grand Summoners units and equipment.
#
# build_unit_embeds(unit) -> List[discord.Embed]   (art + info + True Weapons)
# build_equip_embed(equip) -> discord.Embed        (single info card)
#
# Unit/equip dicts come from core.game_data (see load_units / load_equips),
# which reads the data files update_game_data.py downloads from
# https://www.grandsummoners.info

import re
from typing import List

import discord

from .game_data import (
    clean_text, to_int, slot_type_label, type_label_with_emoji,
    format_skill, format_passives, format_slots, build_skill_fields,
    resolve_image_url, get_unit_detail_image, get_equip_icon,
    get_equip_detail_image, get_equip_break_value, find_equip_any_name,
)

# =========================
# Unit embeds
# =========================

def build_unit_embeds(unit: dict) -> List[discord.Embed]:
    """Leading image-only embed (unit art) + main info embed. The leading
    embed is only added when there's a separate True Weapon image to put at
    the bottom of the main embed - otherwise the unit art goes on the main
    embed directly, so there's never two stacked embeds showing one image."""
    embed = discord.Embed(
        title=clean_text(unit.get("name")),
        description=f"{clean_text(unit.get('attribute'))}, {clean_text(unit.get('type'))}",
        color=discord.Color.dark_theme(),
    )

    stats     = unit.get("stats") or {}
    hp_value  = stats.get("hp")
    atk_value = stats.get("atk")
    def_value = stats.get("def")

    stat_lines: List[str] = []
    if hp_value is not None:
        hp_plus = stats.get("hpplus")
        stat_lines.append(f"HP:   {hp_value}  (+{hp_plus})" if hp_plus is not None else f"HP:   {hp_value}")
    if atk_value is not None:
        atk_plus = stats.get("atkplus")
        stat_lines.append(f"ATK:  {atk_value}  (+{atk_plus})" if atk_plus is not None else f"ATK:  {atk_value}")
    if def_value is not None:
        def_plus = stats.get("defplus")
        stat_lines.append(f"DEF:  {def_value}  (+{def_plus})" if def_plus is not None else f"DEF:  {def_value}")

    embed.add_field(name="Stats", value="```" + ("\n".join(stat_lines) if stat_lines else "-") + "```", inline=False)
    embed.add_field(name="Slots", value=format_slots(unit.get("slots")), inline=False)

    skillset = unit.get("skillset") or {}
    for skill_label, skill_value in build_skill_fields(skillset):
        embed.add_field(name=skill_label, value=skill_value, inline=False)

    embed.add_field(name="Passives", value=format_passives(unit.get("passive")), inline=False)

    dream_data = unit.get("dream") or unit.get("dreamJP")
    if isinstance(dream_data, dict) and dream_data:
        embed.add_field(name="Dream Awakening Ability", value="\u200b", inline=False)
        for element_key, element_data in dream_data.items():
            if not isinstance(element_data, dict):
                continue
            element_label = element_key.strip().title()
            element_passive = format_passives(element_data.get("passive"))
            embed.add_field(name=element_label, value=element_passive, inline=False)

    true_weapon_raw = unit.get("trueweapon") or unit.get("trueweaponJP")
    true_weapon_dicts: List[dict] = []
    if isinstance(true_weapon_raw, dict):
        if true_weapon_raw.get("name"):
            true_weapon_dicts = [true_weapon_raw]
        else:
            numbered_keys = sorted(
                (k for k in true_weapon_raw if re.match(r"^true\d+$", k)),
                key=lambda k: int(k[4:]),
            )
            true_weapon_dicts = [true_weapon_raw[k] for k in numbered_keys if isinstance(true_weapon_raw[k], dict)]

    true_weapon_embeds: List[discord.Embed] = []
    for true_weapon in true_weapon_dicts:
        true_weapon_name    = clean_text(true_weapon.get("name"))
        true_weapon_skill   = format_skill(true_weapon.get("skill"), to_int(true_weapon.get("skillbreak")))
        true_weapon_passive = format_passives(true_weapon.get("passive"))

        matched_equip = find_equip_any_name(f'True "{true_weapon_name}"')

        header_details: List[str] = []
        weapon_image_url = None
        if matched_equip:
            star_value = to_int(matched_equip.get("star"))
            if star_value:
                header_details.append(f"{star_value}★")
            equip_type_plain = slot_type_label(matched_equip.get("type"))
            if equip_type_plain != "Unknown":
                header_details.append(type_label_with_emoji(matched_equip.get("type")))
            weapon_image_url = get_equip_detail_image(matched_equip)
        else:
            weapon_image_url = resolve_image_url(true_weapon.get("detail"))

        if weapon_image_url:
            weapon_image_embed = discord.Embed(color=discord.Color.dark_theme())
            weapon_image_embed.set_image(url=weapon_image_url)
            true_weapon_embeds.append(weapon_image_embed)

        star_type_line = " ".join(header_details) if header_details else "-"
        weapon_info_embed = discord.Embed(
            title=f'True "{true_weapon_name}"',
            description=f"{star_type_line}\n\n**Skill**\n{true_weapon_skill}\n\n**Passive**\n{true_weapon_passive}",
            color=discord.Color.dark_theme(),
        )
        true_weapon_embeds.append(weapon_info_embed)

    embeds: List[discord.Embed] = []
    unit_art_url = get_unit_detail_image(unit)

    if unit_art_url:
        leading = discord.Embed(color=discord.Color.dark_theme())
        leading.set_image(url=unit_art_url)
        embeds.append(leading)

    embeds.append(embed)
    embeds.extend(true_weapon_embeds)

    # Discord allows at most 10 embeds per message; drop the overflow rather
    # than letting a unit with many True Weapons fail the lookup.
    return embeds[:10]

# =========================
# Equipment embeds
# =========================

def build_equip_embed(equip: dict) -> discord.Embed:
    star_value = to_int(equip.get("star"))
    star_label = f"{star_value}★" if star_value else "-"

    embed = discord.Embed(
        title=clean_text(equip.get("name")),
        description=f"{star_label} {type_label_with_emoji(equip.get('type'))}",
        color=discord.Color.dark_theme(),
    )

    embed.add_field(name="Location", value=clean_text(equip.get("location")), inline=False)

    stats     = equip.get("stats") or {}
    hp_value  = stats.get("hp")
    atk_value = stats.get("atk")
    def_value = stats.get("def")

    stat_lines: List[str] = []
    if hp_value:
        stat_lines.append(f"HP:   {hp_value}")
    if atk_value:
        stat_lines.append(f"ATK:  {atk_value}")
    if def_value:
        stat_lines.append(f"DEF:  {def_value}")

    embed.add_field(
        name="Stats",
        value="```" + ("\n".join(stat_lines) if stat_lines else "-") + "```",
        inline=False,
    )

    skillset = equip.get("skillset") or {}
    embed.add_field(
        name="Skill",
        value=format_skill(skillset.get("skill"), get_equip_break_value(skillset)),
        inline=False,
    )

    embed.add_field(name="Passives", value=format_passives(equip.get("passive")), inline=False)

    lore_text = equip.get("lore")
    if lore_text and clean_text(lore_text) != "-":
        embed.add_field(name="Lore", value=clean_text(lore_text), inline=False)

    icon_url = get_equip_icon(equip)
    if icon_url:
        embed.set_thumbnail(url=icon_url)

    return embed
