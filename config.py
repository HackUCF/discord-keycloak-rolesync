import logging

import yaml
from keycloak import KeycloakAdmin
from keycloak.exceptions import KeycloakError


logger = logging.getLogger(__name__)


def _normalize_path(path: str) -> str:
    """Keycloak group paths are absolute and start with a slash."""
    path = path.strip()
    if not path.startswith("/"):
        path = "/" + path
    return path


class GrantConfig:
    """
    Holds the parsed and validated grant rules.

    :param rules: A mapping of grantable group path -> set of granter group paths
    :param group_ids: A mapping of grantable group path -> Keycloak group UUID
    """

    def __init__(self, rules: dict[str, set[str]], group_ids: dict[str, str]):
        self.rules = rules
        self.group_ids = group_ids

    def grantable_groups(self) -> list[str]:
        return sorted(self.rules.keys())

    def can_grant(self, actor_group_paths: set[str], target_group_path: str) -> bool:
        """A user may grant a group if they're a member of one of its granter groups."""
        granters = self.rules.get(target_group_path)
        if not granters:
            return False
        return bool(actor_group_paths & granters)


def load_grant_config(path: str = None, client: KeycloakAdmin = None) -> GrantConfig:
    """
    Load and validate the YAML grant config.

    Validation only logs warnings and skips bad entries; it never raises, so a
    malformed rule can't take the whole bot down. Each grantable group's UUID is
    resolved here so command handlers don't have to look it up on every call.

    A grantable group may also be role-synced (carry a discord-role attribute):
    since Keycloak is the system of record, granting writes group membership and
    the projection then applies the linked Discord role. The two compose.

    :param path: Path to the YAML config file (from the GRANTS_CONFIG env var)
    :param client: A :class:`KeycloakAdmin` client used to resolve group paths
    :rtype: GrantConfig
    """

    if not path:
        logger.warning("GRANTS_CONFIG is not set; no groups will be grantable")
        return GrantConfig(rules={}, group_ids={})

    try:
        with open(path) as f:
            raw = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as e:
        logger.warning("Could not load grant config from %s: %s", path, e)
        return GrantConfig(rules={}, group_ids={})

    rules: dict[str, set[str]] = {}
    group_ids: dict[str, str] = {}

    for entry in raw.get("grants", []):
        group = entry.get("group")
        granted_by = entry.get("granted_by") or []

        if not group or not granted_by:
            logger.warning(
                "Skipping grant entry missing 'group' or 'granted_by': %r", entry
            )
            continue

        group = _normalize_path(group)

        group_id = _resolve_group_id(client, group)
        if group_id is None:
            continue

        granter_paths = set()
        for granter in granted_by:
            granter = _normalize_path(granter)
            # Resolve granters too, purely to catch typos at startup
            if _resolve_group_id(client, granter) is None:
                continue
            granter_paths.add(granter)

        if not granter_paths:
            logger.warning("Skipping grant group %s: no valid granter groups", group)
            continue

        rules[group] = granter_paths
        group_ids[group] = group_id

    logger.info("Loaded %d grantable group(s) from %s", len(rules), path)
    return GrantConfig(rules=rules, group_ids=group_ids)


def _resolve_group_id(client: KeycloakAdmin, path: str) -> str | None:
    try:
        group = client.get_group_by_path(path)
    except KeycloakError as e:
        logger.warning("Could not resolve Keycloak group %s: %s", path, e)
        return None

    if not group or "id" not in group:
        logger.warning("Keycloak group %s not found", path)
        return None

    return group["id"]
