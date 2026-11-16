"""Shared YCCG Elo / decay / AA algorithms (mirrors Interspace src/server/ratings)."""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass


@dataclass
class EloSettings:
    default_rating: float = 1000.0
    k_factor: float = 16.0
    precision: int = 2
    loss_dampen: float = 1.0
    max_advantage: float = 0.0
    curve_factor: float = 10.0
    influence_range: float = 200.0
    cap_range: float = 0.0


CONSTITUTION_ELO = EloSettings()


@dataclass
class DecaySettings:
    epsilon: float = 10.0
    m: float = 1.6


PRODUCTION_DECAY = DecaySettings()


@dataclass
class AaEloSettings:
    k_arch: float = 20.0
    n0: float = 10.0
    influence_range: float = 400.0
    min_games_display: int = 5


DEFAULT_AA = AaEloSettings()


def normalize_deck_name(name: str) -> str:
    s = name.strip().lower()
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"[-_./]+", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def expected_score(rating_a: float, rating_b: float, settings: EloSettings = CONSTITUTION_ELO) -> float:
    base = max(settings.curve_factor, 1e-9)
    denom = max(settings.influence_range, 1e-9)
    return 1.0 / (1.0 + base ** ((rating_b - rating_a) / denom))


def elo_deltas(
    rating_a: float,
    rating_b: float,
    score_a: float,
    settings: EloSettings = CONSTITUTION_ELO,
) -> tuple[float, float]:
    """score_a: 1 win, 0.5 draw, 0 loss for player A."""
    if abs(score_a - 0.5) < 1e-12:
        expected_a = expected_score(rating_a, rating_b, settings)
        delta_a = settings.k_factor * (0.5 - expected_a)
        if settings.max_advantage > 0:
            delta_a = max(-settings.max_advantage, min(settings.max_advantage, delta_a))
        return delta_a, -delta_a

    winner_is_a = score_a >= 1.0
    winner = rating_a if winner_is_a else rating_b
    loser = rating_b if winner_is_a else rating_a
    diff = loser - winner
    cap_lim = settings.cap_range if settings.cap_range > 0 else 0.0
    if cap_lim > 0 and abs(diff) > cap_lim:
        diff = math.copysign(cap_lim, diff)
    base = max(settings.curve_factor, 1e-9)
    denom = max(settings.influence_range, 1e-9)
    expected_winner = 1.0 / (1.0 + base ** (diff / denom))
    winner_delta = settings.k_factor * (1.0 - expected_winner)
    if settings.max_advantage > 0:
        winner_delta = max(-settings.max_advantage, min(settings.max_advantage, winner_delta))
    loser_delta = -winner_delta * settings.loss_dampen
    return (winner_delta, loser_delta) if winner_is_a else (loser_delta, winner_delta)


def apply_rank_decay(
    players: list[tuple[str, float]],
    settings: DecaySettings = PRODUCTION_DECAY,
) -> dict[str, float]:
    """players: (id, elo). Returns id -> decayed elo."""
    if not players:
        return {}
    if len(players) == 1:
        return {players[0][0]: players[0][1]}

    sorted_players = sorted(players, key=lambda p: (-p[1], p[0]))
    n = len(sorted_players)
    groups: list[list[tuple[str, float]]] = []
    for player in sorted_players:
        if groups and abs(groups[-1][0][1] - player[1]) <= settings.epsilon:
            groups[-1].append(player)
        else:
            groups.append([player])

    next_map: dict[str, float] = {}
    groups_below = len(groups) - 1
    for group in groups:
        effective_rank = n - groups_below
        decay = settings.m * ((n - effective_rank) / (n - 1))
        for pid, elo in group:
            next_map[pid] = elo - decay
        groups_below -= 1
    return next_map


def aa_expected(r_a: float, r_b: float, influence: float = DEFAULT_AA.influence_range) -> float:
    d = max(float(influence), 1e-9)
    return 1.0 / (1.0 + 10 ** ((r_b - r_a) / d))


def aa_archetype_delta(
    *,
    true_winner: float,
    true_loser: float,
    arch_winner: float,
    arch_loser: float,
    pair_n_before: int,
    mirror: bool,
    settings: AaEloSettings = DEFAULT_AA,
) -> float:
    if mirror:
        return 0.0
    e_player = aa_expected(true_winner, true_loser, settings.influence_range)
    e_arch = aa_expected(arch_winner, arch_loser, settings.influence_range)
    alpha = 2.0 * abs(e_player - 0.5)
    e_combined = alpha * e_player + (1.0 - alpha) * e_arch
    denom = pair_n_before + settings.n0
    k_eff = settings.k_arch * (pair_n_before / denom) if denom > 0 else 0.0
    return k_eff * (1.0 - e_combined)


def aa_archetype_draw_deltas(
    *,
    true_a: float,
    true_b: float,
    arch_a: float,
    arch_b: float,
    pair_n_before: int,
    mirror: bool,
    settings: AaEloSettings = DEFAULT_AA,
) -> tuple[float, float]:
    if mirror:
        return 0.0, 0.0
    e_player_a = aa_expected(true_a, true_b, settings.influence_range)
    e_arch_a = aa_expected(arch_a, arch_b, settings.influence_range)
    alpha = 2.0 * abs(e_player_a - 0.5)
    e_combined_a = alpha * e_player_a + (1.0 - alpha) * e_arch_a
    denom = pair_n_before + settings.n0
    k_eff = settings.k_arch * (pair_n_before / denom) if denom > 0 else 0.0
    delta_a = k_eff * (0.5 - e_combined_a)
    return delta_a, -delta_a
