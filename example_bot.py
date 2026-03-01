from pkbot.base import BaseBot
from pkbot.actions import ActionCall, ActionCheck, ActionRaise, ActionFold, ActionBid
from pkbot.states import PokerState, GameInfo
from pkbot.runner import parse_args, run_bot
import eval7
import random

# ═══════════════════════════════════════════════════════════════════════════════
# PREFLOP LOOKUP TABLE
# Maps (hi_rank, lo_rank, suited) → HU equity vs random hand [0,1]
# Built once at import. O(1) lookup at runtime.
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
                    s   = 0.15 + 0.022*hi + 0.010*lo
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
# BOARD TEXTURE
# ═══════════════════════════════════════════════════════════════════════════════

def board_texture(board):
    if len(board) < 3:
        return 'dry'
    ranks = [c.rank for c in board]
    suits = [c.suit for c in board]

    if len(ranks) != len(set(ranks)):
        return 'paired'
    if len(set(suits)) == 1:
        return 'monotone'

    suit_counts    = [suits.count(s) for s in set(suits)]
    has_flush_draw = any(c >= 2 for c in suit_counts)
    sorted_r       = sorted(set(ranks))
    connectivity   = sum(1 for i in range(len(sorted_r)-1)
                         if sorted_r[i+1] - sorted_r[i] <= 3)

    if has_flush_draw and connectivity >= 2: return 'wet'
    if has_flush_draw or  connectivity >= 1: return 'semi_wet'
    return 'dry'


# ═══════════════════════════════════════════════════════════════════════════════
# HAND CLASSIFICATION
# Fast categorisation using eval7's C evaluator — no MC needed.
# 14 buckets: 0=Monster … 13=Air
#
# Role: gate MC iters + apply small nudges ON TOP of MC result.
# Never used as the primary equity source when MC is available.
# ═══════════════════════════════════════════════════════════════════════════════

# Fallback hints — ONLY used when time bank is critically low (<4s)
CATEGORY_EQUITY_HINT = {
    0:0.93, 1:0.87, 2:0.75, 3:0.62, 4:0.50,
    5:0.65, 6:0.54, 7:0.49, 8:0.41, 9:0.36,
    10:0.29, 11:0.32, 12:0.25, 13:0.18,
}

# Small nudges applied ON TOP of MC result to correct low-iter bias
CATEGORY_MC_NUDGE = {
    0:+0.02, 1:+0.01, 2: 0.00, 3: 0.00, 4:-0.01,
    5:+0.03, 6:+0.02, 7:+0.02, 8:+0.01, 9:-0.01,
    10:-0.02, 11:-0.01, 12:-0.02, 13:-0.03,
}

def _flush_outs(hole, board):
    for s in range(4):
        total = sum(1 for c in hole+board if c.suit == s)
        mine  = sum(1 for c in hole        if c.suit == s)
        if total == 4 and mine >= 1: return 9
        if total == 3 and mine >= 1 and len(board) < 4: return 3
    return 0

def _straight_outs(hole, board):
    ranks = sorted(set(c.rank+2 for c in hole+board))
    best  = 0
    for low in range(2, 11):
        if len(set(range(low, low+5)) - set(ranks)) == 1:
            best = max(best, 4)
    for low in range(2, 11):
        seg = sorted(r for r in ranks if low <= r <= low+3)
        for i in range(len(seg)-3):
            if seg[i+3] - seg[i] == 3:
                best = max(best, 8)
    return best

def classify_hand(hole, board):
    if len(board) < 3: return (13, 'Air')

    score     = eval7.evaluate(hole + board)
    hand_type = score >> 24

    fd = _flush_outs(hole, board)
    sd = _straight_outs(hole, board)
    has_fd, has_bd   = (fd >= 9), (fd == 3)
    has_oesd, has_gut = (sd >= 8), (sd >= 4)

    # eval7 encoding: 0=HighCard 1=Pair 2=TwoPair 3=Trips 4=Straight 5=Flush 6=FH 7=Quads 8=SF
    if hand_type >= 6: return (0, 'Monster')
    if hand_type == 5: return (0, 'Monster')
    if hand_type == 4: return (1, 'Strong')

    if hand_type == 3:
        br = [c.rank for c in board]
        for hr in [c.rank for c in hole]:
            if br.count(hr) >= 2: return (1, 'Strong')
        return (3, 'MedMade')

    if hand_type == 2:
        br = [c.rank for c in board]
        hr = [c.rank for c in hole]
        from_hole = sum(1 for r in hr if r in br)
        if from_hole == 2:
            return (1,'Strong') if max(hr)==max(br) else (2,'GoodMade')
        return (3, 'MedMade')

    if hand_type == 1:
        br_s   = sorted([c.rank for c in board], reverse=True)
        hr_s   = sorted([c.rank for c in hole],  reverse=True)
        br_set = set(br_s)

        if hr_s[0] == hr_s[1]:  # pocket pair
            if hr_s[0] > max(br_s):
                return (2,'GoodMade') if hr_s[0] >= 10 else (4,'WeakMade')
            if has_fd or has_oesd: return (6,'ComboMed')
            return (4, 'WeakMade')

        paired = next((r for r in hr_s if r in br_set), None)
        if paired is None: return (13, 'Air')

        kicker = max(r for r in hr_s if r != paired)
        top_b  = max(br_s)
        mid_b  = br_s[len(br_s)//2]
        is_top = (paired == top_b)
        is_mid = (paired == mid_b and not is_top)

        if is_top:
            other_b = [r for r in br_s if r != paired]
            need    = max(other_b) if other_b else 0
            if kicker >= need or kicker >= 10:
                if has_fd or has_oesd: return (5,'ComboStrong')
                return (2, 'GoodMade')
            elif kicker >= 7:
                if has_fd or has_oesd: return (6,'ComboMed')
                return (3, 'MedMade')
            else:
                if has_fd or has_oesd: return (6,'ComboMed')
                return (4, 'WeakMade')
        elif is_mid:
            if has_fd and has_oesd:         return (5,'ComboStrong')
            if has_fd or has_oesd or has_gut: return (6,'ComboMed')
            return (4, 'WeakMade')
        else:
            if has_fd or has_oesd: return (6,'ComboMed')
            return (4, 'WeakMade')

    # High card — draw territory
    hr_vals = sorted([c.rank+2 for c in hole], reverse=True)
    brd_max = max(c.rank+2 for c in board)
    overs   = sum(1 for r in hr_vals if r > brd_max)

    if has_fd and has_oesd:        return (5,'ComboStrong')
    if has_fd and overs >= 1:      return (7,'NutDraw')
    if has_fd:                     return (8,'Draw')
    if has_oesd and overs == 2:    return (7,'NutDraw')
    if has_oesd:                   return (8,'Draw')
    if has_gut and overs >= 1:     return (9,'WeakDraw')
    if has_gut:                    return (10,'Gutshot')
    if hr_vals[0] == 14:           return (11,'AceHigh')
    if hr_vals[0] >= 13:           return (12,'HighCard')
    if has_bd:                     return (12,'HighCard')
    return (13, 'Air')


# ═══════════════════════════════════════════════════════════════════════════════
# MONTE CARLO EQUITY
# ═══════════════════════════════════════════════════════════════════════════════

def calc_equity(hole, board, opp_revealed, iters):
    known = set(hole + board + opp_revealed)
    deck  = [c for c in eval7.Deck().cards if c not in known]

    need_opp   = 2 - len(opp_revealed)
    need_board = 5 - len(board)
    if need_opp + need_board > len(deck): return 0.5

    # Bayesian range weighting when one opponent card is revealed
    weights = None
    if len(opp_revealed) == 1 and need_opp == 1:
        rev_r = opp_revealed[0].rank + 2
        w = []
        for c in deck:
            cr = c.rank + 2
            hi, lo = max(rev_r, cr), min(rev_r, cr)
            w.append(PREFLOP_TABLE.get((hi, lo, opp_revealed[0].suit==c.suit), 0.3))
        tot = sum(w)
        if tot > 0:
            weights = [x/tot for x in w]

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
        final_board = board        + samp[need_opp:]
        my_s  = eval7.evaluate(hole + final_board)
        opp_s = eval7.evaluate(opp_hole + final_board)
        valid += 1
        if   my_s > opp_s: wins += 1.0
        elif my_s == opp_s: wins += 0.5

    return wins/valid if valid > 0 else 0.5


def quick_info_value(hole, board, n_samples=8, iters_per=15):
    """
    Estimate expected equity swing from seeing one opponent card.
    Budget: 8 * 15 = 120 MC calls (was 375 in previous version).
    """
    known = set(hole + board)
    deck  = [c for c in eval7.Deck().cards if c not in known]
    if len(deck) < 2: return 0.0

    base = calc_equity(hole, board, [], iters_per)
    k    = min(n_samples, len(deck))
    return sum(abs(calc_equity(hole, board, [c], iters_per) - base)
               for c in random.sample(deck, k)) / k


# ═══════════════════════════════════════════════════════════════════════════════
# OPPONENT MODEL — EMA based, no hard profile flips
#
# Every action updates a smooth exponential moving average.
# Strategy thresholds are continuous functions of these stats.
# Alpha=0.07 → roughly weights last ~14 observations equally.
# ═══════════════════════════════════════════════════════════════════════════════

class OpponentModel:
    ALPHA = 0.07

    def __init__(self):
        self.hands = 0
        # EMA stats — neutral priors
        self.ema_aggression       = 0.30
        self.ema_fold_rate        = 0.45
        self.ema_vpip             = 0.60
        self.ema_bid              = 5.0
        self.ema_bid_sq           = 50.0
        self.ema_auction_win_rate = 0.50
        self.auction_total        = 0
        # Raw counts for MDF check
        self.street_folds   = [0, 0, 0, 0]
        self.street_actions = [0, 0, 0, 0]
        # Showdowns
        self.showdowns = []

    def _ema(self, old, obs):
        return (1.0 - self.ALPHA)*old + self.ALPHA*obs

    def observe_action(self, action_type, street_idx):
        is_raise = (action_type == 'raise')
        is_fold  = (action_type == 'fold')
        self.ema_aggression = self._ema(self.ema_aggression, 1.0 if is_raise else 0.0)
        self.ema_fold_rate  = self._ema(self.ema_fold_rate,  1.0 if is_fold  else 0.0)
        if street_idx == 0:
            self.ema_vpip = self._ema(self.ema_vpip, 0.0 if is_fold else 1.0)
        self.street_actions[street_idx] += 1
        if is_fold:
            self.street_folds[street_idx] += 1

    def observe_bid(self, bid_est):
        if bid_est <= 0: return
        self.ema_bid    = self._ema(self.ema_bid,    float(bid_est))
        self.ema_bid_sq = self._ema(self.ema_bid_sq, float(bid_est)**2)

    def observe_auction(self, opp_won):
        self.auction_total += 1
        self.ema_auction_win_rate = self._ema(
            self.ema_auction_win_rate, 1.0 if opp_won else 0.0)

    @property
    def bid_std(self):
        return max(10.0, (max(0.0, self.ema_bid_sq - self.ema_bid**2))**0.5)

    def street_fold_rate(self, idx):
        a = self.street_actions[idx]
        return self.street_folds[idx] / a if a > 0 else 0.45

    @property
    def is_auction_maniac(self):
        return self.ema_bid > 220 and self.ema_auction_win_rate > 0.58

    @property
    def is_calling_station(self):
        return self.ema_fold_rate < 0.25 and self.ema_vpip > 0.68

    @property
    def is_passive(self):
        return self.ema_aggression < 0.18


# ═══════════════════════════════════════════════════════════════════════════════
# BOT
# ═══════════════════════════════════════════════════════════════════════════════

class Bot(BaseBot):

    def __init__(self):
        self.opp   = OpponentModel()
        self.round = 0

        self._hole          = []
        self._preflop_eq    = 0.5
        self._cat           = (13, 'Air')
        self._texture       = 'dry'
        self._prev_opp_wgr  = 0
        self._prev_street   = 'preflop'
        self._auction_won   = False
        self._auction_lost  = False
        self._chips_pre_auc = 5000
        self._chips_post_auc = 0
        self._my_last_bid   = 0
        self._auc_detected  = False
        self._recent_pnl    = []

    # ─────────────────────────────────────────────────────────────────────────
    # TIME-ADAPTIVE MC ITERS
    # Hard floor at tb < 4s. Category ceilings prevent wasted budget.
    # Total per-round MC budget (all streets): ~280-380 calls vs ~995 before.
    # ─────────────────────────────────────────────────────────────────────────

    def _iters(self, tb, street, cat_id):
        if street == 'preflop': return 0
        if tb < 4.0: return 8   # emergency — preserve time bank

        if   tb > 17: base = 120
        elif tb > 13: base = 90
        elif tb > 9:  base = 65
        elif tb > 6:  base = 45
        elif tb > 4:  base = 25
        else:         base = 12

        # Category ceilings — no point burning iters on obvious hands
        ceiling = {0:20, 1:55, 13:12, 12:15, 11:18}
        base = min(base, ceiling.get(cat_id, base))

        if street == 'river': base = int(base * 0.70)
        return max(8, base)

    # ─────────────────────────────────────────────────────────────────────────
    # EQUITY
    # MC result + category nudge + board texture adjustment.
    # Category hints are fallback only (tb < 4s).
    # ─────────────────────────────────────────────────────────────────────────

    def _equity(self, gs, tb, override=None):
        hole   = [eval7.Card(s) for s in gs.my_hand]
        board  = [eval7.Card(s) for s in gs.board]
        opp_r  = [eval7.Card(s) for s in gs.opp_revealed_cards]
        street = gs.street
        cat_id = self._cat[0]

        if street == 'preflop' and not opp_r:
            return preflop_strength(hole[0], hole[1]) if len(hole)==2 else 0.5

        n = override if override is not None else self._iters(tb, street, cat_id)

        # Time critical fallback
        if n < 8:
            base = CATEGORY_EQUITY_HINT[cat_id]
            tex  = self._texture
            if tex in ('wet', 'monotone') and cat_id in (1,2,3,4): base -= 0.05
            if tex == 'dry'               and cat_id in (1,2,3,4): base += 0.02
            return max(0.05, min(0.95, base))

        raw = calc_equity(hole, board, opp_r, n)

        # Nudge 1: category-specific MC bias correction
        nudge = CATEGORY_MC_NUDGE.get(cat_id, 0.0)

        # Nudge 2: board texture
        tex = self._texture
        if   tex == 'wet'      and cat_id in (1,2,3,4): nudge -= 0.02
        elif tex == 'monotone' and cat_id in (1,2,3,4): nudge -= 0.03
        elif tex == 'dry'      and cat_id in (1,2,3,4): nudge += 0.01

        return max(0.05, min(0.95, raw + nudge))

    # ─────────────────────────────────────────────────────────────────────────
    # THRESHOLDS — smooth EMA deltas, no binary mode switches
    # ─────────────────────────────────────────────────────────────────────────

    def _thresholds(self, street, cat_id):
        ftr = self.opp.ema_fold_rate
        agg = self.opp.ema_aggression

        raise_t = 0.72
        call_t  = 0.42

        # Fold exploitation: continuous, capped at ±0.10
        fold_delta = min(0.10, 0.15 * max(0, ftr - 0.40))
        raise_t   -= fold_delta
        call_t    -= fold_delta * 0.6

        # Aggression tightening: capped at ±0.08
        agg_delta = min(0.08, 0.14 * max(0, agg - 0.45))
        raise_t  += agg_delta
        call_t   += agg_delta * 0.7

        # Calling station: bet thinner for value
        if self.opp.is_calling_station:
            raise_t -= 0.04
            call_t  -= 0.03

        # River: marginally tighter (no equity improvement ahead)
        if street == 'river':
            raise_t += 0.02
            call_t  += 0.02

        return max(0.52, min(0.82, raise_t)), max(0.30, min(0.60, call_t))

    # ─────────────────────────────────────────────────────────────────────────
    # MDF GUARD — prevent exploitation via small bets
    # ─────────────────────────────────────────────────────────────────────────

    def _mdf_call_thresh(self, call_t, pot, cost):
        if cost <= 0 or pot <= 0: return call_t
        bet_frac = cost / pot
        if   bet_frac < 0.15: return call_t - 0.07
        elif bet_frac < 0.30: return call_t - 0.04
        elif bet_frac < 0.50: return call_t - 0.02
        return call_t

    # ─────────────────────────────────────────────────────────────────────────
    # STRUCTURED BLUFF — conditions must stack, no pure random
    # ─────────────────────────────────────────────────────────────────────────

    def _bluff(self, street, cat_id, tex, spr):
        if street == 'preflop':           return False
        if self.opp.is_calling_station:   return False
        if spr < 1.2:                     return False
        ftr = self.opp.ema_fold_rate
        if ftr < 0.40:                    return False
        if tex in ('wet','monotone') and cat_id > 8: return False

        if cat_id in (5,6,7,8):          # semi-bluff with draws
            prob = 0.20 + 0.25*(ftr - 0.40)
            if self._auction_won: prob += 0.05
        elif cat_id in (9,10,11):
            prob = 0.10 + 0.15*(ftr - 0.40)
        elif cat_id in (12,13):
            if tex != 'dry': return False
            prob = 0.05 + 0.10*(ftr - 0.40)
        else:
            return False

        return random.random() < min(prob, 0.38)

    # ─────────────────────────────────────────────────────────────────────────
    # BET SIZING — Kelly multiplier 1.8-2.2 (was 3.5)
    # ─────────────────────────────────────────────────────────────────────────

    def _kelly(self, equity, pot_odds, tb):
        edge = equity - pot_odds
        if edge <= 0: return 0.0
        mult = 1.8 if tb < 5 else 2.2
        return min(1.0, edge * mult)

    def _size_bet(self, equity, pot, min_r, max_r, tex, tb):
        frac     = self._kelly(equity, 0.0, tb)
        tex_mult = {'wet':1.12,'semi_wet':1.04,'dry':0.92,
                    'paired':0.88,'monotone':1.08}.get(tex, 1.0)
        target   = int(pot * (0.38 + frac*0.65) * tex_mult)
        return max(min_r, min(max_r, target))

    def _size_raise(self, equity, pot_odds, pot, min_r, max_r, tb):
        frac   = self._kelly(equity, pot_odds, tb)
        target = int(min_r + frac*(max_r - min_r))
        return max(min_r, min(max_r, target))

    # ─────────────────────────────────────────────────────────────────────────
    # AUCTION BID
    # True Vickrey value when opponent is rational.
    # Drastically underbid vs maniacs — let them burn chips.
    # MC budget: 60 (equity) + 120 (info value) = 180 calls (was 475).
    # ─────────────────────────────────────────────────────────────────────────

    def _bid(self, gs, tb, cat_id, eq):
        pot      = max(1, gs.pot)
        my_chips = gs.my_chips
        hole     = [eval7.Card(s) for s in gs.my_hand]
        board    = [eval7.Card(s) for s in gs.board]

        if cat_id == 0 or eq > 0.87: return 0
        if cat_id == 13 and eq < 0.22:
            return random.randint(0, 6)

        n_s, i_p = (8, 15) if tb > 5 else (5, 10)
        info_delta = quick_info_value(hole, board, n_s, i_p)

        # Conservative pot multiplier (1.5x not 3x)
        true_val = int(info_delta * pot * 1.5)

        opp_avg = self.opp.ema_bid

        if self.opp.is_auction_maniac:
            # Let them overbid — cap at 3% of stack
            bid = min(true_val, int(my_chips * 0.03))
        else:
            # Vickrey: bid true value, capped sensibly
            # If info is worth more than opponent typically bids, bid to win
            bid = true_val if true_val > 1 else 0

        # Noise bid on medium hands
        if bid == 0 and cat_id in (3,4,5,6,7):
            bid = random.randint(1, 10)

        cap_pct = 0.03 if self.opp.is_auction_maniac else 0.07
        bid = max(0, min(bid, int(my_chips * cap_pct), my_chips))

        if bid > 4:
            bid = int(bid * random.uniform(0.96, 1.04))
        return bid

    # ─────────────────────────────────────────────────────────────────────────
    # CALLBACKS
    # ─────────────────────────────────────────────────────────────────────────

    def on_hand_start(self, gi: GameInfo, gs: PokerState):
        self.round += 1
        self._hole          = [eval7.Card(s) for s in gs.my_hand]
        self._cat           = (13, 'Air')
        self._texture       = 'dry'
        self._prev_opp_wgr  = gs.opp_wager
        self._prev_street   = 'preflop'
        self._auction_won   = False
        self._auction_lost  = False
        self._auc_detected  = False
        self._chips_pre_auc = gs.my_chips
        self._chips_post_auc = 0
        self._my_last_bid   = 0
        self._preflop_eq    = preflop_strength(self._hole[0], self._hole[1]) \
                              if len(self._hole) == 2 else 0.5

    def on_hand_end(self, gi: GameInfo, gs: PokerState):
        self._recent_pnl.append(gs.payoff)
        if len(self._recent_pnl) > 200:
            self._recent_pnl = self._recent_pnl[-200:]
        self.opp.hands += 1

        if self._chips_post_auc > 0:
            auction_cost = self._chips_pre_auc - self._chips_post_auc
            if self._auction_won:
                self.opp.observe_bid(auction_cost)
                self.opp.observe_auction(opp_won=False)
            elif self._auction_lost:
                self.opp.observe_bid(self._my_last_bid + random.randint(1, 20))
                self.opp.observe_auction(opp_won=True)
            else:
                self.opp.observe_bid(self._my_last_bid)
                self.opp.observe_auction(opp_won=False)

        if gs.opp_revealed_cards:
            opp_c = [eval7.Card(s) for s in gs.opp_revealed_cards]
            board  = [eval7.Card(s) for s in gs.board]
            if len(opp_c) >= 2 and len(board) >= 3:
                sc = eval7.evaluate(opp_c + board)
                self.opp.showdowns.append((sc, gs.opp_wager))
                if len(self.opp.showdowns) > 150:
                    self.opp.showdowns = self.opp.showdowns[-150:]

    # ─────────────────────────────────────────────────────────────────────────
    # MAIN DECISION
    # ─────────────────────────────────────────────────────────────────────────

    def get_move(self, gi: GameInfo, gs: PokerState):
        street = gs.street
        tb     = gi.time_bank
        sidx   = {'preflop':0,'flop':1,'auction':1,'turn':2,'river':3}.get(street, 0)

        # Track opponent action delta
        if street != self._prev_street:
            self._prev_opp_wgr = 0
        opp_wgr = gs.opp_wager
        if street != 'auction':
            if opp_wgr > self._prev_opp_wgr:
                if gs.cost_to_call > 0:
                    self.opp.observe_action('raise', sidx)
                else:
                    self.opp.observe_action('call', sidx)
            elif (opp_wgr == self._prev_opp_wgr and gs.cost_to_call == 0
                  and street == self._prev_street and street != 'preflop'):
                self.opp.observe_action('check', sidx)
        self._prev_opp_wgr = opp_wgr
        self._prev_street  = street

        # Classify hand + board (postflop)
        if street not in ('preflop',):
            board = [eval7.Card(s) for s in gs.board]
            if len(board) >= 3:
                self._cat     = classify_hand(self._hole, board)
                self._texture = board_texture(board)

        cat_id, _ = self._cat
        tex        = self._texture

        # ── AUCTION ──────────────────────────────────────────────────────────
        if street == 'auction':
            self._chips_pre_auc = gs.my_chips
            eq  = self._equity(gs, tb, override=60)
            bid = self._bid(gs, tb, cat_id, eq)
            self._my_last_bid = bid
            return ActionBid(bid)

        # Detect auction outcome on first post-auction call
        if not self._auc_detected and street in ('flop','turn','river'):
            self._auc_detected = True
            self._chips_post_auc = gs.my_chips
            if gs.opp_revealed_cards:
                paid = self._chips_pre_auc - gs.my_chips
                if paid != self._my_last_bid or self._my_last_bid == 0:
                    self._auction_won = True
                # else: tied — both flags stay False
            else:
                self._auction_lost = True

        # Equity
        equity = self._preflop_eq if street == 'preflop' else self._equity(gs, tb)

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

        raise_t, call_t = self._thresholds(street, cat_id)
        if cost > 0:
            call_t = self._mdf_call_thresh(call_t, pot, cost)

        # ════════════════════════════════════════════════════════════════════
        # PREFLOP
        # ════════════════════════════════════════════════════════════════════
        if street == 'preflop':
            is_sb = (gs.my_wager == 10)

            # ── All-in shove detection and special handling ───────────────────
            # When facing a near-max raise (≥70% of effective stack), use a
            # stricter equity threshold to avoid calling dominated hands.
            # Round 24 failure: called Qs9d (~37% equity) into AKs shove.
            if can_call and cost > 0:
                eff_stack = min(my_chips, opp_chips)
                shove_frac = cost / max(1, eff_stack)
                if shove_frac >= 0.70:
                    # Near or full shove — need real equity to call
                    # pot_odds = cost/(pot+cost), must beat that + margin
                    shove_call_thresh = max(pot_odds + 0.08, 0.46)
                    if equity >= shove_call_thresh:
                        return ActionCall()
                    return ActionFold()
                elif shove_frac >= 0.40:
                    # Large raise (40-70% of stack) — need to beat pot odds +5%
                    large_raise_thresh = max(pot_odds + 0.05, 0.43)
                    if equity >= large_raise_thresh:
                        return ActionCall()
                    return ActionFold()

            if is_sb:
                if equity > 0.58 and can_raise:
                    sz = int(pot * random.uniform(1.8, 2.6))
                    return ActionRaise(max(min_r, min(max_r, sz)))
                if equity > 0.42 and can_call:
                    return ActionCall()
                return ActionFold()
            else:
                if equity >= raise_t and can_raise:
                    sz = int(pot * random.uniform(1.5, 2.3))
                    return ActionRaise(max(min_r, min(max_r, sz)))
                if equity >= call_t:
                    if can_call:  return ActionCall()
                    if can_check: return ActionCheck()
                    return ActionFold()
                if can_check: return ActionCheck()
                return ActionFold()

        # ════════════════════════════════════════════════════════════════════
        # RIVER — binary outcome, simplified, polarised sizing
        # ════════════════════════════════════════════════════════════════════
        if street == 'river':
            if cost > 0:
                # Same oversized-bet protection as flop/turn
                if pot > 0:
                    overbet_frac = cost / pot
                    if overbet_frac >= 1.5:
                        call_t = max(call_t, 0.58)
                    elif overbet_frac >= 0.9:
                        call_t = max(call_t, 0.52)
                    elif overbet_frac >= 0.60:
                        call_t = max(call_t, 0.47)
                if equity >= raise_t and can_raise:
                    sz = int(pot * (0.80 if cat_id <= 2 else 0.40))
                    return ActionRaise(max(min_r, min(max_r, sz)))
                if equity >= call_t:
                    return ActionCall() if can_call else (ActionCheck() if can_check else ActionFold())
                if can_check: return ActionCheck()
                return ActionFold()
            else:
                if equity >= raise_t and can_raise:
                    sz = int(pot * (0.72 if cat_id <= 2 else 0.38))
                    return ActionRaise(max(min_r, min(max_r, sz)))
                if self._bluff(street, cat_id, tex, spr) and can_raise:
                    sz = int(pot * 0.60)
                    return ActionRaise(max(min_r, min(max_r, sz)))
                if can_check: return ActionCheck()
                if can_call:  return ActionCall()
                return ActionFold()

        # ════════════════════════════════════════════════════════════════════
        # FLOP / TURN
        # ════════════════════════════════════════════════════════════════════
        if cost > 0:
            # ── Oversized bet protection ──────────────────────────────────────
            # When opponent bets >0.9x pot, apply stricter call threshold.
            # Prevents round-33-style disaster: calling 143 into 124 pot with
            # bottom pair on an ace-high board.
            # Also prevents round-39-style: calling 1.15x pot bets with 1-pair
            # when revealed card shows opponent has strong components.
            if pot > 0:
                overbet_frac = cost / pot
                if overbet_frac >= 1.5:
                    # Pot-sized+ bet: need strong hand or draw to continue
                    call_t = max(call_t, 0.55)
                elif overbet_frac >= 0.9:
                    call_t = max(call_t, 0.48)
                elif overbet_frac >= 0.60:
                    call_t = max(call_t, 0.44)

            if equity >= raise_t and can_raise:
                ra = self._size_raise(equity, pot_odds, pot, min_r, max_r, tb)
                return ActionRaise(ra)
            if equity >= call_t:
                return ActionCall() if can_call else (ActionCheck() if can_check else ActionFold())
            if self._bluff(street, cat_id, tex, spr) and can_raise:
                ra = max(min_r, min(max_r, int(min_r + 0.22*(max_r - min_r))))
                return ActionRaise(ra)
            if can_check: return ActionCheck()
            return ActionFold()
        else:
            if equity >= raise_t and can_raise:
                ra = self._size_bet(equity, pot, min_r, max_r, tex, tb)
                return ActionRaise(ra)
            # Expanded value betting: bet medium-strong hands more proactively
            # Fixes passive play like R7 (top-two-pair checked through) and
            # R49 (pair on paired board never bet).
            # Cat 2=GoodMade (TPTK, overpair), cat 3=MedMade (top pair med kicker)
            # bet these at 50% frequency on flop, 65% on turn (more often as board runs out)
            if (cat_id in (2, 3) and equity >= 0.60 and can_raise):
                bet_freq = 0.50 if street == 'flop' else 0.65
                if random.random() < bet_freq:
                    sz = self._size_bet(equity, pot, min_r, max_r, tex, tb)
                    return ActionRaise(max(min_r, min(max_r, sz)))
            # Protection bet: medium equity, dry/semi-wet board only, reduced freq
            if (equity >= 0.55 and can_raise
                    and tex in ('dry','semi_wet')
                    and random.random() < 0.22):
                sz = int(pot * 0.26)
                return ActionRaise(max(min_r, min(max_r, sz)))
            if self._bluff(street, cat_id, tex, spr) and can_raise:
                sz = int(pot * 0.38)
                return ActionRaise(max(min_r, min(max_r, sz)))
            if can_check: return ActionCheck()
            if can_call:  return ActionCall()
            return ActionFold()


if __name__ == '__main__':
    args = parse_args()
    run_bot(Bot(), args)