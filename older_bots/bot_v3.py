from pkbot.base import BaseBot
from pkbot.actions import ActionCall, ActionCheck, ActionRaise, ActionFold, ActionBid
from pkbot.states import PokerState, GameInfo
from pkbot.runner import parse_args, run_bot
import eval7
import random

# ═══════════════════════════════════════════════════════════════════════════════
# PREFLOP LOOKUP TABLE  (O(1) at runtime, built once at import)
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
                    if suited:   s += 0.04
                    if   gap==1: s += 0.04
                    elif gap==2: s += 0.02
                    elif gap==3: s += 0.005
                    elif gap>=5: s -= 0.035
                    if hi == 14:
                        s += 0.05
                        if   lo>=13: s += 0.04
                        elif lo>=12: s += 0.02
                        elif lo>=11: s += 0.01
                    elif hi==13 and lo>=12: s += 0.025
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
    r1, r2 = c1.rank+2, c2.rank+2
    hi, lo = max(r1,r2), min(r1,r2)
    return PREFLOP_TABLE.get((hi, lo, c1.suit==c2.suit), 0.5)


# ═══════════════════════════════════════════════════════════════════════════════
# HAND CLASSIFICATION  (eval7 C evaluator — fast, no MC needed)
#
# eval7.evaluate() >> 24 encoding:
#   0=HighCard  1=Pair  2=TwoPair  3=Trips  4=Straight
#   5=Flush  6=FullHouse  7=Quads  8=StraightFlush
#
# 14 buckets (descending strength):
#   0  Monster     : Flush / Full house / Quads / Str flush
#   1  Strong      : Set / Top-two pair / Nut straight
#   2  GoodMade    : Overpair (TT+) / TPTK / good two pair
#   3  MedMade     : Top pair med kicker / mid pair
#   4  WeakMade    : TP weak kicker / bottom pair / underpair
#   5  ComboStrong : Pair + FD or OESD (15+ outs)
#   6  ComboMed    : Pair + gutshot / weaker combos (10-14 outs)
#   7  NutDraw     : Nut FD / OESD + overcards (~12 outs)
#   8  Draw        : OESD / FD (~8-9 outs)
#   9  WeakDraw    : Gutshot + overcards (~6 outs)
#  10  Gutshot     : Gutshot only (~4 outs)
#  11  AceHigh     : Ace high
#  12  HighCard    : King/Queen high
#  13  Air         : Complete miss
# ═══════════════════════════════════════════════════════════════════════════════

CATEGORY_HINT = {
    0:0.93, 1:0.87, 2:0.75, 3:0.62, 4:0.50,
    5:0.65, 6:0.54, 7:0.49, 8:0.41, 9:0.36,
    10:0.29, 11:0.32, 12:0.25, 13:0.18,
}

AUCTION_BID_PCT = {
    0:  0.00,   # Monster: winning anyway, zero info value
    1:  0.15,   # Strong: confirms lead, suppresses bluffs
    2:  0.20,   # GoodMade: close decision zone, high info value
    3:  0.18,   # MedMade: need to know if we're beaten
    4:  0.12,   # WeakMade: lower — may give up on bad news
    5:  0.25,   # ComboStrong: pair+draw, need info to know play style
    6:  0.20,   # ComboMed: similar
    7:  0.22,   # NutDraw: knowing their hand changes pot odds decision
    8:  0.18,   # Draw: moderate info value
    9:  0.08,   # WeakDraw: likely folding if we miss
    10: 0.08,   # Gutshot: same
    11: 0.05,   # AceHigh: marginal
    12: 0.05,   # HighCard: marginal
    13: 0.00,   # Air: folding anyway
}


def _flush_outs(hole, board):
    for s in range(4):
        total = sum(1 for c in hole+board if c.suit==s)
        mine  = sum(1 for c in hole       if c.suit==s)
        if total==4 and mine>=1: return 9
        if total==3 and mine>=1 and len(board)<4: return 3
    return 0

def _straight_outs(hole, board):
    ranks = sorted(set(c.rank+2 for c in hole+board))
    best  = 0
    for low in range(2, 11):
        if len(set(range(low, low+5)) - set(ranks)) == 1:
            best = max(best, 4)
    for low in range(2, 11):
        seg = sorted(r for r in ranks if low<=r<=low+3)
        for i in range(len(seg)-3):
            if seg[i+3]-seg[i] == 3:
                best = max(best, 8)
    return best

def classify_hand(hole, board):
    if len(board) < 3: return (13, 'Air')

    score     = eval7.evaluate(hole+board)
    hand_type = score >> 24

    fd = _flush_outs(hole, board)
    sd = _straight_outs(hole, board)
    has_fd   = (fd >= 9)
    has_bd   = (fd == 3)
    has_oesd = (sd >= 8)
    has_gut  = (sd >= 4)

    # eval7: 0=HighCard 1=Pair 2=TwoPair 3=Trips 4=Straight 5=Flush 6=FH 7=Quads 8=SF
    if hand_type >= 6: return (0, 'Monster')   # Full House / Quads / Straight Flush
    if hand_type == 5: return (0, 'Monster')   # Flush
    if hand_type == 4: return (1, 'Strong')    # Straight

    if hand_type == 3:  # Trips
        br = [c.rank for c in board]
        for hr in [c.rank for c in hole]:
            if br.count(hr) >= 2: return (1, 'Strong')  # set
        return (3, 'MedMade')  # trips from board pair

    if hand_type == 2:  # Two Pair
        br = [c.rank for c in board]
        hr = [c.rank for c in hole]
        from_hole = sum(1 for r in hr if r in br)
        if from_hole == 2:
            return (1,'Strong') if max(hr)==max(br) else (2,'GoodMade')
        return (3, 'MedMade')

    if hand_type == 1:  # One Pair
        br_s   = sorted([c.rank for c in board], reverse=True)
        hr_s   = sorted([c.rank for c in hole],  reverse=True)
        br_set = set(br_s)

        if hr_s[0] == hr_s[1]:  # pocket pair
            if hr_s[0] > max(br_s):
                return (2,'GoodMade') if hr_s[0]>=10 else (4,'WeakMade')
            if has_fd or has_oesd: return (6,'ComboMed')
            return (4, 'WeakMade')

        paired = next((r for r in hr_s if r in br_set), None)
        if paired is None: return (13, 'Air')

        kicker = max(r for r in hr_s if r!=paired)
        top_b  = max(br_s)
        mid_b  = br_s[len(br_s)//2]
        is_top = (paired == top_b)
        is_mid = (paired == mid_b and not is_top)

        if is_top:
            other_b = [r for r in br_s if r!=paired]
            need    = max(other_b) if other_b else 0
            if kicker>=need or kicker>=10:
                if has_fd or has_oesd: return (5,'ComboStrong')
                return (2, 'GoodMade')
            elif kicker>=7:
                if has_fd or has_oesd: return (6,'ComboMed')
                return (3, 'MedMade')
            else:
                if has_fd or has_oesd: return (6,'ComboMed')
                return (4, 'WeakMade')
        elif is_mid:
            if has_fd and has_oesd:            return (5,'ComboStrong')
            if has_fd or has_oesd or has_gut:  return (6,'ComboMed')
            return (4, 'WeakMade')
        else:
            if has_fd or has_oesd: return (6,'ComboMed')
            return (4, 'WeakMade')

    # hand_type == 0: High card — draw territory
    hr_vals = sorted([c.rank+2 for c in hole], reverse=True)
    brd_max = max(c.rank+2 for c in board)
    overs   = sum(1 for r in hr_vals if r>brd_max)

    if has_fd and has_oesd:     return (5,'ComboStrong')
    if has_fd and overs>=1:     return (7,'NutDraw')
    if has_fd:                  return (8,'Draw')
    if has_oesd and overs==2:   return (7,'NutDraw')
    if has_oesd:                return (8,'Draw')
    if has_gut and overs>=1:    return (9,'WeakDraw')
    if has_gut:                 return (10,'Gutshot')
    if hr_vals[0]==14:          return (11,'AceHigh')
    if hr_vals[0]>=13:          return (12,'HighCard')
    if has_bd:                  return (12,'HighCard')
    return (13, 'Air')


# ═══════════════════════════════════════════════════════════════════════════════
# MONTE CARLO EQUITY
# Bayesian range-weighted sampling when one opp card is known.
# ═══════════════════════════════════════════════════════════════════════════════

def calc_equity(hole, board, opp_revealed, iters):
    known = set(hole+board+opp_revealed)
    deck  = [c for c in eval7.Deck().cards if c not in known]

    need_opp   = 2 - len(opp_revealed)
    need_board = 5 - len(board)
    if need_opp+need_board > len(deck): return 0.5

    weights = None
    if len(opp_revealed)==1 and need_opp==1:
        rev_r = opp_revealed[0].rank+2
        w = []
        for c in deck:
            cr = c.rank+2
            hi, lo = max(rev_r,cr), min(rev_r,cr)
            w.append(PREFLOP_TABLE.get((hi,lo,opp_revealed[0].suit==c.suit), 0.3))
        tot = sum(w)
        if tot > 0: weights = [x/tot for x in w]

    wins, valid = 0.0, 0
    for _ in range(iters):
        try:
            if weights and need_opp==1:
                unk  = random.choices(deck, weights=weights, k=1)
                rest = [c for c in deck if c not in unk]
                samp = unk + random.sample(rest, need_board)
            else:
                samp = random.sample(deck, need_opp+need_board)
        except (ValueError, IndexError):
            break
        opp_hole    = opp_revealed + samp[:need_opp]
        final_board = board        + samp[need_opp:]
        my_s  = eval7.evaluate(hole+final_board)
        opp_s = eval7.evaluate(opp_hole+final_board)
        valid += 1
        if   my_s > opp_s: wins += 1.0
        elif my_s == opp_s: wins += 0.5

    return wins/valid if valid>0 else 0.5


# ═══════════════════════════════════════════════════════════════════════════════
# OPPONENT MODEL  (lightweight EMA — no complex profiles)
# Alpha = 0.09 → ~11 hand window
# ═══════════════════════════════════════════════════════════════════════════════

class OppModel:
    A = 0.09

    def __init__(self):
        self.hands        = 0
        self.ema_agg      = 0.30
        self.ema_fold     = 0.38
        self.ema_vpip     = 0.60
        self.ema_bid      = 8.0
        self.opp_won_aucs = 0
        self.total_aucs   = 0

    def _ema(self, old, obs): return (1-self.A)*old + self.A*obs

    def see_action(self, atype, sidx):
        self.ema_agg  = self._ema(self.ema_agg,  1.0 if atype=='raise' else 0.0)
        self.ema_fold = self._ema(self.ema_fold,  1.0 if atype=='fold'  else 0.0)
        if sidx == 0:
            self.ema_vpip = self._ema(self.ema_vpip, 0.0 if atype=='fold' else 1.0)

    def see_bid(self, est):
        if est > 0:
            self.ema_bid = self._ema(self.ema_bid, float(est))

    def see_auction(self, opp_won):
        self.total_aucs += 1
        if opp_won: self.opp_won_aucs += 1

    @property
    def is_calling_station(self):
        return self.ema_fold < 0.20 and self.ema_vpip > 0.72

    @property
    def is_passive(self):
        return self.ema_agg < 0.18


# ═══════════════════════════════════════════════════════════════════════════════
# BOT
# ═══════════════════════════════════════════════════════════════════════════════

class Bot(BaseBot):

    def __init__(self):
        self.opp   = OppModel()
        self.round = 0

        self._hole           = []
        self._preflop_eq     = 0.5
        self._cat            = (13, 'Air')
        self._prev_opp_wgr   = 0
        self._prev_street    = 'preflop'
        self._auction_won    = False
        self._auction_lost   = False
        self._chips_pre_auc  = 5000
        self._chips_post_auc = 0
        self._my_last_bid    = 0
        self._auc_detected   = False

    def _iters(self, tb, street, cat_id):
        if street == 'preflop': return 0
        if tb < 3.0: return 6

        if   tb > 16: base = 100
        elif tb > 12: base = 75
        elif tb > 8:  base = 55
        elif tb > 5:  base = 35
        elif tb > 3:  base = 18
        else:         base = 10

        ceiling = {0:15, 1:45, 13:8, 12:12, 11:15}
        base = min(base, ceiling.get(cat_id, base))
        if street == 'river': base = int(base * 0.75)
        return max(6, base)

    def _equity(self, gs, tb, override=None):
        hole   = [eval7.Card(s) for s in gs.my_hand]
        board  = [eval7.Card(s) for s in gs.board]
        opp_r  = [eval7.Card(s) for s in gs.opp_revealed_cards]
        street = gs.street
        cat_id = self._cat[0]

        if street == 'preflop' and not opp_r:
            return preflop_strength(hole[0], hole[1]) if len(hole)==2 else 0.5

        n = override if override is not None else self._iters(tb, street, cat_id)
        if n < 6: return CATEGORY_HINT[cat_id]
        return calc_equity(hole, board, opp_r, n)

    def _compute_bid(self, gs, cat_id):
        pot      = max(1, gs.pot)
        my_chips = gs.my_chips

        if cat_id == 13 or self._equity(gs, 0, override=0) < 0.22:
            return random.randint(0, 3)
        if cat_id == 0:
            return 0

        pct      = AUCTION_BID_PCT.get(cat_id, 0.10)
        base_bid = int(pot * pct)

        opp_avg = self.opp.ema_bid
        if opp_avg > base_bid:
            adaptive = int(opp_avg * 1.12 + 2)
            ceiling  = int(pot * (pct + 0.10))
            if adaptive <= ceiling:
                base_bid = adaptive

        cap = int(my_chips * 0.06)
        bid = max(0, min(base_bid, cap, my_chips))

        if bid > 3:
            bid = int(bid * random.uniform(0.92, 1.08))
            bid = max(0, min(bid, cap, my_chips))

        return bid

    def _thresholds(self, street, cat_id):
        ftr = self.opp.ema_fold
        agg = self.opp.ema_agg

        raise_t = 0.68
        call_t  = 0.40

        fold_adj = min(0.10, 0.15 * max(0, ftr - 0.38))
        raise_t -= fold_adj
        call_t  -= fold_adj * 0.5

        agg_adj = min(0.08, 0.14 * max(0, agg - 0.42))
        raise_t += agg_adj
        call_t  += agg_adj * 0.6

        if self.opp.is_calling_station:
            raise_t -= 0.04
            call_t  -= 0.03

        if street == 'river':
            raise_t += 0.02
            call_t  += 0.02

        return max(0.50, min(0.82, raise_t)), max(0.28, min(0.58, call_t))

    def _size_bet(self, equity, pot, min_r, max_r):
        edge   = max(0.0, equity - 0.50)
        frac   = min(1.0, edge * 2.5)
        target = int(pot * (0.40 + frac * 0.65))
        return max(min_r, min(max_r, target))

    def _size_raise(self, equity, pot_odds, pot, min_r, max_r):
        edge   = max(0.0, equity - pot_odds)
        frac   = min(1.0, edge * 2.5)
        target = int(min_r + frac * (max_r - min_r))
        return max(min_r, min(max_r, target))

    def _should_bluff(self, street, cat_id, spr):
        if street == 'preflop':           return False
        if self.opp.is_calling_station:   return False
        if spr < 1.2:                     return False
        ftr = self.opp.ema_fold
        if ftr < 0.38:                    return False

        if cat_id in (5,6,7,8):
            prob = 0.18 + 0.22*(ftr - 0.38)
            if self._auction_won: prob += 0.05
        elif cat_id in (9,10,11):
            prob = 0.08 + 0.12*(ftr - 0.38)
        elif cat_id in (12,13):
            prob = 0.04 + 0.06*(ftr - 0.38)
        else:
            return False

        return random.random() < min(prob, 0.35)

    def on_hand_start(self, gi: GameInfo, gs: PokerState):
        self.round += 1
        self._hole           = [eval7.Card(s) for s in gs.my_hand]
        self._cat            = (13, 'Air')
        self._prev_opp_wgr   = gs.opp_wager
        self._prev_street    = 'preflop'
        self._auction_won    = False
        self._auction_lost   = False
        self._auc_detected   = False
        self._chips_pre_auc  = gs.my_chips
        self._chips_post_auc = 0
        self._my_last_bid    = 0
        self._preflop_eq     = preflop_strength(self._hole[0], self._hole[1]) \
                               if len(self._hole) == 2 else 0.5

    def on_hand_end(self, gi: GameInfo, gs: PokerState):
        self.opp.hands += 1

        if gs.is_terminal and gs.cost_to_call != 0 and gs.payoff > 0:
            sidx = {'preflop':0,'flop':1,'turn':2,'river':3}.get(gs.street, 0)
            self.opp.see_action('fold', sidx)

        if self._chips_post_auc > 0:
            auction_cost = self._chips_pre_auc - self._chips_post_auc
            if self._auction_won:
                self.opp.see_bid(auction_cost)
                self.opp.see_auction(opp_won=False)
            elif self._auction_lost:
                self.opp.see_bid(self._my_last_bid + random.randint(1, 15))
                self.opp.see_auction(opp_won=True)
            else:
                self.opp.see_bid(self._my_last_bid)
                self.opp.see_auction(opp_won=False)

    def get_move(self, gi: GameInfo, gs: PokerState):
        street = gs.street
        tb     = gi.time_bank
        sidx   = {'preflop':0,'flop':1,'auction':1,'turn':2,'river':3}.get(street, 0)

        if street != self._prev_street:
            self._prev_opp_wgr = 0
        opp_wgr = gs.opp_wager
        if street != 'auction':
            if opp_wgr > self._prev_opp_wgr:
                self.opp.see_action('raise' if gs.cost_to_call > 0 else 'call', sidx)
            elif (opp_wgr == self._prev_opp_wgr and gs.cost_to_call == 0
                  and street == self._prev_street and street != 'preflop'):
                self.opp.see_action('check', sidx)
        self._prev_opp_wgr = opp_wgr
        self._prev_street  = street

        if street != 'preflop':
            board = [eval7.Card(s) for s in gs.board]
            if len(board) >= 3:
                self._cat = classify_hand(self._hole, board)

        cat_id, _ = self._cat

        # ── AUCTION ───────────────────────────────────────────────────────────
        if street == 'auction':
            self._chips_pre_auc = gs.my_chips
            bid = self._compute_bid(gs, cat_id)
            self._my_last_bid = bid
            return ActionBid(bid)

        if not self._auc_detected and street in ('flop', 'turn', 'river'):
            self._auc_detected   = True
            self._chips_post_auc = gs.my_chips
            if gs.opp_revealed_cards:
                paid = self._chips_pre_auc - gs.my_chips
                if paid > 0 and paid != self._my_last_bid:
                    self._auction_won = True
                elif self._my_last_bid == 0:
                    self._auction_won = True
            else:
                self._auction_lost = True

        equity = self._preflop_eq if street == 'preflop' else self._equity(gs, tb)

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

        if cost > 0 and pot > 0:
            overbet = cost / pot
            if   overbet >= 1.5: call_t = max(call_t, 0.55)
            elif overbet >= 0.9: call_t = max(call_t, 0.48)
            elif overbet >= 0.6: call_t = max(call_t, 0.43)

        # ══════════════════════════════════════════════════════════════════════
        # PREFLOP
        # ══════════════════════════════════════════════════════════════════════
        if street == 'preflop':
            is_sb = (gs.my_wager == 10)

            if can_call and cost > 0:
                eff   = min(my_chips, opp_chips)
                sfrac = cost / max(1, eff)
                if sfrac >= 0.70:
                    thresh = max(pot_odds + 0.08, 0.46)
                    return ActionCall() if equity >= thresh else ActionFold()
                elif sfrac >= 0.40:
                    thresh = max(pot_odds + 0.05, 0.43)
                    return ActionCall() if equity >= thresh else ActionFold()

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

        # ══════════════════════════════════════════════════════════════════════
        # POSTFLOP
        # ══════════════════════════════════════════════════════════════════════
        if cost > 0:
            if equity >= raise_t and can_raise:
                ra = self._size_raise(equity, pot_odds, pot, min_r, max_r)
                return ActionRaise(ra)
            if equity >= call_t:
                return ActionCall() if can_call else (ActionCheck() if can_check else ActionFold())
            if self._should_bluff(street, cat_id, spr) and can_raise:
                ra = max(min_r, min(max_r, int(min_r + 0.20*(max_r - min_r))))
                return ActionRaise(ra)
            if can_check: return ActionCheck()
            return ActionFold()

        else:
            if equity >= raise_t and can_raise:
                ra = self._size_bet(equity, pot, min_r, max_r)
                return ActionRaise(ra)
            if cat_id in (2, 3) and equity >= 0.58 and can_raise:
                freq = 0.50 if street == 'flop' else 0.65
                if random.random() < freq:
                    sz = self._size_bet(equity, pot, min_r, max_r)
                    return ActionRaise(max(min_r, min(max_r, sz)))
            if equity >= 0.54 and can_raise and random.random() < 0.20:
                sz = int(pot * 0.28)
                return ActionRaise(max(min_r, min(max_r, sz)))
            if self._should_bluff(street, cat_id, spr) and can_raise:
                sz = int(pot * 0.38)
                return ActionRaise(max(min_r, min(max_r, sz)))
            if can_check: return ActionCheck()
            if can_call:  return ActionCall()
            return ActionFold()


if __name__ == '__main__':
    args = parse_args()
    run_bot(Bot(), args)
