# Security model & access-control flows

This document describes the trust model of the Discord ↔ Keycloak role-sync bot: who can change
what, what every add/remove path actually does, and how unauthorized actors are handled. It is a
*description of the current behavior*, not a vulnerability-reporting policy.

## 1. The one rule everything follows: Keycloak is the system of record

Group membership lives in **Keycloak**. Discord roles are a **projection** of that membership — a
read-only mirror, never a source of truth. Every flow below is just a consequence of this:

- To give someone access, they must end up **in the Keycloak group**.
- Discord roles are recomputed to match Keycloak. A Discord role on its own grants nothing and is
  reverted if it doesn't match Keycloak.

A group may be **role-synced** (carries `discord-role` + `discord-guild` attributes → projected
onto a Discord role), **grant-managed** (listed in `grants.yaml`), **both**, or neither. Because
Keycloak is authoritative, "both" composes cleanly: a grant writes membership and the projection
applies the role.

## 2. Identities and components

| Component | Identity | Privilege |
|-----------|----------|-----------|
| **Bot → Keycloak** | a Keycloak service account (`KEYCLOAK_USERNAME`) | `realm-management` roles `view-users` + `manage-users`. **Realm-wide**: it can add/remove *any* user to/from *any* group. |
| **Bot → Discord** | the Discord bot user | `Manage Roles`; can only edit roles **below** its highest role in the hierarchy. |
| **Actor** (person running a command) | their Discord user ID → matched to a Keycloak user via the **`discord_id`** custom attribute (`q=discord_id:<id>`) | None inherently; authorization is computed per-command (§4). |
| **Policy** | `grants.yaml` (`GRANTS_CONFIG`) | Declares, per grantable group, which Keycloak groups' members may grant it. |

> **Critical:** the bot's Keycloak account is privileged realm-wide. The `grants.yaml` restrictions
> are enforced **in the bot's application code only** — they are *not* Keycloak fine-grained
> permissions. Keycloak itself would happily let this account modify any group. The grant rules are
> a guardrail inside the bot, not a Keycloak-enforced boundary.

## 3. Authorization model for the slash commands

A user may `/grant` or `/revoke` a group **iff**:

1. Their Discord ID resolves to a Keycloak user (they have a `discord_id` attribute set), **and**
2. That Keycloak user is a member of at least one of the group's `granted_by` groups in
   `grants.yaml`.

Authorization is **by Keycloak group membership, not Discord roles** — so it is consistent across
every server the bot is in. "Rotating" roles (e.g. a yearly "Infra Director") are modeled as a
Keycloak group with one member, so authority moves by changing group membership, never code.

## 4. Add / remove flows

### A. Add via the bot — `/grant @user /group`
1. Bot resolves the **actor's** `discord_id` → Keycloak user. No match → *"You don't have a linked
   Keycloak account."* (stop, no change).
2. Bot checks the actor's Keycloak groups against the group's `granted_by`. Not authorized →
   *"You're not authorized to grant `/group`."* (stop, no change).
3. Bot resolves the **target's** `discord_id` → Keycloak user. No match → *"…has no linked Keycloak
   account…"* (stop, no change).
4. Bot calls `group_user_add` → target is now in the Keycloak group. **This is the actual grant.**
5. If the group is role-synced, the bot immediately adds the linked Discord role to the target
   (best-effort) and the reply states whether the role was applied. The periodic reconcile will
   also keep it correct.

### B. Remove via the bot — `/revoke @user /group`
Same authorization as A. On success calls `group_user_remove` (the actual revoke) and, for a
role-synced group, immediately removes the linked Discord role. The reply notes the role outcome.

### C. Add directly in Keycloak (admin UI / Admin API / another tool)
This is fully authoritative — it bypasses `grants.yaml` entirely (those rules only gate the bot's
commands). The user is in the group immediately. For a role-synced group, the linked Discord role
is applied at the **next reconcile poll** (`GROUP_SYNC_INTERVAL_MINUTES`, default 5), not instantly.

### D. Remove directly in Keycloak
Authoritative and immediate in Keycloak; the linked Discord role is removed at the next reconcile
poll. (Also corrected instantly if the user's Discord roles happen to change — see F.)

### E. Manually adding a Discord role (out of band, e.g. a server admin in Discord)
Grants **nothing**. `on_member_update` fires: if the role is a *linked* role and the user is **not**
in the corresponding Keycloak group, the bot **removes the role** to match Keycloak. Non-linked
(cosmetic) roles are ignored. So you cannot escalate access by handing out a Discord role.

### F. Manually removing a Discord role (out of band)
If the user **is** still in the Keycloak group, `on_member_update` **restores** the role. Keycloak
membership wins.

> Reconciliation timing: Discord-side changes are corrected in real time via `on_member_update`;
> Keycloak-side changes (C, D) are corrected on the periodic poll, so allow up to one interval.

## 5. How unauthorized actors are handled

| Actor / action | Outcome |
|----------------|---------|
| Discord user with **no `discord_id`** runs any command | Rejected: "no linked Keycloak account." Cannot act, and cannot be a grant **target**. |
| Linked user **not in any granter group** runs `/grant`/`/revoke` | Rejected: "not authorized." No change. |
| Any user runs `/grants` | Lists only groups *they* are authorized to grant (membership-checked). |
| Autocomplete on the `group` field | Suggests **all** grantable group names to everyone (authorization is enforced at execution, not per-keystroke). This is minor **information disclosure**: unauthorized users can *see* grantable group paths, but cannot grant them. |
| Server admin assigns a linked Discord role manually | Reverted by the bot (flow E). No access gained. |
| Someone with **direct Keycloak access** changes membership | **Fully effective and bypasses `grants.yaml`.** Direct Keycloak access is, by design, above the bot. Treat Keycloak admin/group-management rights as the real privilege boundary. |
| The **bot** itself (bug or compromise) | Can modify any group in the realm (its account is realm-wide). `grants.yaml` is only a guardrail in code, not enforced by Keycloak. |

## 6. Trust boundaries to protect

1. **`discord_id` must be administrator-/IdP-managed, never user-editable.** Authorization keys off
   it: if a user could set their own `discord_id`, they could impersonate another Discord user or
   claim a granter's identity. Ensure the realm's user-profile permissions do not let users edit
   this attribute themselves.
2. **`grants.yaml` is the policy.** Whoever can edit or deploy it controls who can grant what.
   Review changes to it like access-control changes.
3. **Keycloak admin / group-management rights** are the top of the hierarchy — they bypass the bot's
   rules. Scope them tightly.
4. **The bot's Keycloak service account** should have *only* `view-users` + `manage-users` on the
   target realm — nothing broader (no `manage-realm`, `manage-clients`).
5. **Discord role hierarchy:** the bot can only manage roles **below** its highest role, so it can
   never grant a role more privileged than itself. Keep privileged Discord roles above the bot.
6. **Secrets** (`DISCORD_BOT_TOKEN`, Keycloak service-account password) live in `.env`
   (git-ignored). Compromise of either is equivalent to compromise of the bot in §5.

## 7. Cross-server behavior

The bot acts across **every guild it is in**. Authorization is global (by Keycloak group), not
per-server: a user authorized to grant `/group` can do so from any server, affecting the Discord
role wherever that group is projected (its `discord-guild`). There is no per-server scoping of grant
authority — model that into group design if you need it.

## 8. Failure modes (fail safe, not open)

- Target/actor has no linked account → command refuses; **no** partial change.
- Bad/typo'd group paths in `grants.yaml` → logged and skipped at startup; the bot stays up and
  those groups are simply not grantable.
- Discord role apply fails (e.g. role above the bot in the hierarchy → `403 Missing Permissions`) →
  the Keycloak membership change still stands (Keycloak is authoritative), the failure is logged,
  and the command reply surfaces the warning. The next reconcile retries.
- Keycloak unreachable at startup → reconcile/command-load error is caught and logged; the bot does
  not crash-loop.
