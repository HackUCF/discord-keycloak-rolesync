# Keycloak Groups to Discord Roles sync

This is a python application that projects Keycloak group membership onto Discord roles.

**Keycloak is the system of record.** Membership lives in Keycloak; the bot keeps Discord roles in
sync to reflect it. For each linked group it adds the Discord role to everyone in the group and
removes it from anyone who holds the role but isn't a member. Because Keycloak can't emit
membership-change events, this runs at startup and then on a periodic poll
(`GROUP_SYNC_INTERVAL_MINUTES`, default 5). Manually editing a *linked* Discord role is reverted to
match Keycloak — to change who has access, change it in Keycloak (admin UI or the grant commands
below).

For this to work you need to:
1. Implement the [Discord Keycloak Identity Provider](https://github.com/wadahiro/keycloak-discord)
2. Set up the Keycloak groups you'd like to project with the following attributes:
   - `discord-role` containing the ID of the role (requires dev mode for Discord to be enabled)
   - `discord-guild` containing the ID of the guild the role is in
3. Create a Keycloak user with Admin API access
   - If you have fine-grained authz enabled, provide the account with the  `view-users` & `manage-users` roles
4. Create a Discord Application ([here](https://discord.com/developers/applications)) & add the bot to your server.
   - Make sure it has the Server Members intent, otherwise it won't see role/member state
   - The bot needs the **Manage Roles** permission, and its highest role must sit *above* every
     role it manages, or it can't add/remove them
   - To use the grant commands (below), invite the bot with the `applications.commands` scope

## Example config

```yaml
      DISCORD_BOT_TOKEN: MZ1yGvKTjE0rY0cV8i47CjAa.uRHQPq.Xb1Mk2nEhe-4iUcrGOuegj57zMC
      KEYCLOAK_URL: https://keycloak.example.com
      KEYCLOAK_USERNAME: KeycloakUsername
      KEYCLOAK_PASSWORD: KeycloakPassword
      KEYCLOAK_REALM: Example-Corp
      # Only required if KeycloakUsername isn't an account under the Example-Corp realm
      KEYCLOAK_ADMIN_REALM: master
      # How often (minutes) to re-project Keycloak membership onto Discord roles (default 5)
      GROUP_SYNC_INTERVAL_MINUTES: 5
      # Path to the grant rules file (see below); omit to disable the grant commands
      GRANTS_CONFIG: /config/grants.yaml
```

## Granting groups via slash commands

On top of the role sync, trusted users can add others to Keycloak groups directly, across any
server the bot is in:

- `/grant <user> <group>` — add a user to a group
- `/revoke <user> <group>` — remove a user from a group
- `/grants` — list the groups you're allowed to grant

Authorization is by **Keycloak group membership**: a user may grant a group only if they belong to
one of its `granted_by` groups. Rules live in the YAML file pointed to by `GRANTS_CONFIG`:

```yaml
grants:
  - group: "/infra/infra-developers"   # the grantable group's Keycloak path
    granted_by:                         # Keycloak group paths whose members may grant it
      - "/execs"
      - "/infra/infra-director"
```

> [!IMPORTANT]
> A grant-managed group must **not** have the `discord-role`/`discord-guild` attributes. A group is
> either role-synced (projected onto a Discord role) or grant-managed (membership-only, written by
> the commands); never both. A group that is both would be claimed by the projection sync and
> excluded from the grant rules — so such entries are skipped with a warning at startup.

Both the grantable group and its granters are resolved against Keycloak at startup; typos or
non-existent paths are logged and skipped rather than crashing the bot. See `grants.yaml` for a
full sample.
