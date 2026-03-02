# CCG ELO Bot

A Discord bot for ELO ranking of Yu-Gi-Oh! players and archetype tier lists. Members are ranked on leaderboards; matches record both player ELO and deck/archetype ELO for a meta tier list.

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

4. **Run**
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

## Example flow

1. `/leaderboard create Season 1`
2. `/leaderboard add Season 1` (each player adds themselves)
3. `/leaderboard match Season 1 @Alice @Bob Salamangreat Sky Striker`
4. `/leaderboard view Season 1` — member rankings
5. `/leaderboard tierlist` — archetype rankings

Deck names are normalized (case, spacing) so "Salamangreat" and "salamangreat" count as the same archetype.

Data is stored in `data/ccg_elo.db` (SQLite).
