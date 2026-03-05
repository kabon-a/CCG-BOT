# CCG ELO Bot

A Discord bot for Yu-Gi-Oh! format management: ELO ranking, archetype tier lists, courtroom-style polls with Shannon-based thresholds, and @active role tracking for proposal eligibility.

**Features:**
- **Leaderboards** — ELO ranking for members and archetype meta tier lists
- **Polls** — Courtroom-style polls with role-based eligibility, quorum (65%), and Shannon-derived winning threshold
- **@active role** — Automatically assigned to users with recent activity (messages, reactions, voting); removed after 7 days of inactivity

## Setup

1. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

2. **Create a Discord bot**
   - Go to [Discord Developer Portal](https://discord.com/developers/applications)
   - New Application → Bot → Reset Token and copy it
   - Enable **Message Content Intent** and **Server Members Intent** under Bot settings
   - Invite the bot with `applications.commands` and `bot` scopes

3. **Configure**
   - Copy `.env.example` to `.env`
   - Set `BOT_TOKEN=your_token_here`

4. **Permissions**
   - For @active role assignment: bot needs **Manage Roles** and its role must be above @active in the role hierarchy

5. **Run**
   ```bash
   python main.py
   ```

## Commands

### Leaderboard

| Command | Description |
|---------|-------------|
| `/leaderboard create <name>` | Create a new leaderboard |
| `/leaderboard list` | List all leaderboards |
| `/leaderboard add <leaderboard> [display_name]` | Add yourself to a leaderboard |
| `/leaderboard match <leaderboard> <winner> <loser> <winner_deck> <loser_deck>` | Record a match (updates member ELO + archetype tier list) |
| `/leaderboard customize <leaderboard> <display_name>` | Set your display name on a leaderboard |
| `/leaderboard view [leaderboard]` | View the member ELO leaderboard |
| `/leaderboard tierlist` | View the archetype/deck tier list (meta strength by win-rate) |
| `/leaderboard remove <leaderboard>` | Remove yourself from a leaderboard |
| `/leaderboard reset <name>` | Reset all ELOs on a leaderboard (Mod/Admin) |
| `/leaderboard reset_tierlist` | Reset archetype tier list (Mod/Admin) |
| `/leaderboard delete <name>` | Delete a leaderboard (Mod/Admin) |

### ELO Settings

| Command | Description |
|---------|-------------|
| `/leaderboard settings view <leaderboard>` | View current ELO settings |
| `/leaderboard settings default_rating <leaderboard> <value>` | Starting ELO |
| `/leaderboard settings k_factor <leaderboard> <value>` | ELO sensitivity |
| `/leaderboard settings precision <leaderboard> <value>` | Decimal places for display |
| `/leaderboard settings loss_dampen <leaderboard> <value>` | Reduce loser's ELO loss (0–1) |
| `/leaderboard settings max_advantage <leaderboard> <value>` | Cap max ELO change per game |
| `/leaderboard settings curve_factor <leaderboard> <value>` | Curve factor (400 = standard) |
| `/leaderboard settings influence_range <leaderboard> <value>` | Influence range |
| `/leaderboard settings ffa_distribution <leaderboard> <value>` | FFA distribution |

### Announce

| Command | Description |
|---------|-------------|
| `/announce <message> [channel]` | Send a message through the bot (Mod/Admin). Default: current channel. |

### Poll (Courtroom-style)

| Command | Description |
|---------|-------------|
| `/poll create <title> <options> <duration> [roles]` | Create a poll. Options are comma-separated; duration uses `1d`, `24h`, `60m` format; roles restrict who can vote (omit for everyone). |

Polls use reaction-based voting (users may vote on multiple options). When a poll closes, the bot posts a report with:
- No. of Active Eligible Voters (eligible roles + @active)
- Total Valid Voters and Valid Votes
- Shannon-based winning threshold (Pwin = 1.5 / (n_eff + 0.5))
- Pass/fail verdict (quorum 65%, winning % must meet threshold)

### @active Role

The bot assigns the `@active` role to users who interact with the server (messages, reactions, slash commands, voting). This role is removed after 7 days of inactivity. The bot creates the role if it does not exist. Ensure the bot's role is above @active so it can assign and remove it.

## Example flow

1. `/leaderboard create Season 1`
2. `/leaderboard add Season 1` (each player adds themselves)
3. `/leaderboard match Season 1 @Alice @Bob Salamangreat Sky Striker`
4. `/leaderboard view Season 1` — member rankings
5. `/leaderboard tierlist` — archetype rankings

Deck names are normalized (case, spacing) so "Salamangreat" and "salamangreat" count as the same archetype.

Data is stored in `data/ccg_elo.db` (SQLite).
