from pkbot.base import BaseBot
from pkbot.actions import ActionCall, ActionCheck, ActionRaise, ActionFold, ActionBid
from pkbot.states import PokerState, GameInfo
from pkbot.runner import parse_args, run_bot
import eval7
import random

# ═══════════════════════════════════════════════════════════════════════════════
# PREFLOP TABLE  —  O(1) equity lookup, no MC
# ═══════════════════════════════════════════════════════════════════════════════

def _build_preflop_table():
    table = {}
    for hi in range(2, 15):
        for lo in range(2, hi + 1):
            for suited in (True, False):
                if hi == lo:
                    vals = {14:0.852,13:0.823,12:0.795,11:0.773,10:0.752,
                            9:0.721,8:0.692,7:0.663,6:0.633,5:0.604,
                            4:0.575,3:0.546,2:0.517}
                    s = vals[hi]
                else:
                    gap = hi - lo
                    s = 0.15 + 0.022 * hi + 0.010 * lo
                    if suited: s += 0.04
                    if   gap == 1: s += 0.04
                    elif gap == 2: s += 0.02
                    elif gap == 3: s += 0.005
                    elif gap >= 5: s -= 0.035
                    if hi == 14:
                        s += 0.05
                        if   lo >= 13: s += 0.04
                        elif lo >= 12: s += 0.02
                        elif lo >= 11: s += 0.01
                    elif hi == 13 and lo >= 12:
                        s += 0.025
                table[(hi, lo, suited)] = max(0.0, min(1.0, s))
    overrides = {
        (14,14,False):0.852,(13,13,False):0.823,(12,12,False):0.795,
        (11,11,False):0.773,(10,10,False):0.752,(9,9,False):0.721,
        (14,13,True):0.667,(14,13,False):0.655,(14,12,True):0.640,
        (14,12,False):0.627,(14,11,True):0.627,(14,11,False):0.614,
        (14,10,True):0.615,(14,10,False):0.601,(13,12,True):0.598,
        (13,12,False):0.585,(13,11,True):0.583,(13,11,False):0.569,
        (12,11,True):0.574,(12,11,False):0.560,(11,10,True):0.568,
        (11,10,False):0.553,(10,9,True):0.561,(10,9,False):0.546,
        (9,8,True):0.554,(9,8,False):0.539,(8,7,True):0.546,
        (8,7,False):0.531,(7,6,True):0.537,(7,6,False):0.522,
        (14,2,True):0.584,(14,2,False):0.570,
    }
    table.update(overrides)
    return table

PREFLOP_TABLE = _build_preflop_table()

def preflop_strength(c1, c2):
    r1, r2 = c1.rank + 2, c2.rank + 2
    hi, lo = max(r1, r2), min(r1, r2)
    return PREFLOP_TABLE.get((hi, lo, c1.suit == c2.suit), 0.5)


# ═══════════════════════════════════════════════════════════════════════════════
# HAND CLASSIFICATION  —  14 categories, eval7 C evaluator (no MC needed)
#
# eval7 hand_type encoding (score >> 24):
#   0=HighCard  1=Pair  2=TwoPair  3=Trips  4=Straight
#   5=Flush  6=FullHouse  7=Quads  8=StraightFlush
# ═══════════════════════════════════════════════════════════════════════════════

# Calibrated from the 49-case HU flop taxonomy (doc 3).
# e.g. TPTK ~74%, mid pair ~55-67%, NutDraw ~54%, OESD ~40%
CATEGORY_EQUITY = {
    0: 0.93,   # Monster: flush/FH/quads/SF — ~85-100%, avg ~93
    1: 0.87,   # Strong: set/nut straight/top-two — ~84-90%
    2: 0.74,   # GoodMade: TPTK/overpair TT+ — ~72-78%
    3: 0.64,   # MedMade: top pair med kicker / mid pair — ~55-67%
    4: 0.50,   # WeakMade: bottom pair / underpair / TP weak kicker — ~38-52%
    5: 0.65,   # ComboStrong: pair + FD or OESD (~14+ outs) — ~62-76%
    6: 0.55,   # ComboMed: pair + gutshot / weaker combo — ~50-65%
    7: 0.52,   # NutDraw: nut FD + overcard(s) — ~50-54%
    8: 0.40,   # Draw: OESD or plain FD — ~39-43%
    9: 0.38,   # WeakDraw: gutshot + overcard(s) — ~36-42%
    10: 0.30,  # Gutshot only — ~30%
    11: 0.32,  # AceHigh — ~32-36%
    12: 0.25,  # HighCard — ~25-28%
    13: 0.18,  # Air — ~15%
}


def _flush_outs(hole, board):
    for s in range(4):
        total = sum(1 for c in hole + board if c.suit == s)
        mine  = sum(1 for c in hole if c.suit == s)
        if total == 4 and mine >= 1: return 9
        if total == 3 and mine >= 1 and len(board) < 4: return 3
    return 0

def _straight_outs(hole, board):
    ranks = sorted(set(c.rank + 2 for c in hole + board))
    best = 0
    for low in range(2, 11):
        if len(set(range(low, low + 5)) - set(ranks)) == 1:
            best = max(best, 4)
    for low in range(2, 11):
        seg = sorted(r for r in ranks if low <= r <= low + 3)
        for i in range(len(seg) - 3):
            if seg[i+3] - seg[i] == 3:
                best = max(best, 8)
    return best

def classify_hand(hole, board):
    if len(board) < 3:
        return (13, 'Air')

    score     = eval7.evaluate(hole + board)
    hand_type = score >> 24   # 0=HighCard … 8=StraightFlush

    fd = _flush_outs(hole, board)
    sd = _straight_outs(hole, board)
    has_fd   = fd >= 9
    has_oesd = sd >= 8
    has_gut  = sd >= 4
    has_bd   = fd == 3

    if hand_type >= 5: return (0, 'Monster')   # Flush, FH, Quads, SF
    if hand_type == 4: return (1, 'Strong')    # Straight

    if hand_type == 3:  # Trips
        br = [c.rank for c in board]
        for hr in [c.rank for c in hole]:
            if br.count(hr) >= 2: return (1, 'Strong')   # set
        return (3, 'MedMade')                              # board trips

    if hand_type == 2:  # Two Pair
        br = [c.rank for c in board]
        hr = [c.rank for c in hole]
        from_hole = sum(1 for r in hr if r in br)
        if from_hole == 2:
            return (1, 'Strong') if max(hr) == max(br) else (2, 'GoodMade')
        return (3, 'MedMade')

    if hand_type == 1:  # One Pair
        br_s   = sorted([c.rank for c in board], reverse=True)
        hr_s   = sorted([c.rank for c in hole],  reverse=True)
        br_set = set(br_s)

        if hr_s[0] == hr_s[1]:   # pocket pair
            if hr_s[0] > max(br_s):
                return (2, 'GoodMade') if hr_s[0] >= 10 else (4, 'WeakMade')
            if has_fd or has_oesd: return (6, 'ComboMed')
            return (4, 'WeakMade')

        paired = next((r for r in hr_s if r in br_set), None)
        if paired is None: return (13, 'Air')

        kicker = max(r for r in hr_s if r != paired)
        top_b  = max(br_s)
        mid_b  = br_s[len(br_s) // 2]
        is_top = paired == top_b
        is_mid = paired == mid_b and not is_top

        if is_top:
            other_b = [r for r in br_s if r != paired]
            need = max(other_b) if other_b else 0
            if kicker >= need or kicker >= 10:
                return (5, 'ComboStrong') if (has_fd or has_oesd) else (2, 'GoodMade')
            elif kicker >= 7:
                return (6, 'ComboMed') if (has_fd or has_oesd) else (3, 'MedMade')
            else:
                return (6, 'ComboMed') if (has_fd or has_oesd) else (4, 'WeakMade')
        elif is_mid:
            if has_fd and has_oesd:            return (5, 'ComboStrong')
            if has_fd or has_oesd or has_gut:  return (6, 'ComboMed')
            return (4, 'WeakMade')
        else:
            return (6, 'ComboMed') if (has_fd or has_oesd) else (4, 'WeakMade')

    # High card (0) — draw territory
    hr_vals = sorted([c.rank + 2 for c in hole], reverse=True)
    brd_max = max(c.rank + 2 for c in board)
    overs   = sum(1 for r in hr_vals if r > brd_max)

    if has_fd and has_oesd:     return (5, 'ComboStrong')
    if has_fd and overs >= 1:   return (7, 'NutDraw')
    if has_fd:                  return (8, 'Draw')
    if has_oesd and overs == 2: return (7, 'NutDraw')
    if has_oesd:                return (8, 'Draw')
    if has_gut and overs >= 1:  return (9, 'WeakDraw')
    if has_gut:                 return (10, 'Gutshot')
    if hr_vals[0] == 14:        return (11, 'AceHigh')
    if hr_vals[0] >= 13:        return (12, 'HighCard')
    if has_bd:                  return (12, 'HighCard')
    return (13, 'Air')


# ═══════════════════════════════════════════════════════════════════════════════
# AUCTION BID TABLE  —  fraction of pot by category (zero MC overhead)
#
# Middle/draw categories bid highest: information most changes our decision
# when hand strength is uncertain. Monsters & air have near-zero info value.
# Intimidation bonus (+0.05) for cats 2-8: winning auction suppresses opponent
# bluffing because they know we have one of their cards revealed.
# ═══════════════════════════════════════════════════════════════════════════════

AUCTION_BID_FRAC = {
    0:  0.00,   # Monster — winning anyway, info worthless
    1:  0.08,   # Strong — slight confirmation value
    2:  0.22,   # GoodMade — want to know if dominated
    3:  0.30,   # MedMade — highest uncertainty, most info value
    4:  0.25,   # WeakMade — need info to decide fold/continue
    5:  0.32,   # ComboStrong — pair+draw, info is crucial
    6:  0.25,   # ComboMed — significant uncertainty
    7:  0.28,   # NutDraw — knowing their hand changes pot odds decision
    8:  0.22,   # Draw — moderate info value
    9:  0.15,   # WeakDraw — modest
    10: 0.10,   # Gutshot — low value
    11: 0.18,   # AceHigh — card reveal often clarifies a lot
    12: 0.08,   # HighCard — might fold anyway
    13: 0.02,   # Air — noise bid only, for unpredictability
}

INTIMIDATION_BONUS = 0.05   # applied to cats 2-8


# ═══════════════════════════════════════════════════════════════════════════════
# REVEALED CARD THREAT ANALYSIS
#
# When we WIN the auction and see their card, we score how threatening it is
# vs the current board. This directly drives the strategic matrix:
#
#   Low threat (brick):  they likely have weak/air → lower raise_t → be aggressive
#                        also boost bluff probability (we know they're weak)
#   High threat (connected): they likely have a real hand → raise raise_t
#                             lean toward pot control / call-down lines
#                             avoid bloating pot on draws
#
#   Lost auction: they have info on us → tighten slightly (they can value-bet us)
# ═══════════════════════════════════════════════════════════════════════════════

def revealed_card_threat(rev_card, board):
    """Returns 0.0 (brick) to 1.0 (very connected). 0.5 = unknown/neutral."""
    if rev_card is None or not board:
        return 0.5

    r = rev_card.rank + 2
    s = rev_card.suit
    board_ranks = [c.rank + 2 for c in board]
    board_suits = [c.suit for c in board]
    threat = 0.0

    if r in board_ranks:
        threat += 0.40   # pairs the board — real made hand possible

    suit_count = board_suits.count(s)
    if suit_count >= 2:
        threat += 0.25   # flush draw component

    straight_connects = sum(1 for br in board_ranks if abs(r - br) <= 2)
    if straight_connects >= 2:
        threat += 0.20
    elif straight_connects == 1:
        threat += 0.10

    if r >= 13:
        threat += 0.15   # high card — dangerous on its own

    return min(1.0, threat)


# ═══════════════════════════════════════════════════════════════════════════════
# MONTE CARLO EQUITY  (Bayesian-weighted when one opp card known)
# ═══════════════════════════════════════════════════════════════════════════════

def calc_equity(hole, board, opp_revealed, iters):
    known = set(hole + board + opp_revealed)
    deck  = [c for c in eval7.Deck().cards if c not in known]

    need_opp   = 2 - len(opp_revealed)
    need_board = 5 - len(board)
    if need_opp + need_board > len(deck):
        return 0.5

    weights = None
    if len(opp_revealed) == 1 and need_opp == 1:
        rev_r = opp_revealed[0].rank + 2
        w = []
        for c in deck:
            cr = c.rank + 2
            hi, lo = max(rev_r, cr), min(rev_r, cr)
            w.append(PREFLOP_TABLE.get((hi, lo, opp_revealed[0].suit == c.suit), 0.3))
        tot = sum(w)
        if tot > 0:
            weights = [x / tot for x in w]

    wins, valid = 0.0, 0
    for _ in range(iters):
        try:
            if weights and need_opp == 1:
                unk  = random.choices(deck, weights=weights, k=1)
                rest = [c for c in deck if c not in unk]
                samp = unk + random.sample(rest, need_board)
            else:
                samp = random.sample(deck, need_opp + need_board)
        except (ValueError, IndexError):
            break
        opp_hole    = opp_revealed + samp[:need_opp]
        final_board = board + samp[need_opp:]
        my_s  = eval7.evaluate(hole + final_board)
        opp_s = eval7.evaluate(opp_hole + final_board)
        valid += 1
        if   my_s > opp_s: wins += 1.0
        elif my_s == opp_s: wins += 0.5

    return wins / valid if valid > 0 else 0.5


# ═══════════════════════════════════════════════════════════════════════════════
# OPPONENT MODEL  —  minimal EMA, fast adaptation
# ═══════════════════════════════════════════════════════════════════════════════

class OpponentModel:
    ALPHA = 0.08

    def __init__(self):
        self.hands          = 0
        self.ema_aggression = 0.30
        self.ema_fold_rate  = 0.40
        self.ema_bid        = 8.0
        self.ema_auc_win    = 0.50

    def _ema(self, old, obs):
        return (1.0 - self.ALPHA) * old + self.ALPHA * obs

    def observe_action(self, action_type):
        self.ema_aggression = self._ema(self.ema_aggression, 1.0 if action_type == 'raise' else 0.0)
        self.ema_fold_rate  = self._ema(self.ema_fold_rate,  1.0 if action_type == 'fold'  else 0.0)

    def observe_bid(self, bid):
        if bid >= 0:
            self.ema_bid = self._ema(self.ema_bid, float(bid))

    def observe_auction(self, they_won):
        self.ema_auc_win = self._ema(self.ema_auc_win, 1.0 if they_won else 0.0)

    @property
    def is_foldy(self):   return self.ema_fold_rate > 0.45
    @property
    def is_station(self): return self.ema_fold_rate < 0.22 and self.ema_aggression < 0.25
    @property
    def is_aggro(self):   return self.ema_aggression > 0.45


# ═══════════════════════════════════════════════════════════════════════════════
# BOT
# ═══════════════════════════════════════════════════════════════════════════════

class Bot(BaseBot):

    def __init__(self):
        self.opp   = OpponentModel()
        self.round = 0

        self._hole            = []
        self._preflop_eq      = 0.5
        self._cat             = (13, 'Air')
        self._prev_opp_wgr    = 0
        self._prev_street     = 'preflop'
        self._auction_won     = False
        self._auction_lost    = False
        self._chips_pre_auc   = 5000
        self._chips_post_auc  = 0
        self._my_bid          = 0
        self._auc_detected    = False
        self._revealed_threat = 0.5

    # ── MC iteration budget ──────────────────────────────────────────────────
    # Total target: ~5-8s per 1000-hand game
    # Obvious hands (monster/air) get low iters — result is clear already

    def _iters(self, tb, cat_id):
        if tb < 3.0: return 0      # emergency: use category fallback
        if tb < 5.0: return 12

        if   tb > 18: base = 100
        elif tb > 14: base = 80
        elif tb > 10: base = 60
        elif tb > 7:  base = 45
        else:         base = 25

        cap = {0: 15, 1: 40, 12: 15, 13: 10, 11: 18}.get(cat_id, base)
        return min(base, cap)

    # ── Equity ────────────────────────────────────────────────────────────────

    def _equity(self, gs, tb, override_iters=None):
        hole   = [eval7.Card(s) for s in gs.my_hand]
        board  = [eval7.Card(s) for s in gs.board]
        opp_r  = [eval7.Card(s) for s in gs.opp_revealed_cards]
        cat_id = self._cat[0]

        if gs.street == 'preflop' and not opp_r:
            return self._preflop_eq

        n = override_iters if override_iters is not None else self._iters(tb, cat_id)
        if n == 0:
            return CATEGORY_EQUITY[cat_id]
        return calc_equity(hole, board, opp_r, n)

    # ── Auction bid (category-based, ZERO MC) ─────────────────────────────────

    def _auction_bid(self, cat_id, pot, my_chips):
        frac = AUCTION_BID_FRAC.get(cat_id, 0.10)
        if 2 <= cat_id <= 8:
            frac += INTIMIDATION_BONUS

        bid = int(pot * frac)
        bid = min(bid, int(my_chips * 0.08))

        # Floor for weak hands — always bid something to stay unpredictable
        if cat_id in (9, 10, 11) and bid < 3:
            bid = random.randint(1, 6)

        # Noise ±8%
        if bid > 4:
            bid = int(bid * random.uniform(0.92, 1.08))

        return max(0, min(bid, my_chips))

    # ── Thresholds ────────────────────────────────────────────────────────────

    def _thresholds(self, street):
        ftr = self.opp.ema_fold_rate
        agg = self.opp.ema_aggression

        raise_t = 0.70
        call_t  = 0.40

        if ftr > 0.42:
            delta = min(0.10, 0.16 * (ftr - 0.42))
            raise_t -= delta
            call_t  -= delta * 0.5

        if agg > 0.42:
            delta = min(0.08, 0.14 * (agg - 0.42))
            raise_t += delta
            call_t  += delta * 0.6

        if self.opp.is_station:
            raise_t -= 0.05
            call_t  -= 0.04

        if street == 'river':
            raise_t += 0.02
            call_t  += 0.02

        return max(0.52, min(0.82, raise_t)), max(0.28, min(0.58, call_t))

    # ── Revealed card adjustment ──────────────────────────────────────────────
    # Implements the strategic matrix:
    #
    # WON + low threat (brick revealed):
    #   → they likely have air/weak → lower thresholds, be more aggressive
    #   → boost bluff probability (we KNOW they're weak, they don't know we know)
    #
    # WON + high threat (connected card):
    #   → they likely have something real → raise thresholds
    #   → pot control mode; lean toward calling down, not building pot
    #   → on draws specifically: don't overcommit vs likely made hand
    #
    # LOST auction:
    #   → they have info on us → they can value-bet accurately → fewer bluffs
    #   → tighten slightly to avoid calling off chips vs their value

    def _apply_auction_adj(self, raise_t, call_t, cat_id):
        if self._auction_won:
            threat = self._revealed_threat
            if threat < 0.25:          # brick — they're weak
                raise_t -= 0.06
                call_t  -= 0.04
            elif threat > 0.65:        # connected — they have something
                raise_t += 0.07
                call_t  += 0.04
                if cat_id in (7, 8, 9, 10):   # don't chase draws vs made hands
                    raise_t += 0.04
        elif self._auction_lost:
            raise_t += 0.03
            call_t  += 0.02
        return raise_t, call_t

    # ── Bet sizing ────────────────────────────────────────────────────────────

    def _value_bet_size(self, equity, pot, min_r, max_r):
        frac   = max(0.0, min(1.0, (equity - 0.55) / 0.35))
        target = int(pot * (0.38 + frac * 0.47))
        return max(min_r, min(max_r, target))

    def _raise_size(self, equity, pot_odds, min_r, max_r):
        edge   = max(0.0, equity - pot_odds)
        frac   = min(1.0, edge * 2.5)
        target = int(min_r + frac * (max_r - min_r))
        return max(min_r, min(max_r, target))

    # ── Bluff ─────────────────────────────────────────────────────────────────

    def _should_bluff(self, cat_id, spr):
        if self.opp.is_station: return False
        if spr < 1.2:           return False
        ftr = self.opp.ema_fold_rate
        if ftr < 0.38:          return False

        # Extra bluff boost when we won auction and their card was a brick —
        # we know they're weak AND they don't know exactly what we know
        bluff_boost = 0.08 if (self._auction_won and self._revealed_threat < 0.30) else 0.0

        if   cat_id in (5, 6, 7, 8): prob = 0.18 + 0.25 * (ftr - 0.38) + bluff_boost
        elif cat_id in (9, 10, 11):  prob = 0.08 + 0.15 * (ftr - 0.38) + bluff_boost
        elif cat_id in (12, 13):     prob = 0.04 + 0.10 * (ftr - 0.38) + bluff_boost
        else:                        return False

        return random.random() < min(prob, 0.40)

    # ── Engine callbacks ──────────────────────────────────────────────────────

    def on_hand_start(self, gi: GameInfo, gs: PokerState):
        self.round += 1
        self._hole            = [eval7.Card(s) for s in gs.my_hand]
        self._cat             = (13, 'Air')
        self._prev_opp_wgr    = gs.opp_wager
        self._prev_street     = 'preflop'
        self._auction_won     = False
        self._auction_lost    = False
        self._auc_detected    = False
        self._chips_pre_auc   = gs.my_chips
        self._chips_post_auc  = 0
        self._my_bid          = 0
        self._revealed_threat = 0.5
        self._preflop_eq      = preflop_strength(self._hole[0], self._hole[1]) \
                                if len(self._hole) == 2 else 0.5

    def on_hand_end(self, gi: GameInfo, gs: PokerState):
        self.opp.hands += 1

        if gs.is_terminal and gs.payoff > 0 and gs.cost_to_call != 0:
            self.opp.observe_action('fold')

        if self._chips_post_auc > 0:
            auction_cost = self._chips_pre_auc - self._chips_post_auc
            if self._auction_won:
                self.opp.observe_bid(auction_cost)
                self.opp.observe_auction(they_won=False)
            elif self._auction_lost:
                self.opp.observe_bid(self._my_bid + random.randint(1, 20))
                self.opp.observe_auction(they_won=True)
            else:
                self.opp.observe_bid(self._my_bid)
                self.opp.observe_auction(they_won=False)

    # ── Main decision ─────────────────────────────────────────────────────────

    def get_move(self, gi: GameInfo, gs: PokerState):
        street = gs.street
        tb     = gi.time_bank

        # Track opponent actions
        if street != self._prev_street:
            self._prev_opp_wgr = 0
        opp_wgr = gs.opp_wager
        if street != 'auction':
            if opp_wgr > self._prev_opp_wgr:
                self.opp.observe_action('raise' if gs.cost_to_call > 0 else 'call')
            elif (opp_wgr == self._prev_opp_wgr and gs.cost_to_call == 0
                  and street == self._prev_street and street != 'preflop'):
                self.opp.observe_action('check')
        self._prev_opp_wgr = opp_wgr
        self._prev_street  = street

        # Classify hand on postflop streets
        if street != 'preflop':
            board = [eval7.Card(s) for s in gs.board]
            if len(board) >= 3:
                self._cat = classify_hand(self._hole, board)

        cat_id = self._cat[0]

        # ═════════════════════════════════════════════════════════════════════
        # AUCTION  —  category-based bid, zero MC
        # ═════════════════════════════════════════════════════════════════════
        if street == 'auction':
            self._chips_pre_auc = gs.my_chips
            bid = self._auction_bid(cat_id, max(1, gs.pot), gs.my_chips)
            self._my_bid = bid
            return ActionBid(bid)

        # Detect auction outcome + compute revealed card threat
        if not self._auc_detected and street in ('flop', 'turn', 'river'):
            self._auc_detected   = True
            self._chips_post_auc = gs.my_chips
            board_cards          = [eval7.Card(s) for s in gs.board]

            if gs.opp_revealed_cards:
                paid = self._chips_pre_auc - gs.my_chips
                if paid != self._my_bid:
                    self._auction_won = True
                # else: tie — both paid own bid

                rev_card = eval7.Card(gs.opp_revealed_cards[0])
                self._revealed_threat = revealed_card_threat(rev_card, board_cards)
            else:
                self._auction_lost    = True
                self._revealed_threat = 0.5

        # Equity
        equity = self._equity(gs, tb)

        # State
        pot       = max(1, gs.pot)
        cost      = gs.cost_to_call
        my_chips  = gs.my_chips
        opp_chips = gs.opp_chips
        pot_odds  = cost / (pot + cost) if (pot + cost) > 0 else 0.0
        spr       = min(my_chips, opp_chips) / pot

        can_raise = gs.can_act(ActionRaise)
        can_call  = gs.can_act(ActionCall)
        can_check = gs.can_act(ActionCheck)
        min_r = max_r = 0
        if can_raise:
            min_r, max_r = gs.raise_bounds

        raise_t, call_t = self._thresholds(street)
        raise_t, call_t = self._apply_auction_adj(raise_t, call_t, cat_id)

        # ═════════════════════════════════════════════════════════════════════
        # PREFLOP
        # ═════════════════════════════════════════════════════════════════════
        if street == 'preflop':
            is_sb = (gs.my_wager == 10)

            if can_call and cost > 0:
                eff        = min(my_chips, opp_chips)
                shove_frac = cost / max(1, eff)
                if shove_frac >= 0.70:
                    thresh = max(pot_odds + 0.08, 0.46)
                    return ActionCall() if equity >= thresh else ActionFold()
                if shove_frac >= 0.40:
                    thresh = max(pot_odds + 0.05, 0.43)
                    return ActionCall() if equity >= thresh else ActionFold()

            if is_sb:
                if equity > 0.55 and can_raise:
                    sz = int(pot * random.uniform(2.0, 2.8))
                    return ActionRaise(max(min_r, min(max_r, sz)))
                if equity > 0.40 and can_call:
                    return ActionCall()
                if can_call and cost <= 10 and equity > 0.32:
                    return ActionCall()
                return ActionFold()
            else:
                if equity >= raise_t and can_raise:
                    sz = int(pot * random.uniform(1.8, 2.5))
                    return ActionRaise(max(min_r, min(max_r, sz)))
                if equity >= call_t:
                    return ActionCall() if can_call else (ActionCheck() if can_check else ActionFold())
                if can_check:
                    return ActionCheck()
                return ActionFold()

        # ═════════════════════════════════════════════════════════════════════
        # POST-FLOP  (flop / turn / river)
        # ═════════════════════════════════════════════════════════════════════

        # Tighten call threshold vs overbets
        if cost > 0 and pot > 0:
            overbet = cost / pot
            if   overbet >= 1.5: call_t = max(call_t, 0.58)
            elif overbet >= 0.9: call_t = max(call_t, 0.50)
            elif overbet >= 0.6: call_t = max(call_t, 0.44)

        if cost > 0:
            if equity >= raise_t and can_raise:
                ra = self._raise_size(equity, pot_odds, min_r, max_r)
                return ActionRaise(ra)
            if equity >= call_t:
                return ActionCall() if can_call else (ActionCheck() if can_check else ActionFold())
            if self._should_bluff(cat_id, spr) and can_raise:
                ra = max(min_r, min(max_r, int(min_r + 0.25 * (max_r - min_r))))
                return ActionRaise(ra)
            if can_check: return ActionCheck()
            return ActionFold()

        else:
            if equity >= raise_t and can_raise:
                ra = self._value_bet_size(equity, pot, min_r, max_r)
                return ActionRaise(ra)
            if cat_id in (2, 3) and equity >= 0.58 and can_raise:
                freq = 0.50 if street == 'flop' else 0.65
                if random.random() < freq:
                    ra = self._value_bet_size(equity, pot, min_r, max_r)
                    return ActionRaise(ra)
            if equity >= 0.54 and can_raise and random.random() < 0.20:
                ra = max(min_r, min(max_r, int(pot * 0.30)))
                return ActionRaise(ra)
            if self._should_bluff(cat_id, spr) and can_raise:
                ra = max(min_r, min(max_r, int(pot * 0.40)))
                return ActionRaise(ra)
            if can_check: return ActionCheck()
            if can_call:  return ActionCall()
            return ActionFold()


if __name__ == '__main__':
    args = parse_args()
    run_bot(Bot(), args)
