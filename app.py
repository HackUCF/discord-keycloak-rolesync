import logging
import os
from typing import List, Optional
from dotenv import load_dotenv
load_dotenv()


import discord
from keycloak import KeycloakAdmin


# ---------- Logging ----------
dt_fmt = "%Y-%m-%d %H:%M:%S"
formatter = logging.Formatter(
    "[{asctime}] [{levelname:<8}] {name}: {message}", dt_fmt, style="{"
)

handler = logging.StreamHandler()
handler.setFormatter(formatter)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logger.addHandler(handler)


# ---------- Keycloak & Discord clients ----------

KeycloakClient = KeycloakAdmin(
    server_url=os.environ["KEYCLOAK_URL"],
    username=os.environ["KEYCLOAK_USERNAME"],
    password=os.environ["KEYCLOAK_PASSWORD"],
    realm_name=os.environ["KEYCLOAK_REALM"],
    user_realm_name=os.environ["KEYCLOAK_ADMIN_REALM"],
)

intents = discord.Intents.default()
intents.members = True  # we need to see guild members to add/remove roles
DiscordClient = discord.Client(intents=intents)


# ---------- Helper functions ----------
def get_linked_groups(client: KeycloakAdmin) -> List[dict]:
    """
    Return all Keycloak groups that have the `discord-guild` and `discord-role` attributes set.
    """
    page_start = 0
    page_size = 100
    all_groups: List[dict] = []

    groups = client.get_groups(
        query={"briefRepresentation": "false", "first": page_start, "max": page_size}
    )
    all_groups += groups

    while len(groups) == page_size:
        page_start += page_size
        groups = client.get_groups(
            query={"briefRepresentation": "false", "first": page_start, "max": page_size}
        )
        all_groups += groups

    valid_groups: List[dict] = []
    for group in all_groups:
        try:
            if group["attributes"]["discord-guild"] and group["attributes"]["discord-role"]:
                valid_groups.append(group)
        except KeyError:
            # group is missing one of the attributes, ignore
            pass

    return valid_groups


def get_linked_role(client: discord.Client, group: dict) -> Optional[discord.Role]:
    """
    Given a Keycloak group that has discord-guild and discord-role attributes,
    return the corresponding Discord Role object, or None if it can't be found.
    """
    guild_id = int(group["attributes"]["discord-guild"][0])
    role_id = int(group["attributes"]["discord-role"][0])

    guild = client.get_guild(guild_id)
    if guild is None:
        logger.warning("Guild %s not found for group %s", guild_id, group["name"])
        return None

    role = guild.get_role(role_id)
    if role is None:
        logger.warning(
            "Role %s not found in guild %s for group %s",
            role_id,
            guild_id,
            group["name"],
        )
        return None

    return role


def get_group_members(client: KeycloakAdmin, group_id: str) -> List[dict]:
    """
    Return all Keycloak users that are members of the given group.
    """
    page_start = 0
    page_size = 100
    members: List[dict] = []

    group_members = client.get_group_members(
        group_id=group_id, query={"first": page_start, "max": page_size}
    )
    members += group_members

    while len(group_members) == page_size:
        page_start += page_size
        group_members = client.get_group_members(
            group_id=group_id, query={"first": page_start, "max": page_size}
        )
        members += group_members

    return members


def get_discord_id_from_attributes(user: dict) -> Optional[int]:
    """
    Read the `discord_id` user attribute from a Keycloak user representation.

    The assignment requires using a discord_id attribute on Keycloak users rather than
    a federated identity provider.
    """
    attrs = user.get("attributes") or {}
    values = attrs.get("discord_id")
    if not values:
        return None

    try:
        return int(values[0])
    except (ValueError, TypeError):
        return None


# ---------- Discord events ----------
@DiscordClient.event
async def on_ready():
    logger.info("Logged in to Discord as %s", DiscordClient.user)

    # Fetch all Keycloak groups that are linked to Discord roles
    groups = get_linked_groups(client=KeycloakClient)

    for group in groups:
        role = get_linked_role(client=DiscordClient, group=group)
        if role is None:
            continue

        logger.info(
            "Syncing Discord role '%s' with Keycloak group '%s'",
            role.name,
            group["name"],
        )

        # 1. Build the list of Discord IDs that *should* have this role (from Keycloak)
        keycloak_members = get_group_members(client=KeycloakClient, group_id=group["id"])
        desired_discord_ids = set()

        for kc_user in keycloak_members:
            discord_id = get_discord_id_from_attributes(kc_user)
            if discord_id is None:
                continue
            desired_discord_ids.add(discord_id)

        guild = role.guild

        # 2. Add the role to users who are in the Keycloak group but don't yet have the Discord role
        current_role_member_ids = {member.id for member in role.members}

        for discord_id in desired_discord_ids:
            if discord_id in current_role_member_ids:
                continue

            member = guild.get_member(discord_id)
            if member is None:
                logger.warning(
                    "Discord user %s (from Keycloak group %s) not found in guild %s",
                    discord_id,
                    group["name"],
                    guild.id,
                )
                continue

            logger.info(
                "Adding Discord role '%s' to member %s (%s) based on Keycloak group '%s'",
                role.name,
                member.display_name,
                member.id,
                group["name"],
            )
            await member.add_roles(role, reason="Synced from Keycloak group membership")

        # 3. Remove the role from users who currently have it but are *not* in the Keycloak group
        for member in list(role.members):
            if member.id not in desired_discord_ids:
                logger.info(
                    "Removing Discord role '%s' from member %s (%s) "
                    "because they are not in Keycloak group '%s'",
                    role.name,
                    member.display_name,
                    member.id,
                    group["name"],
                )
                await member.remove_roles(role, reason="Synced from Keycloak group membership")

    logger.info("Initial sync from Keycloak to Discord complete.")


# We don't need on_member_update for this assignment; we are doing a one-shot sync
DiscordClient.run(
    token=os.environ["DISCORD_BOT_TOKEN"],
    log_handler=handler,
    log_formatter=formatter,
)
