# CCG ELO Bot

A Discord bot for ELO ranking of Yu-Gi-Oh! card names. Users add card names to leaderboards, report match results, and customize display names.

## Setup

1. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

2. **Create a Discord bot**
   - Go to [Discord Developer Portal](https://discord.com/developers/applications)
   - New Application → Bot → Reset Token and copy it
   - Enable **Message Content Intent** under Bot settings
   - Invite the bot with `applications.commands` and `bot` scopes

3. **Configure**
   - Copy `.env.example` to `.env`
   - Set `BOT_TOKEN=your_token_here`

4. **Run**
   ```bash
   python main.py
   ```

## Commands

| Command | Description |
|---------|-------------|
| `/leaderboard create <name>` | Create a new leaderboard |
| `/leaderboard list` | List all leaderboards in the server |
| `/leaderboard add <leaderboard> <card_name> [display_name]` | Add a Yu-Gi-Oh! card to a leaderboard |
| `/leaderboard match <leaderboard> <winner> <loser>` | Record a match result (updates ELO) |
| `/leaderboard customize <leaderboard> <card_name> <display_name>` | Change the display name of a card on the leaderboard |
| `/leaderboard view [leaderboard]` | View the ELO leaderboard |
| `/leaderboard remove <leaderboard> <card_name>` | Remove a card from a leaderboard |
| `/leaderboard delete <name>` | Delete an entire leaderboard |

## Example flow

1. `/leaderboard create Best Monsters`
2. `/leaderboard add Best Monsters "Dark Magician"`
3. `/leaderboard add Best Monsters "Blue-Eyes White Dragon"`
4. `/leaderboard match Best Monsters "Dark Magician" "Blue-Eyes White Dragon"`
5. `/leaderboard customize Best Monsters "Dark Magician" "DM 🎩"`
6. `/leaderboard view Best Monsters`

Data is stored in `data/ccg_elo.db` (SQLite).
