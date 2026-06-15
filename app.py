import asyncio
import json
import logging
import os
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks
from keycloak import KeycloakAdmin

from config import GrantConfig, load_grant_config

# How often (minutes) to reconcile Discord roles against Keycloak group membership.
# Keycloak can't push membership-change events, so we poll to project its state onto Discord.
SYNC_INTERVAL_MINUTES = int(os.environ.get("GROUP_SYNC_INTERVAL_MINUTES", "5"))


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per log record. Audit fields passed via extra= are merged in."""

    _AUDIT_FIELDS = (
        "event", "trigger", "outcome",
        "actor", "target", "group", "role", "guild_id",
        "error",
    )

    def format(self, record: logging.LogRecord) -> str:
        entry: dict = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "msg": record.getMessage(),
        }
        for field in self._AUDIT_FIELDS:
            if hasattr(record, field):
                entry[field] = getattr(record, field)
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry)


handler = logging.StreamHandler()
handler.setFormatter(JsonFormatter())

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logger.addHandler(handler)


KeycloakClient = KeycloakAdmin(
    server_url=os.environ["KEYCLOAK_URL"],
    username=os.environ["KEYCLOAK_USERNAME"],
    password=os.environ["KEYCLOAK_PASSWORD"],
    realm_name=os.environ["KEYCLOAK_REALM"],
    user_realm_name=os.environ["KEYCLOAK_ADMIN_REALM"],
)


# We require the Members intent to receive updates to role membership
intents = discord.Intents.default()
intents.members = True

# A commands.Bot gives us a slash-command tree on top of the existing event handlers.
# The command_prefix is required but unused; all interaction is via slash commands.
DiscordClient = commands.Bot(command_prefix="!", intents=intents)

# Populated in on_ready once Keycloak is reachable; read by the slash commands.
GrantRules: GrantConfig | None = None

# Guards the one-time command tree sync so reconnects don't re-sync (and risk rate limits).
_commands_synced = False


def get_linked_groups(client: KeycloakAdmin = None) -> list:
    """
    Get all Keycloak groups that have the required attributes for linking to a Discord role
    :param client: A KeycloakAdmin instance configured for your realm
    :rtype: list
    :return: A list of groups with the required attributes
    """

    # Keycloak paginates the response on the Admin API endpoints
    # Therefore, we'll need to make sure we grab every group
    page_start = 0
    page_size = 100
    all_groups = []

    # Grab the first page of groups and add them to the list of groups
    # We're setting briefRepresentation to false, so it'll return the groups' attributes
    # These will be useful later on
    groups = client.get_groups(
        query={"briefRepresentation": "false", "first": page_start, "max": page_size}
    )
    all_groups += groups

    # Check if the size of the page matches what page size we asked for
    # If it does, request the next page and add them to the list of groups
    # Keep going until the page size doesn't match the requested page size
    while len(groups) == page_size:
        page_start += page_size
        groups = client.get_groups(
            query={
                "briefRepresentation": "false",
                "first": page_start,
                "max": page_size,
            }
        )
        all_groups += groups

    # Create a list of all groups with the required Keycloak attributes
    valid_groups = []

    for group in all_groups:
        try:
            if (
                group["attributes"]["discord-guild"]
                and group["attributes"]["discord-role"]
            ):
                valid_groups.append(group)
        except KeyError:
            # If the group doesn't have the required attributes, it'll throw a KeyError
            # We can just catch and kill the error :)
            pass

    return valid_groups


def get_linked_role(
    client: discord.client.Client = None, group: dict = None
) -> discord.Role | None:
    """
    Get the Discord role that is linked to a Keycloak group
    :param client: A Discord Client instance
    :param group: A dict containing a Keycloak group with attributes `discord-guild` and `discord-role`
    :rtype: discord.Role | None
    :return: The Discord role linked to the provided Keycloak group
    """

    guild_id = int(group["attributes"]["discord-guild"][0])
    role_id = int(group["attributes"]["discord-role"][0])

    guild = client.get_guild(guild_id)
    if guild is None:
        return None

    role = guild.get_role(role_id)
    if role is None:
        return None

    return role


def get_group_members(client: KeycloakAdmin = None, group_id: str = None) -> list:
    """
    Get the users that are in the Keycloak group
    :param client: A :class:`KeycloakAdmin` client
    :param group_id: A :class:`str` with the group's UUID in Keycloak
    :rtype: list
    :return: A :class:`list` containing all users in the group
    """

    # See comments in the get_linked_groups function for how we're handling Keycloak's Admin API pagination.
    # briefRepresentation=false makes the endpoint return each member's attributes
    # (incl. discord_id) inline, so we don't have to fetch every user individually.
    page_start = 0
    page_size = 100
    members = []

    def page(first):
        return client.get_group_members(
            group_id=group_id,
            query={"briefRepresentation": "false", "first": first, "max": page_size},
        )

    group_members = page(page_start)
    members += group_members

    while len(group_members) == page_size:
        page_start += page_size
        group_members = page(page_start)
        members += group_members

    return members


def get_discord_id(user: dict = None) -> int:
    """
    Read the Discord ID from a Keycloak user representation's `discord_id`
    attribute. The user dict must be a full representation (briefRepresentation
    false), as returned by get_group_members and get_user.
    :param user: A Keycloak user representation
    :rtype: int
    :return: The user's Discord ID
    """

    values = (user.get("attributes") or {}).get("discord_id")

    if not values:
        raise Exception("User has no discord_id attribute")

    return int(values[0])


def lookup_keycloak_user(discord_id: int) -> dict | None:
    """
    Find the Keycloak user whose `discord_id` attribute matches a Discord ID.

    The `q` search is pinned to an exact match: without it Keycloak does a
    substring search, so a short Discord ID could match the wrong account. Since
    this is the authorization anchor for the grant commands, an ambiguous result
    (more than one match) is treated as no match rather than trusting users[0].

    :param discord_id: The user's Discord ID
    :rtype: dict | None
    :return: The Keycloak user, or None if there's no unambiguous matching account
    """

    users = KeycloakClient.get_users(
        query={"q": "discord_id:%s" % discord_id, "exact": "true"}
    )
    if len(users) != 1:
        if len(users) > 1:
            logger.warning(
                "Ambiguous Keycloak lookup for discord_id %s: %d users matched",
                discord_id, len(users))
        return None
    return users[0]


def get_user_group_paths(user_id: str) -> set[str]:
    """
    Get the set of Keycloak group paths a user belongs to.
    :param user_id: The user's UUID in Keycloak
    :rtype: set[str]
    """

    groups = KeycloakClient.get_user_groups(user_id=user_id)
    return {group["path"] for group in groups}


def get_group_by_discord_role(role_id: int) -> dict | None:
    """
    Find the role-synced Keycloak group linked to a Discord role, if any.
    :param role_id: The Discord role's ID
    :rtype: dict | None
    :return: The linked Keycloak group, or None if the role isn't linked
    """

    groups = KeycloakClient.get_groups(
        query={"q": "discord-role:%s" % role_id, "exact": "true"}
    )
    return groups[0] if groups else None


def is_group_member(user_id: str, group_id: str) -> bool:
    """
    Check whether a Keycloak user is a member of a Keycloak group.
    :param user_id: The user's UUID in Keycloak
    :param group_id: The group's UUID in Keycloak
    :rtype: bool
    """

    groups = KeycloakClient.get_user_groups(user_id=user_id)
    return any(group["id"] == group_id for group in groups)


def get_group_discord_ids(group_id: str) -> set[int]:
    """
    Resolve the Discord IDs of every member of a Keycloak group.
    Members without a linked Discord identity are skipped.
    :param group_id: The group's UUID in Keycloak
    :rtype: set[int]
    """

    discord_ids = set()
    for member in get_group_members(client=KeycloakClient, group_id=group_id):
        try:
            discord_ids.add(get_discord_id(user=member))
        except Exception:
            # No linked Discord identity; nothing we can project onto Discord
            continue

    return discord_ids


async def reconcile_groups() -> list[dict]:
    """
    Project Keycloak group membership onto Discord roles.

    Keycloak is the system of record: for each linked group we add the Discord
    role to everyone who is in the group and remove it from anyone who holds the
    role but is not. All Keycloak reads run off-thread so we don't block the
    event loop; the Discord role edits run on the loop.

    :rtype: list[dict]
    :return: The linked Keycloak groups that were processed (for callers that
        need to know which groups are role-synced)
    """

    groups = await asyncio.to_thread(get_linked_groups, KeycloakClient)

    for group in groups:
        role = get_linked_role(client=DiscordClient, group=group)
        if not role:
            continue

        logger.info(
            "Reconciling Keycloak group %s onto Discord role %s",
            group["name"], role.name,
        )
        desired_ids = await asyncio.to_thread(get_group_discord_ids, group["id"])

        # Add the role to group members who don't have it yet
        for discord_id in desired_ids:
            member = role.guild.get_member(discord_id)
            if member is None or role in member.roles:
                continue
            try:
                await member.add_roles(
                    role, reason="In Keycloak group %s" % group["name"]
                )
                logger.info(
                    "Granted role %s to %s", role.name, member.name,
                    extra={
                        "event": "role_granted",
                        "trigger": "reconcile",
                        "outcome": "success",
                        "target": {"discord_id": member.id, "discord_name": member.name},
                        "role": {"id": role.id, "name": role.name},
                        "group": {"path": group.get("path"), "name": group["name"]},
                        "guild_id": role.guild.id,
                    },
                )
            except discord.HTTPException as e:
                logger.warning(
                    "Could not grant role %s to %s: %s", role.name, member.name, e,
                    extra={
                        "event": "role_granted",
                        "trigger": "reconcile",
                        "outcome": "failure",
                        "target": {"discord_id": member.id, "discord_name": member.name},
                        "role": {"id": role.id, "name": role.name},
                        "group": {"path": group.get("path"), "name": group["name"]},
                        "guild_id": role.guild.id,
                        "error": str(e),
                    },
                )

        # Remove the role from anyone who holds it but isn't in the group
        for member in list(role.members):
            if member.id in desired_ids:
                continue
            try:
                await member.remove_roles(
                    role, reason="Not in Keycloak group %s" % group["name"]
                )
                logger.info(
                    "Removed role %s from %s", role.name, member.name,
                    extra={
                        "event": "role_revoked",
                        "trigger": "reconcile",
                        "outcome": "success",
                        "target": {"discord_id": member.id, "discord_name": member.name},
                        "role": {"id": role.id, "name": role.name},
                        "group": {"path": group.get("path"), "name": group["name"]},
                        "guild_id": role.guild.id,
                    },
                )
            except discord.HTTPException as e:
                logger.warning(
                    "Could not remove role %s from %s: %s", role.name, member.name, e,
                    extra={
                        "event": "role_revoked",
                        "trigger": "reconcile",
                        "outcome": "failure",
                        "target": {"discord_id": member.id, "discord_name": member.name},
                        "role": {"id": role.id, "name": role.name},
                        "group": {"path": group.get("path"), "name": group["name"]},
                        "guild_id": role.guild.id,
                        "error": str(e),
                    },
                )

    return groups


@tasks.loop(minutes=SYNC_INTERVAL_MINUTES)
async def group_sync_loop():
    """Periodically re-project Keycloak membership, since Keycloak can't push events."""
    try:
        await reconcile_groups()
    except Exception:
        logger.exception("Scheduled group reconciliation failed")


@DiscordClient.event
async def on_ready():
    logger.info("Logged in as %s", DiscordClient.user)

    await reconcile_groups()

    # Load the grant rules, then register the slash commands. Both are guarded to
    # run only once, since on_ready can fire again on reconnects.
    global GrantRules, _commands_synced
    GrantRules = load_grant_config(
        path=os.environ.get("GRANTS_CONFIG"),
        client=KeycloakClient,
    )

    if not _commands_synced:
        await DiscordClient.tree.sync()
        _commands_synced = True
        logger.info("Synced application commands")

    if not group_sync_loop.is_running():
        group_sync_loop.start()


@DiscordClient.event
async def on_member_update(previous, current):
    """
    Enforce Keycloak as the system of record in real time: if someone's Discord
    roles are changed out of band, revert any *linked* role to match their
    Keycloak group membership. Non-linked roles (cosmetic, etc.) are ignored.
    """

    if current.id == DiscordClient.user.id:
        return

    changed_roles = set(previous.roles).symmetric_difference(current.roles)
    if not changed_roles:
        return

    keycloak_user = await asyncio.to_thread(lookup_keycloak_user, current.id)
    if keycloak_user is None:
        # No linked account means they can't be in any group; leave it to the
        # next reconcile rather than stripping roles off an unknown user here.
        return

    current_role_ids = {role.id for role in current.roles}

    for role in changed_roles:
        group = await asyncio.to_thread(get_group_by_discord_role, role.id)
        if group is None:
            continue  # not a role-synced group; not ours to manage

        in_group = await asyncio.to_thread(
            is_group_member, keycloak_user["id"], group["id"]
        )
        has_role = role.id in current_role_ids

        target_info = {
            "discord_id": current.id,
            "discord_name": current.name,
            "kc_id": keycloak_user["id"],
        }
        role_info = {"id": role.id, "name": role.name}
        group_info = {"path": group.get("path"), "name": group["name"]}

        if in_group and not has_role:
            try:
                await current.add_roles(
                    role, reason="In Keycloak group %s (restored)" % group["name"]
                )
                logger.warning(
                    "Restored role %s to %s — was removed out of band", role.name, current.name,
                    extra={
                        "event": "role_restored",
                        "trigger": "revert",
                        "outcome": "success",
                        "target": target_info,
                        "role": role_info,
                        "group": group_info,
                        "guild_id": current.guild.id,
                    },
                )
            except discord.HTTPException as e:
                logger.warning(
                    "Could not restore role %s to %s: %s", role.name, current.name, e,
                    extra={
                        "event": "role_restored",
                        "trigger": "revert",
                        "outcome": "failure",
                        "target": target_info,
                        "role": role_info,
                        "group": group_info,
                        "guild_id": current.guild.id,
                        "error": str(e),
                    },
                )
        elif has_role and not in_group:
            try:
                await current.remove_roles(
                    role, reason="Not in Keycloak group %s (reverted)" % group["name"]
                )
                logger.warning(
                    "Reverted role %s from %s — was added out of band", role.name, current.name,
                    extra={
                        "event": "role_reverted",
                        "trigger": "revert",
                        "outcome": "success",
                        "target": target_info,
                        "role": role_info,
                        "group": group_info,
                        "guild_id": current.guild.id,
                    },
                )
            except discord.HTTPException as e:
                logger.warning(
                    "Could not revert role %s from %s: %s", role.name, current.name, e,
                    extra={
                        "event": "role_reverted",
                        "trigger": "revert",
                        "outcome": "failure",
                        "target": target_info,
                        "role": role_info,
                        "group": group_info,
                        "guild_id": current.guild.id,
                        "error": str(e),
                    },
                )


async def group_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """
    Suggest grantable group paths. Authorization is enforced when the command
    runs, not here, to keep autocomplete responsive (no per-keystroke Keycloak
    lookups).
    """

    if GrantRules is None:
        return []

    current = current.lower()
    return [
        app_commands.Choice(name=group, value=group)
        for group in GrantRules.grantable_groups()
        if current in group.lower()
    ][:25]


def get_group(group_id: str) -> dict | None:
    """Fetch a single Keycloak group (full representation, incl. attributes)."""
    try:
        return KeycloakClient.get_group(group_id=group_id)
    except Exception:
        return None


async def apply_linked_role(group_id: str, user: discord.User, add: bool) -> str:
    """
    If the Keycloak group is role-synced, add/remove its linked Discord role on
    the user right away, so a grant/revoke is reflected immediately instead of
    waiting for the next reconcile poll. Best-effort: a grant-managed-only group,
    an unknown guild member, or a Discord error is a logged no-op, never raised.

    Returns a short note describing what happened to the Discord role (empty when
    the group has no linked role), so the command can append it to its reply.
    """

    group = await asyncio.to_thread(get_group, group_id)
    if not group:
        return ""

    attributes = group.get("attributes") or {}
    if "discord-role" not in attributes or "discord-guild" not in attributes:
        return ""  # grant-managed only; no Discord role to project

    role = get_linked_role(client=DiscordClient, group=group)
    if role is None:
        return " (its linked Discord role couldn't be found)"

    member = role.guild.get_member(user.id)
    if member is None:
        return f" (couldn't update the **{role.name}** role: user isn't in {role.guild.name})"

    event = "role_granted" if add else "role_revoked"
    verb = "Granted" if add else "Revoked"
    try:
        if add:
            await member.add_roles(role, reason="%s Keycloak group %s" % (verb, group["name"]))
        else:
            await member.remove_roles(role, reason="%s Keycloak group %s" % (verb, group["name"]))
        logger.info(
            "%s linked role %s for %s", verb, role.name, member.name,
            extra={
                "event": event,
                "trigger": "command",
                "outcome": "success",
                "target": {"discord_id": member.id, "discord_name": member.name},
                "role": {"id": role.id, "name": role.name},
                "group": {"path": group.get("path"), "name": group["name"]},
                "guild_id": role.guild.id,
            },
        )
        return f" and {'assigned' if add else 'removed'} the **{role.name}** Discord role"
    except discord.HTTPException as e:
        logger.warning(
            "Could not apply linked role %s for %s: %s", role.name, user, e,
            extra={
                "event": event,
                "trigger": "command",
                "outcome": "failure",
                "target": {"discord_id": user.id, "discord_name": str(user)},
                "role": {"id": role.id, "name": role.name},
                "group": {"path": group.get("path"), "name": group["name"]},
                "guild_id": role.guild.id,
                "error": str(e),
            },
        )
        action = "assign" if add else "remove"
        return f" (⚠️ couldn't {action} the **{role.name}** role: {e.text or e})"


@DiscordClient.tree.command(name="grant", description="Add a user to a Keycloak group")
@app_commands.describe(user="The user to add", group="The Keycloak group to grant")
@app_commands.autocomplete(group=group_autocomplete)
async def grant(interaction: discord.Interaction, user: discord.User, group: str):
    await interaction.response.defer(ephemeral=True)

    if GrantRules is None:
        await interaction.followup.send(
            "Grant rules aren't loaded yet, try again shortly."
        )
        return

    if group not in GrantRules.rules:
        await interaction.followup.send(f"`{group}` is not a grantable group.")
        return

    actor = await asyncio.to_thread(lookup_keycloak_user, interaction.user.id)
    if actor is None:
        await interaction.followup.send("You don't have a linked Keycloak account.")
        return

    actor_groups = await asyncio.to_thread(get_user_group_paths, actor["id"])
    if not GrantRules.can_grant(actor_groups, group):
        logger.warning(
            "%s attempted to grant %s but is not authorized",
            interaction.user.name, group,
            extra={
                "event": "authz_denied",
                "trigger": "command",
                "actor": {
                    "discord_id": interaction.user.id,
                    "discord_name": interaction.user.name,
                    "kc_id": actor["id"],
                    "kc_username": actor["username"],
                },
                "group": {"path": group},
            },
        )
        await interaction.followup.send(f"You're not authorized to grant `{group}`.")
        return

    target = await asyncio.to_thread(lookup_keycloak_user, user.id)
    if target is None:
        await interaction.followup.send(
            f"{user.mention} has no linked Keycloak account; they need to sign in via Discord SSO first."
        )
        return

    group_id = GrantRules.group_ids[group]
    await asyncio.to_thread(
        KeycloakClient.group_user_add, user_id=target["id"], group_id=group_id
    )
    role_note = await apply_linked_role(group_id, user, add=True)

    logger.info(
        "%s granted %s to %s", actor["username"], group, target["username"],
        extra={
            "event": "group_member_added",
            "trigger": "command",
            "outcome": "success",
            "actor": {
                "discord_id": interaction.user.id,
                "discord_name": interaction.user.name,
                "kc_id": actor["id"],
                "kc_username": actor["username"],
            },
            "target": {
                "discord_id": user.id,
                "discord_name": user.name,
                "kc_id": target["id"],
                "kc_username": target["username"],
            },
            "group": {"path": group, "kc_id": group_id},
        },
    )
    await interaction.followup.send(f"Added {user.mention} to `{group}`{role_note}.")


@DiscordClient.tree.command(
    name="revoke", description="Remove a user from a Keycloak group"
)
@app_commands.describe(user="The user to remove", group="The Keycloak group to revoke")
@app_commands.autocomplete(group=group_autocomplete)
async def revoke(interaction: discord.Interaction, user: discord.User, group: str):
    await interaction.response.defer(ephemeral=True)

    if GrantRules is None:
        await interaction.followup.send(
            "Grant rules aren't loaded yet, try again shortly."
        )
        return

    if group not in GrantRules.rules:
        await interaction.followup.send(f"`{group}` is not a grantable group.")
        return

    actor = await asyncio.to_thread(lookup_keycloak_user, interaction.user.id)
    if actor is None:
        await interaction.followup.send("You don't have a linked Keycloak account.")
        return

    actor_groups = await asyncio.to_thread(get_user_group_paths, actor["id"])
    if not GrantRules.can_grant(actor_groups, group):
        logger.warning(
            "%s attempted to revoke %s but is not authorized",
            interaction.user.name, group,
            extra={
                "event": "authz_denied",
                "trigger": "command",
                "actor": {
                    "discord_id": interaction.user.id,
                    "discord_name": interaction.user.name,
                    "kc_id": actor["id"],
                    "kc_username": actor["username"],
                },
                "group": {"path": group},
            },
        )
        await interaction.followup.send(f"You're not authorized to revoke `{group}`.")
        return

    target = await asyncio.to_thread(lookup_keycloak_user, user.id)
    if target is None:
        await interaction.followup.send(
            f"{user.mention} has no linked Keycloak account."
        )
        return

    group_id = GrantRules.group_ids[group]
    await asyncio.to_thread(
        KeycloakClient.group_user_remove, user_id=target["id"], group_id=group_id
    )
    role_note = await apply_linked_role(group_id, user, add=False)

    logger.info(
        "%s revoked %s from %s", actor["username"], group, target["username"],
        extra={
            "event": "group_member_removed",
            "trigger": "command",
            "outcome": "success",
            "actor": {
                "discord_id": interaction.user.id,
                "discord_name": interaction.user.name,
                "kc_id": actor["id"],
                "kc_username": actor["username"],
            },
            "target": {
                "discord_id": user.id,
                "discord_name": user.name,
                "kc_id": target["id"],
                "kc_username": target["username"],
            },
            "group": {"path": group, "kc_id": group_id},
        },
    )
    await interaction.followup.send(f"Removed {user.mention} from `{group}`{role_note}.")


@DiscordClient.tree.command(
    name="grants", description="List the groups you're allowed to grant"
)
async def grants(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    if GrantRules is None:
        await interaction.followup.send(
            "Grant rules aren't loaded yet, try again shortly."
        )
        return

    actor = await asyncio.to_thread(lookup_keycloak_user, interaction.user.id)
    if actor is None:
        await interaction.followup.send("You don't have a linked Keycloak account.")
        return

    actor_groups = await asyncio.to_thread(get_user_group_paths, actor["id"])
    allowed = [
        group
        for group in GrantRules.grantable_groups()
        if GrantRules.can_grant(actor_groups, group)
    ]

    if not allowed:
        await interaction.followup.send("You're not authorized to grant any groups.")
        return

    listing = "\n".join(f"- `{group}`" for group in allowed)
    await interaction.followup.send(f"You can grant:\n{listing}")


DiscordClient.run(
    token=os.environ["DISCORD_BOT_TOKEN"], log_handler=handler, log_formatter=JsonFormatter()
)
