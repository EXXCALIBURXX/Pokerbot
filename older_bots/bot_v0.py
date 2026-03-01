from pkbot.base import BaseBot
from pkbot.actions import ActionCall, ActionCheck, ActionRaise, ActionFold, ActionBid
from pkbot.states import PokerState, GameInfo
from pkbot.runner import parse_args, run_bot
import eval7
import random

# ═══════════════════════════════════════════════════════════════════════════════
# PREFLOP LOOKUP TABLE
# Maps (hi_rank, lo_rank, suited) → equity vs random HU hand [0,1]
# Based on known HU equity values + formula interpolation.
# O(1) lookup at runtime.
# ═══════════════════════════════════════════════════════════════════════════════

def _build_preflop_table():
    table = {}
    for hi in range(2, 15):
        for lo in range(2, hi + 1):
            for suited in (True, False):
                if hi == lo:  # pocket pair
                    if hi == 14: s = 0.852
                    elif hi == 13: s = 0.823
                    elif hi == 12: s = 0.795
                    elif hi == 11: s = 0.773
                    elif hi == 10: s = 0.752
                    elif hi == 9:  s = 0.721
                    elif hi == 8:  s = 0.692
                    elif hi == 7:  s = 0.663
                    elif hi == 6:  s = 0.633
                    elif hi == 5:  s = 0.604
                    elif hi == 4:  s = 0.575
                    elif hi == 3:  s = 0.546
                    else:          s = 0.517
                else:
                    gap = hi - lo
                    s = 0.15 + 0.022 * hi + 0.010 * lo
                    if suited: s += 0.04
                    if gap == 1:   s += 0.04
                    elif gap == 2: s += 0.02
                    elif gap == 3: s += 0.005
                    elif gap >= 5: s -= 0.035
                    if hi == 14:
                        s += 0.05
                        if lo >= 13:   s += 0.04
                        elif lo >= 12: s += 0.02
                        elif lo >= 11: s += 0.01
                    elif hi == 13 and lo >= 12: s += 0.025
                table[(hi, lo, suited)] = max(0.0, min(1.0, s))

    # Known HU equity overrides (computed from equity calculators)
    overrides = {
        (14, 14, False): 0.852, (13, 13, False): 0.823,
        (12, 12, False): 0.795, (11, 11, False): 0.773,
        (10, 10, False): 0.752, (9, 9, False): 0.721,
        (14, 13, True):  0.667, (14, 13, False): 0.655,
        (14, 12, True):  0.640, (14, 12, False): 0.627,
        (14, 11, True):  0.627, (14, 11, False): 0.614,
        (14, 10, True):  0.615, (14, 10, False): 0.601,
        (13, 12, True):  0.598, (13, 12, False): 0.585,
        (13, 11, True):  0.583, (13, 11, False): 0.569,
        (12, 11, True):  0.574, (12, 11, False): 0.560,
        (11, 10, True):  0.568, (11, 10, False): 0.553,
        (10, 9, True):   0.561, (10, 9, False):  0.546,
        (9, 8, True):    0.554, (9, 8, False):   0.539,
        (8, 7, True):    0.546, (8, 7, False):   0.531,
        (7, 6, True):    0.537, (7, 6, False):   0.522,
        (14, 2, True):   0.584, (14, 2, False):  0.570,
    }
    for k, v in overrides.items():
        # Normalize key: always (hi, lo, suited) where hi >= lo
        hi, lo, suited = k
        table[(hi, lo, suited)] = v
        # Pairs are keyed as (hi, hi, False) by convention
    return table

PREFLOP_TABLE = _build_preflop_table()


def preflop_strength(c1, c2):
    r1 = c1.rank + 2
    r2 = c2.rank + 2
    hi, lo = max(r1, r2), min(r1, r2)
    suited = (c1.suit == c2.suit)
    return PREFLOP_TABLE.get((hi, lo, suited), 0.5)


# ═══════════════════════════════════════════════════════════════════════════════
# HAND CLASSIFICATION (Flop and beyond)
# Classifies our 2 hole cards + N board cards into one of 15 buckets.
# Returns (category_id, category_name, raw_equity_hint)
# Uses eval7's evaluate() for speed — no MC needed for classification.
#
# Categories (descending strength):
#  0  Monster     : Straight flush / quads / full house / flopped flush
#  1  Strong      : Set / two-pair top two / nut straight
#  2  GoodMade    : Overpair / TPTK / top two (non-top)
#  3  MedMade     : TP good kicker / mid pair / trips weak kicker
#  4  WeakMade    : TP weak kicker / bottom pair / underpair
#  5  ComboStrong : Pair + flush draw / pair + OESD (15+ outs)
#  6  ComboMed    : Pair + gutshot / flush draw + OESD (10-14 outs)
#  7  NutDraw     : Nut flush draw / OESD + overcards (~12 outs)
#  8  Draw        : OESD / flush draw / double gutshot (~8-9 outs)
#  9  WeakDraw    : Gutshot + overcards (~6 outs)
# 10  Gutshot     : Gutshot only (~4 outs)
# 11  AceHigh     : Ace high + backdoor
# 12  HighCard    : King/Queen high, marginal
# 13  Air         : Complete miss, no draw
# ═══════════════════════════════════════════════════════════════════════════════

# Equity hints per category — used as a FAST FALLBACK if MC budget is exhausted
# These are conservative estimates for HU play
CATEGORY_EQUITY = {
    0:  0.94,  # Monster
    1:  0.88,  # Strong
    2:  0.76,  # GoodMade
    3:  0.63,  # MedMade
    4:  0.51,  # WeakMade
    5:  0.66,  # ComboStrong
    6:  0.55,  # ComboMed
    7:  0.50,  # NutDraw
    8:  0.42,  # Draw
    9:  0.37,  # WeakDraw
    10: 0.30,  # Gutshot
    11: 0.33,  # AceHigh
    12: 0.26,  # HighCard
    13: 0.18,  # Air
}

CATEGORY_NAMES = {
    0: 'Monster', 1: 'Strong', 2: 'GoodMade', 3: 'MedMade', 4: 'WeakMade',
    5: 'ComboStrong', 6: 'ComboMed', 7: 'NutDraw', 8: 'Draw',
    9: 'WeakDraw', 10: 'Gutshot', 11: 'AceHigh', 12: 'HighCard', 13: 'Air',
}


def _count_flush_outs(hole, board):
    """Count flush draw outs (0, 9, or partial)."""
    suits = [c.suit for c in hole + board]
    for s in range(4):
        mine = sum(1 for c in hole if c.suit == s)
        total = suits.count(s)
        if total == 4 and mine >= 1:
            return 9  # flush draw
        if total == 3 and mine >= 1 and len(board) < 4:
            return 3  # backdoor
    return 0


def _count_straight_outs(hole, board):
    """Estimate straight draw outs: 8=OESD, 4=gutshot, 0=none."""
    ranks = sorted(set((c.rank + 2) for c in hole + board))
    # Check for OESD / gutshot in all 5-card windows
    best = 0
    for low in range(2, 11):
        window = set(range(low, low + 5))
        have = window & set(ranks)
        miss = len(window) - len(have)
        if miss == 1:
            best = max(best, 4)   # gutshot
        if miss == 0:
            best = max(best, 0)   # already made — handled elsewhere
    # OESD: exactly 4 consecutive ranks present
    for low in range(2, 11):
        seg = [r for r in ranks if low <= r <= low + 3]
        if len(seg) >= 4:
            consec = sorted(seg)
            for i in range(len(consec) - 3):
                if consec[i+3] - consec[i] == 3:
                    best = max(best, 8)  # OESD
    return best


def _has_ace(hole):
    return any(c.rank + 2 == 14 for c in hole)


def classify_hand(hole, board):
    """
    Classify current hand into a category.
    Uses eval7 hand ranks for made hand detection.
    Returns (category_id, category_name).
    """
    if len(board) < 3:
        return (13, 'Air')  # shouldn't happen postflop

    all5 = hole + board
    score = eval7.evaluate(all5)
    hand_type = score >> 24  # eval7 encodes hand type in top byte
    # eval7 hand type: 1=High Card, 2=Pair, 3=Two Pair, 4=Trips,
    #                  5=Straight, 6=Flush, 7=Full House, 8=Quads, 9=Str Flush

    flush_outs = _count_flush_outs(hole, board)
    straight_outs = _count_straight_outs(hole, board)

    # eval7 encoding: 0=HighCard 1=Pair 2=TwoPair 3=Trips 4=Straight 5=Flush 6=FH 7=Quads 8=SF
    if hand_type >= 6:
        return (0, 'Monster')   # Full House, Quads, Straight Flush

    if hand_type == 5:
        return (0, 'Monster')   # Flush

    if hand_type == 4:
        return (1, 'Strong')    # Straight

    if hand_type == 3:  # Trips
        # Set (both hole cards used for trips) vs board-paired trips
        board_ranks = [c.rank for c in board]
        hole_ranks = [c.rank for c in hole]
        # Check if it's a set (pocket pair hit board) or trips (one hole card)
        for hr in hole_ranks:
            if board_ranks.count(hr) >= 2:
                return (1, 'Strong')  # set
        return (3, 'MedMade')  # trips from board pair

    if hand_type == 2:  # Two Pair
        board_ranks = sorted([c.rank for c in board], reverse=True)
        hole_ranks = [c.rank for c in hole]
        # Check if both pairs involve hole cards (good two pair)
        pairs_from_hole = sum(1 for hr in hole_ranks if board_ranks.count(hr) >= 1)
        if pairs_from_hole == 2:
            # Top two pair
            top_board = max(board_ranks)
            if max(hole_ranks) == top_board:
                return (1, 'Strong')
            return (2, 'GoodMade')
        return (3, 'MedMade')  # one hole card contributes

    if hand_type == 1:  # One Pair
        board_ranks_sorted = sorted([c.rank for c in board], reverse=True)
        hole_ranks = sorted([c.rank for c in hole], reverse=True)
        board_ranks_set = set(board_ranks_sorted)

        # Overpair: pocket pair beats all board cards
        if hole_ranks[0] == hole_ranks[1]:
            if hole_ranks[0] > max(board_ranks_sorted):
                cat = 2 if hole_ranks[0] >= 10 else 4
                return (cat, 'GoodMade' if cat == 2 else 'WeakMade')
            else:
                # Underpair
                if flush_outs >= 9 or straight_outs >= 8:
                    return (5, 'ComboStrong')
                if flush_outs >= 9 or straight_outs >= 4:
                    return (6, 'ComboMed')
                return (4, 'WeakMade')

        # Find which hole card paired
        paired_rank = None
        for hr in hole_ranks:
            if hr in board_ranks_set:
                paired_rank = hr
                break

        if paired_rank is None:
            # Shouldn't happen; fallback
            return (13, 'Air')

        # Determine pair tier
        top_board = max(board_ranks_sorted)
        mid_board = sorted(board_ranks_sorted)[len(board_ranks_sorted) // 2]
        kicker = max(hr for hr in hole_ranks if hr != paired_rank)

        is_top_pair = (paired_rank == top_board)
        is_mid_pair = (paired_rank == mid_board and not is_top_pair)

        # Combo draws elevate category
        has_fd = flush_outs >= 9
        has_oesd = straight_outs >= 8
        has_gut = straight_outs >= 4

        if is_top_pair:
            # TPTK: kicker beats all non-paired board ranks
            other_board = [r for r in board_ranks_sorted if r != paired_rank]
            if kicker >= (max(other_board) if other_board else 0):
                if has_fd or has_oesd:
                    return (5, 'ComboStrong')
                return (2, 'GoodMade')
            elif kicker >= 10:  # good kicker
                if has_fd or has_oesd:
                    return (5, 'ComboStrong')
                return (2, 'GoodMade')
            elif kicker >= 7:  # medium kicker
                if has_fd or has_oesd:
                    return (6, 'ComboMed')
                return (3, 'MedMade')
            else:  # weak kicker
                if has_fd or has_oesd:
                    return (6, 'ComboMed')
                return (4, 'WeakMade')

        elif is_mid_pair:
            if has_fd and has_oesd:
                return (5, 'ComboStrong')
            if has_fd or has_oesd:
                return (6, 'ComboMed')
            if has_gut:
                return (6, 'ComboMed')
            return (4, 'WeakMade')

        else:  # bottom pair
            if has_fd or has_oesd:
                return (6, 'ComboMed')
            return (4, 'WeakMade')

    # ── No pair (High Card) — evaluate draws ─────────────────────────────────
    has_ace = _has_ace(hole)
    has_fd = flush_outs >= 9
    has_bd = flush_outs == 3
    has_oesd = straight_outs >= 8
    has_gut = straight_outs >= 4

    hole_ranks = sorted([c.rank + 2 for c in hole], reverse=True)
    board_max_rank = max(c.rank + 2 for c in board)

    # Overcards: both hole cards beat board max
    overcards = sum(1 for r in hole_ranks if r > board_max_rank)

    if has_fd and has_oesd:
        return (5, 'ComboStrong')  # Massive draw ~15 outs

    if has_fd and overcards >= 1:
        return (7, 'NutDraw')

    if has_fd:
        return (8, 'Draw')

    if has_oesd and overcards == 2:
        return (7, 'NutDraw')

    if has_oesd:
        return (8, 'Draw')

    if has_gut and overcards >= 1:
        return (9, 'WeakDraw')

    if has_gut:
        return (10, 'Gutshot')

    if has_ace:
        return (11, 'AceHigh')

    if hole_ranks[0] >= 13:  # King high or better
        return (12, 'HighCard')

    if has_bd:
        return (12, 'HighCard')

    return (13, 'Air')


# ═══════════════════════════════════════════════════════════════════════════════
# MONTE CARLO EQUITY
# Adaptive iters based on time budget + hand category.
# When opp card is known, uses weighted sampling (Bayesian over their range).
# ═══════════════════════════════════════════════════════════════════════════════

def calc_equity(hole_cards, board_cards, opp_revealed, iters):
    """
    Monte Carlo equity.
    If opp_revealed has 1 card, samples the other card weighted by
    how likely villain is to hold that combo (using preflop table).
    """
    known = set(hole_cards + board_cards + opp_revealed)
    deck  = [c for c in eval7.Deck().cards if c not in known]

    need_opp   = 2 - len(opp_revealed)
    need_board = 5 - len(board_cards)
    need_total = need_opp + need_board

    if need_total > len(deck):
        return 0.5

    # Build weights for opponent's unknown card (Bayesian range weighting)
    weights = None
    if len(opp_revealed) == 1 and need_opp == 1:
        rev = opp_revealed[0]
        rev_rank = rev.rank + 2
        w_list = []
        for c in deck:
            cr = c.rank + 2
            hi, lo = max(rev_rank, cr), min(rev_rank, cr)
            suited = (rev.suit == c.suit)
            w = PREFLOP_TABLE.get((hi, lo, suited), 0.3)
            w_list.append(w)
        total_w = sum(w_list)
        if total_w > 0:
            weights = [w / total_w for w in w_list]

    wins  = 0.0
    valid = 0

    for _ in range(iters):
        try:
            if weights and need_opp == 1:
                unknown = random.choices(deck, weights=weights, k=1)
                remaining = [c for c in deck if c not in unknown]
                board_sample = random.sample(remaining, need_board)
                sample = unknown + board_sample
            else:
                sample = random.sample(deck, need_total)
        except (ValueError, IndexError):
            break

        opp_hole    = opp_revealed + sample[:need_opp]
        final_board = board_cards  + sample[need_opp:]

        my_score  = eval7.evaluate(hole_cards + final_board)
        opp_score = eval7.evaluate(opp_hole   + final_board)
        valid += 1

        if my_score > opp_score:
            wins += 1.0
        elif my_score == opp_score:
            wins += 0.5

    return wins / valid if valid > 0 else 0.5


def estimate_info_value(hole, board, n_samples=15, iters_per=25):
    """
    Estimate the expected equity change from seeing ONE opponent card.
    Used to compute rational auction bid.
    Returns avg |equity_after - equity_before| ∈ [0, 0.5]
    """
    known = set(hole + board)
    deck  = [c for c in eval7.Deck().cards if c not in known]
    if len(deck) < 2:
        return 0.0

    base_eq = calc_equity(hole, board, [], iters_per)

    samples = min(n_samples, len(deck))
    total_delta = 0.0
    for card in random.sample(deck, samples):
        eq = calc_equity(hole, board, [card], iters_per)
        total_delta += abs(eq - base_eq)

    return total_delta / samples


# ═══════════════════════════════════════════════════════════════════════════════
# OPPONENT MODEL
# Tracks betting patterns, auction tendencies, and showdown data.
# Supports multiple strategy profiles for the opponent.
# ═══════════════════════════════════════════════════════════════════════════════

class OpponentModel:
    def __init__(self):
        self.hands_played = 0

        # Preflop stats
        self.vpip_count = 0
        self.pfr_count  = 0

        # Action counts by street [preflop, flop, turn, river]
        self.raises = [0, 0, 0, 0]
        self.calls  = [0, 0, 0, 0]
        self.checks = [0, 0, 0, 0]
        self.folds  = [0, 0, 0, 0]

        # Auction data
        self.bid_history   = []  # our best estimates of their bids
        self.auction_wins  = 0   # times they won (we lost)
        self.auction_count = 0

        # Showdown data: (opp_score, opp_total_wager)
        self.showdowns = []

        # Detected opponent strategy profile
        # Profiles: 'unknown', 'passive', 'aggressive', 'tight', 'calling_station', 'auction_maniac'
        self.profile = 'unknown'
        self._profile_update_freq = 50  # re-evaluate every N hands

    def record_action(self, action_type, street_idx):
        if   action_type == 'fold':  self.folds[street_idx]  += 1
        elif action_type == 'call':  self.calls[street_idx]  += 1
        elif action_type == 'check': self.checks[street_idx] += 1
        elif action_type == 'raise': self.raises[street_idx] += 1

    def record_bid_estimate(self, estimated_bid):
        self.bid_history.append(estimated_bid)
        if len(self.bid_history) > 400:
            self.bid_history = self.bid_history[-400:]

    def record_auction_result(self, opp_won):
        self.auction_count += 1
        if opp_won:
            self.auction_wins += 1

    def update_profile(self):
        """Re-classify opponent profile every N hands."""
        if self.hands_played < 30:
            return
        agg = self.aggression_factor
        ftr = self.fold_to_raise_rate
        vpip = self.vpip_count / max(1, self.hands_played)
        auc_rate = self.auction_wins / max(1, self.auction_count)

        if auc_rate > 0.65 and self.avg_bid > 300:
            self.profile = 'auction_maniac'   # overbids auctions
        elif vpip < 0.35 and agg < 0.25:
            self.profile = 'tight'
        elif vpip > 0.65 and ftr < 0.30:
            self.profile = 'calling_station'
        elif agg > 0.50:
            self.profile = 'aggressive'
        elif agg < 0.25:
            self.profile = 'passive'
        else:
            self.profile = 'balanced'

    @property
    def fold_to_raise_rate(self):
        total = sum(self.folds) + sum(self.calls) + sum(self.raises)
        return sum(self.folds) / total if total > 0 else 0.5

    @property
    def aggression_factor(self):
        r = sum(self.raises)
        c = sum(self.calls) + sum(self.checks)
        return r / (r + c) if (r + c) > 0 else 0.3

    @property
    def avg_bid(self):
        recent = self.bid_history[-60:]
        return sum(recent) / len(recent) if recent else 100.0

    @property
    def bid_std(self):
        recent = self.bid_history[-60:]
        if len(recent) < 2:
            return 50.0
        avg = sum(recent) / len(recent)
        return (sum((x - avg)**2 for x in recent) / len(recent)) ** 0.5

    @property
    def bid_percentile_75(self):
        """75th percentile of recent bids — useful for 'just above' bidding."""
        recent = sorted(self.bid_history[-60:])
        if not recent:
            return 150
        idx = int(len(recent) * 0.75)
        return recent[min(idx, len(recent) - 1)]


# ═══════════════════════════════════════════════════════════════════════════════
# BOT
# ═══════════════════════════════════════════════════════════════════════════════

class Bot(BaseBot):

    def __init__(self):
        self.opp_model    = OpponentModel()
        self.round_count  = 0

        # Per-hand state
        self._hand_hole         = []
        self._hand_board        = []
        self._hand_opp_revealed = []
        self._preflop_eq        = 0.5
        self._flop_category     = (13, 'Air')
        self._prev_opp_wager    = 0
        self._prev_street       = 'preflop'
        self._auction_won       = False
        self._auction_lost      = False
        self._chips_before_auction = 0
        self._chips_after_auction  = 0
        self._my_last_bid          = 0
        self._auction_detected     = False

        # Bankroll tracking
        self._recent_payoffs    = []

        # Strategy mode
        self._strategy_mode = 'balanced'

    # ── Time budget ──────────────────────────────────────────────────────────

    def _sim_iters(self, game_info, street, category_id=None):
        """
        Allocate MC iterations based on:
        1. Remaining time budget
        2. Street (preflop = 0, river = fewer needed)
        3. Hand category (monster/air = fewer, medium/draw = more)
        """
        tb = game_info.time_bank
        if street == 'preflop':
            return 0  # use lookup table

        # Base by time remaining
        if   tb > 17: base = 200
        elif tb > 14: base = 150
        elif tb > 10: base = 100
        elif tb > 6:  base = 70
        elif tb > 3:  base = 40
        elif tb > 1:  base = 20
        else:         base = 10

        # Reduce for monsters and air (category obvious, MC not needed)
        if category_id is not None:
            if category_id == 0:   base = min(base, 30)   # monster — just bet
            elif category_id == 13: base = min(base, 20)  # air — just fold/bluff
            elif category_id in (1, 2): base = min(base, 80)  # strong — less MC needed

        # River: no more cards coming, reduce slightly
        if street == 'river':
            base = int(base * 0.8)

        return max(10, base)

    # ── Equity ───────────────────────────────────────────────────────────────

    def _equity(self, game_info, current_state, override_iters=None, cat_id=None):
        hole  = [eval7.Card(s) for s in current_state.my_hand]
        board = [eval7.Card(s) for s in current_state.board]
        opp_r = [eval7.Card(s) for s in current_state.opp_revealed_cards]
        street = current_state.street

        if street == 'preflop' and not opp_r and len(hole) == 2:
            return preflop_strength(hole[0], hole[1])

        iters = override_iters if override_iters is not None else self._sim_iters(game_info, street, cat_id)

        # If we're very tight on time, fall back to category hint
        if iters < 10 and cat_id is not None:
            return CATEGORY_EQUITY[cat_id]

        return calc_equity(hole, board, opp_r, iters)

    # ── Hand classification ───────────────────────────────────────────────────

    def _classify(self, current_state):
        hole  = [eval7.Card(s) for s in current_state.my_hand]
        board = [eval7.Card(s) for s in current_state.board]
        if len(board) < 3:
            return (13, 'Air')
        return classify_hand(hole, board)

    # ── Bet sizing ───────────────────────────────────────────────────────────

    def _kelly_fraction(self, equity, pot_odds):
        edge = equity - pot_odds
        if edge <= 0: return 0.0
        return min(1.0, edge * 3.5)

    def _size_value_bet(self, equity, pot, min_r, max_r):
        """Size bet proportional to equity edge. Returns chip amount."""
        frac = self._kelly_fraction(equity, 0.0)
        # Scale from 0.4x pot (thin value) to 1.1x pot (nut)
        target = int(pot * (0.40 + frac * 0.70))
        return max(min_r, min(max_r, target))

    def _size_call_raise(self, equity, pot_odds, pot, min_r, max_r):
        """Raise size when facing a bet."""
        frac = self._kelly_fraction(equity, pot_odds)
        target = int(min_r + frac * (max_r - min_r))
        return max(min_r, min(max_r, target))

    # ── Bluff decision ────────────────────────────────────────────────────────

    def _should_bluff(self, current_state, cat_id):
        street = current_state.street
        if street == 'preflop': return False

        ftr = self.opp_model.fold_to_raise_rate
        profile = self.opp_model.profile

        # Never bluff into a calling station
        if profile == 'calling_station': return False

        base = 0.04
        if ftr > 0.55: base += 0.07
        if ftr > 0.70: base += 0.08

        # Bluff more with draws (semi-bluff), less with pure air
        if cat_id in (5, 6, 7, 8): base += 0.06  # semi-bluff with draws
        if cat_id == 13: base *= 0.5              # pure air — be conservative
        if street == 'turn': base += 0.02

        # Won auction → bluff more (opponent may fear we have info)
        if self._auction_won: base += 0.04

        return random.random() < base

    # ── Auction bid ───────────────────────────────────────────────────────────

    def _compute_bid(self, current_state, game_info, cat_id, equity):
        """
        Second-price auction: bid true information value.
        In a Vickrey auction, optimal strategy = bid your true valuation.
        We never overbid (wastes chips), never underbid (lose valuable info).
        """
        pot      = max(1, current_state.pot)
        my_chips = current_state.my_chips
        hole     = [eval7.Card(s) for s in current_state.my_hand]
        board    = [eval7.Card(s) for s in current_state.board]

        # ── Case 1: Monster or near-nut — info has zero value ────────────────
        if cat_id == 0 or equity > 0.88:
            return 0

        # ── Case 2: Air — no point; we're giving up anyway ───────────────────
        if cat_id == 13 and equity < 0.25:
            return random.randint(0, 5)  # tiny random bid for unpredictability

        # ── Compute information value ─────────────────────────────────────────
        # Time budget: use fast estimate (fewer samples)
        tb = game_info.time_bank
        n_samp  = 12 if tb > 8 else 8
        iters_p = 20 if tb > 8 else 12

        info_delta = estimate_info_value(hole, board, n_samp, iters_p)

        # Chips at risk on remaining streets (rough estimate)
        remaining_streets = 2 if current_state.street in ('auction', 'flop') else 1
        pot_remaining_est = pot * (1.5 * remaining_streets)

        true_value = int(info_delta * pot_remaining_est)

        # ── Adjust based on opponent profile ─────────────────────────────────
        opp_avg = self.opp_model.avg_bid
        profile = self.opp_model.profile

        if profile == 'auction_maniac':
            # They overbid — bid your true value and let them waste chips
            bid = true_value
        else:
            # Bid just above their likely bid IF info is worth it
            # In second-price: winning and paying their bid is good if true_value > their bid
            target = int(opp_avg * 1.05 + 5)  # just above their avg
            bid = min(true_value, target) if true_value > opp_avg * 0.8 else 0

        # ── Hard caps ────────────────────────────────────────────────────────
        # Never risk more than 8% of stack on info
        cap = int(my_chips * 0.08)
        # But floor: bid at least something if it's medium equity (force them to pay)
        if cat_id in (3, 4, 5, 6, 7) and bid == 0:
            bid = random.randint(1, 10)  # force them to overpay slightly

        bid = max(0, min(bid, cap, my_chips))

        # Slight randomization (±5%) to avoid reverse-engineering
        if bid > 5:
            bid = int(bid * random.uniform(0.95, 1.05))

        return bid

    # ── Strategy mode ────────────────────────────────────────────────────────

    def _update_strategy_mode(self):
        profile = self.opp_model.profile
        if profile == 'passive':
            self._strategy_mode = 'exploit_passive'
        elif profile in ('tight', 'balanced') and self.opp_model.fold_to_raise_rate > 0.55:
            self._strategy_mode = 'exploit_folder'
        elif profile == 'auction_maniac':
            self._strategy_mode = 'exploit_maniac'
        elif profile == 'calling_station':
            self._strategy_mode = 'valuebet_heavy'
        else:
            self._strategy_mode = 'balanced'

    def _get_thresholds(self, street, equity, cat_id):
        """
        Compute raise_threshold and call_threshold based on strategy mode.
        Returns (raise_thresh, call_thresh)
        """
        agg = self.opp_model.aggression_factor
        ftr = self.opp_model.fold_to_raise_rate
        mode = self._strategy_mode

        if mode == 'exploit_folder':
            # Widen raise range — they fold a lot
            raise_thresh = 0.58 - 0.08 * ftr
            call_thresh  = 0.35
        elif mode == 'exploit_passive':
            # Extract value — they don't raise back
            raise_thresh = 0.62
            call_thresh  = 0.38
        elif mode == 'exploit_maniac':
            # Tighten up — they overbid and over-aggress; let them spew
            raise_thresh = 0.72
            call_thresh  = 0.50
        elif mode == 'valuebet_heavy':
            # They call too much — bet thin value relentlessly
            raise_thresh = 0.60
            call_thresh  = 0.40
        else:
            # Balanced
            raise_thresh = 0.68 - 0.08 * ftr
            call_thresh  = 0.38 + 0.05 * agg

        # Tighten when facing aggression and we have weak category
        if agg > 0.45 and cat_id in (4, 9, 10, 11, 12, 13):
            raise_thresh += 0.05
            call_thresh  += 0.04

        # River: no more cards, be slightly tighter
        if street == 'river':
            raise_thresh += 0.03
            call_thresh  += 0.02

        return raise_thresh, call_thresh

    # ── Engine Callbacks ─────────────────────────────────────────────────────

    def on_hand_start(self, game_info: GameInfo, current_state: PokerState):
        self.round_count += 1
        self._hand_hole         = [eval7.Card(s) for s in current_state.my_hand]
        self._hand_board        = []
        self._hand_opp_revealed = []
        self._prev_opp_wager    = current_state.opp_wager
        self._prev_street       = 'preflop'
        self._auction_won       = False
        self._auction_lost      = False
        self._auction_detected  = False
        self._chips_before_auction = current_state.my_chips
        self._chips_after_auction  = 0
        self._my_last_bid          = 0
        self._flop_category     = (13, 'Air')

        if len(self._hand_hole) == 2:
            self._preflop_eq = preflop_strength(self._hand_hole[0], self._hand_hole[1])
        else:
            self._preflop_eq = 0.5

        # Update opponent profile periodically
        if self.round_count % self.opp_model._profile_update_freq == 0:
            self.opp_model.update_profile()
            self._update_strategy_mode()

    def on_hand_end(self, game_info: GameInfo, current_state: PokerState):
        payoff = current_state.payoff
        self._recent_payoffs.append(payoff)
        if len(self._recent_payoffs) > 200:
            self._recent_payoffs = self._recent_payoffs[-200:]

        self.opp_model.hands_played += 1

        # Detect fold: if hand ended before river showdown with unequal wagers
        if current_state.is_terminal and current_state.cost_to_call != 0:
            # Someone folded. If payoff > 0, opponent folded; if < 0, we folded.
            if payoff < 0:
                pass  # we folded — no info about opp
            else:
                # Opponent folded — record it on the street they folded
                fold_street = current_state.street
                fold_sidx = {'preflop': 0, 'flop': 1, 'auction': 1, 'turn': 2, 'river': 3}.get(fold_street, 0)
                self.opp_model.record_action('fold', fold_sidx)

        # Infer opponent bid from chip change around auction
        # Use _chips_after_auction (captured on first post-auction get_move call)
        if self._chips_after_auction > 0:
            auction_cost = self._chips_before_auction - self._chips_after_auction
            if self._auction_won:
                # We won — we paid opponent's bid (second-price)
                opp_bid = auction_cost
                self.opp_model.record_bid_estimate(opp_bid)
                self.opp_model.record_auction_result(opp_won=False)
            elif self._auction_lost:
                # We lost — we paid nothing (auction_cost should be 0)
                # Opponent bid > our bid, but we don't know exact amount
                # Estimate: at least our bid + small margin
                self.opp_model.record_bid_estimate(self._my_last_bid + random.randint(1, 30))
                self.opp_model.record_auction_result(opp_won=True)
            else:
                # Tied — both paid own bid, auction_cost = our bid
                self.opp_model.record_bid_estimate(self._my_last_bid)
                self.opp_model.record_auction_result(opp_won=False)

        # Showdown tracking
        if current_state.opp_revealed_cards:
            opp_cards = [eval7.Card(s) for s in current_state.opp_revealed_cards]
            board     = [eval7.Card(s) for s in current_state.board]
            if len(opp_cards) >= 2 and len(board) >= 3:
                opp_score = eval7.evaluate(opp_cards + board)
                self.opp_model.showdowns.append((opp_score, current_state.opp_wager))
                if len(self.opp_model.showdowns) > 150:
                    self.opp_model.showdowns = self.opp_model.showdowns[-150:]

    def get_move(self, game_info: GameInfo, current_state: PokerState):
        street = current_state.street
        sidx   = {'preflop': 0, 'flop': 1, 'auction': 1, 'turn': 2, 'river': 3}.get(street, 0)

        # ── Track opponent actions ───────────────────────────────────────────
        cur_opp_wager = current_state.opp_wager
        cur_cost = current_state.cost_to_call
        if street != 'auction' and street != self._prev_street:
            # Street changed — reset wager tracking.
            self._prev_opp_wager = 0
        if street != 'auction':
            if cur_opp_wager > self._prev_opp_wager:
                # Opponent raised or bet
                self.opp_model.record_action('raise', sidx)
            elif cur_opp_wager == self._prev_opp_wager and cur_cost == 0 and street == self._prev_street:
                # Same street, opponent wager unchanged, no cost to call = opponent checked
                if current_state.opp_wager > 0 or self._prev_opp_wager > 0:
                    self.opp_model.record_action('check', sidx)
        self._prev_opp_wager = cur_opp_wager
        self._prev_street = street

        # ── Classify hand (postflop only) ────────────────────────────────────
        if street not in ('preflop', 'auction'):
            self._flop_category = self._classify(current_state)
        cat_id, cat_name = self._flop_category

        # ── Auction ──────────────────────────────────────────────────────────
        if street == 'auction':
            self._flop_category = self._classify(current_state)
            cat_id, _ = self._flop_category
            self._chips_before_auction = current_state.my_chips
            eq  = self._equity(game_info, current_state, override_iters=100, cat_id=cat_id)
            bid = self._compute_bid(current_state, game_info, cat_id, eq)
            self._my_last_bid = bid
            return ActionBid(bid)

        # ── Compute equity ───────────────────────────────────────────────────
        if street == 'preflop':
            equity = self._preflop_eq
        else:
            equity = self._equity(game_info, current_state, cat_id=cat_id)

        # ── Gather state ─────────────────────────────────────────────────────
        pot          = max(1, current_state.pot)
        cost_to_call = current_state.cost_to_call
        my_chips     = current_state.my_chips
        opp_chips    = current_state.opp_chips

        pot_total = pot + cost_to_call
        pot_odds  = cost_to_call / pot_total if pot_total > 0 else 0.0

        can_raise = current_state.can_act(ActionRaise)
        can_call  = current_state.can_act(ActionCall)
        can_check = current_state.can_act(ActionCheck)

        if can_raise:
            min_raise, max_raise = current_state.raise_bounds
        else:
            min_raise = max_raise = 0

        eff_stack = min(my_chips, opp_chips)
        spr = eff_stack / pot

        # ── Thresholds ───────────────────────────────────────────────────────
        raise_thresh, call_thresh = self._get_thresholds(street, equity, cat_id)

        # ── Preflop specific: positional aggression ───────────────────────────
        if street == 'preflop':
            is_sb = (current_state.my_wager == 10)  # small blind
            if is_sb and equity > 0.60 and can_raise:
                target = int(pot * random.uniform(2.0, 2.8))
                ra = max(min_raise, min(max_raise, target))
                return ActionRaise(ra)
            if equity >= raise_thresh and can_raise:
                target = int(pot * random.uniform(1.5, 2.5))
                ra = max(min_raise, min(max_raise, target))
                return ActionRaise(ra)
            if equity >= call_thresh:
                return ActionCall() if can_call else (ActionCheck() if can_check else ActionFold())
            if can_check:
                return ActionCheck()
            return ActionFold()

        # ── Post-flop ─────────────────────────────────────────────────────────

        # Detect auction result on first post-auction call
        if not self._auction_detected:
            self._auction_detected = True
            self._chips_after_auction = current_state.my_chips
            opp_r = current_state.opp_revealed_cards
            if opp_r:
                # We got a card — we either won or tied
                auction_cost = self._chips_before_auction - self._chips_after_auction
                if auction_cost == self._my_last_bid and self._my_last_bid > 0:
                    # Tied — both paid own bid
                    pass
                else:
                    # Won — we paid their bid
                    self._auction_won = True
            else:
                # No card — we lost
                self._auction_lost = True

        if cost_to_call > 0:
            # ── Facing a bet ─────────────────────────────────────────────────
            if equity >= raise_thresh and can_raise:
                ra = self._size_call_raise(equity, pot_odds, pot, min_raise, max_raise)
                return ActionRaise(ra)

            if equity >= call_thresh:
                return ActionCall() if can_call else (ActionCheck() if can_check else ActionFold())

            # Semi-bluff / bluff
            if self._should_bluff(current_state, cat_id) and can_raise and spr > 1.5:
                ra = int(min_raise + 0.25 * (max_raise - min_raise))
                ra = max(min_raise, min(max_raise, ra))
                return ActionRaise(ra)

            if can_check:
                return ActionCheck()
            return ActionFold()

        else:
            # ── No bet to call — we act first ────────────────────────────────
            if equity >= raise_thresh and can_raise:
                ra = self._size_value_bet(equity, pot, min_raise, max_raise)
                return ActionRaise(ra)

            # Medium hand: protection bet
            if equity >= 0.54 and can_raise and random.random() < 0.30:
                target = int(pot * 0.28)
                ra = max(min_raise, min(max_raise, target))
                return ActionRaise(ra)

            # Semi-bluff
            if self._should_bluff(current_state, cat_id) and can_raise and spr > 1.5:
                target = int(pot * 0.40)
                ra = max(min_raise, min(max_raise, target))
                return ActionRaise(ra)

            if can_check:
                return ActionCheck()
            if can_call:
                return ActionCall()
            return ActionFold()


if __name__ == '__main__':
    args = parse_args()
    run_bot(Bot(), args)