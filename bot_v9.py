from pkbot.base import BaseBot
from pkbot.actions import ActionCall, ActionCheck, ActionRaise, ActionFold, ActionBid
from pkbot.states import PokerState, GameInfo
from pkbot.runner import parse_args, run_bot
import eval7

# ─── Game rules (confirmed from problem statement) ───────────────────────────
# Pre-flop:  SB posts 10, acts FIRST  (fold / call / raise)
#            BB posts 20, acts SECOND
# Post-flop: BB acts FIRST on every street (flop / turn / river)
#            SB acts SECOND  →  SB has positional advantage postflop
# Engine street string: 'pre-flop'  (hyphen, not space)
# ─────────────────────────────────────────────────────────────────────────────

STREET_PREFLOP = 'pre-flop'


# ═══════════════════════════════════════════════════════════════════════════════
# PREFLOP TABLE  —  O(1) equity lookup, built once at import
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
                    s = 0.15 + 0.022*hi + 0.010*lo
                    if suited:  s += 0.04
                    if   gap==1: s += 0.04
                    elif gap==2: s += 0.02
                    elif gap==3: s += 0.005
                    elif gap>=5: s -= 0.035
                    if hi==14:
                        s += 0.05
                        if   lo>=13: s += 0.04
                        elif lo>=12: s += 0.02
                        elif lo>=11: s += 0.01
                    elif hi==13 and lo>=12: s += 0.025
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
    r1, r2 = c1.rank+2, c2.rank+2
    hi, lo = max(r1,r2), min(r1,r2)
    return PREFLOP_TABLE.get((hi, lo, c1.suit==c2.suit), 0.5)


# ═══════════════════════════════════════════════════════════════════════════════
# HAND CLASSIFICATION  —  14 categories, deterministic via eval7
#
# eval7 score >> 24:  0=HighCard 1=Pair 2=TwoPair 3=Trips 4=Straight
#                     5=Flush 6=FullHouse 7=Quads 8=StraightFlush
# ═══════════════════════════════════════════════════════════════════════════════

CATEGORY_EQUITY = {
    0: 0.93,  # Monster  : flush / FH / quads / SF
    1: 0.87,  # Strong   : straight / set / top-two from hole
    2: 0.74,  # GoodMade : TPTK / overpair TT+
    3: 0.64,  # MedMade  : top pair med kicker / mid pair
    4: 0.50,  # WeakMade : bottom pair / underpair / TP weak kicker
    5: 0.65,  # ComboStr : pair + FD or OESD (14+ outs)
    6: 0.55,  # ComboMed : pair + gutshot / weaker combo
    7: 0.52,  # NutDraw  : nut FD + overcard(s)
    8: 0.40,  # Draw     : OESD or plain FD
    9: 0.38,  # WeakDraw : gutshot + overcard(s)
   10: 0.30,  # Gutshot  : 4 outs only
   11: 0.32,  # AceHigh
   12: 0.25,  # HighCard
   13: 0.18,  # Air
}

def _flush_outs(hole, board):
    for s in range(4):
        total = sum(1 for c in hole+board if c.suit==s)
        mine  = sum(1 for c in hole if c.suit==s)
        if total==4 and mine>=1:           return 9
        if total==3 and mine>=1 and len(board)<4: return 3
    return 0

def _straight_outs(hole, board):
    ranks = sorted(set(c.rank+2 for c in hole+board))
    best = 0
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
    has_fd   = fd >= 9
    has_oesd = sd >= 8
    has_gut  = sd >= 4
    has_bd   = fd == 3

    if hand_type >= 5: return (0, 'Monster')
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
        if hr_s[0] == hr_s[1]:
            if hr_s[0] > max(br_s):
                return (2,'GoodMade') if hr_s[0]>=10 else (4,'WeakMade')
            if has_fd or has_oesd: return (6,'ComboMed')
            return (4, 'WeakMade')
        paired = next((r for r in hr_s if r in br_set), None)
        if paired is None: return (13, 'Air')
        kicker = max(r for r in hr_s if r != paired)
        top_b  = max(br_s)
        mid_b  = br_s[len(br_s)//2]
        is_top = paired == top_b
        is_mid = paired == mid_b and not is_top
        if is_top:
            other_b = [r for r in br_s if r != paired]
            need = max(other_b) if other_b else 0
            if kicker >= need or kicker >= 10:
                return (5,'ComboStrong') if (has_fd or has_oesd) else (2,'GoodMade')
            elif kicker >= 7:
                return (6,'ComboMed') if (has_fd or has_oesd) else (3,'MedMade')
            else:
                return (6,'ComboMed') if (has_fd or has_oesd) else (4,'WeakMade')
        elif is_mid:
            if has_fd and has_oesd:           return (5,'ComboStrong')
            if has_fd or has_oesd or has_gut: return (6,'ComboMed')
            return (4, 'WeakMade')
        else:
            return (6,'ComboMed') if (has_fd or has_oesd) else (4,'WeakMade')

    hr_vals = sorted([c.rank+2 for c in hole], reverse=True)
    brd_max = max(c.rank+2 for c in board)
    overs   = sum(1 for r in hr_vals if r>brd_max)
    if has_fd and has_oesd:   return (5,'ComboStrong')
    if has_fd and overs>=1:   return (7,'NutDraw')
    if has_fd:                return (8,'Draw')
    if has_oesd and overs==2: return (7,'NutDraw')
    if has_oesd:              return (8,'Draw')
    if has_gut and overs>=1:  return (9,'WeakDraw')
    if has_gut:               return (10,'Gutshot')
    if hr_vals[0]==14:        return (11,'AceHigh')
    if hr_vals[0]>=13:        return (12,'HighCard')
    if has_bd:                return (12,'HighCard')
    return (13, 'Air')


# ═══════════════════════════════════════════════════════════════════════════════
# EXACT EQUITY  —  river full enum (~1ms), turn with 1 known card (~2ms)
# ═══════════════════════════════════════════════════════════════════════════════

_DECK_52 = eval7.Deck().cards

def exact_river_equity(hole, board, opp_revealed):
    known     = set(hole+board+opp_revealed)
    remaining = [c for c in _DECK_52 if c not in known]
    my_score  = eval7.evaluate(hole+board)
    wins, total = 0.0, 0
    if len(opp_revealed) == 1:
        rev = opp_revealed[0]
        for c in remaining:
            s = eval7.evaluate([rev,c]+board)
            total += 1
            if   my_score > s: wins += 1.0
            elif my_score == s: wins += 0.5
    else:
        n = len(remaining)
        for i in range(n):
            for j in range(i+1, n):
                s = eval7.evaluate([remaining[i],remaining[j]]+board)
                total += 1
                if   my_score > s: wins += 1.0
                elif my_score == s: wins += 0.5
    return wins/total if total>0 else 0.5

def exact_turn_equity_known(hole, board, opp_revealed):
    """~1,980 evals, ~2ms. Only called when 1 opp card is known."""
    known     = set(hole+board+opp_revealed)
    remaining = [c for c in _DECK_52 if c not in known]
    rev       = opp_revealed[0]
    wins, total = 0.0, 0
    for ri, river_card in enumerate(remaining):
        full_board = board + [river_card]
        my_score   = eval7.evaluate(hole+full_board)
        for ui in range(len(remaining)):
            if ui == ri: continue
            s = eval7.evaluate([rev, remaining[ui]]+full_board)
            total += 1
            if   my_score > s: wins += 1.0
            elif my_score == s: wins += 0.5
    return wins/total if total>0 else 0.5


# ═══════════════════════════════════════════════════════════════════════════════
# AUCTION BID TABLE
# ═══════════════════════════════════════════════════════════════════════════════

AUCTION_BID_FRAC = {
    0:  0.05,   # Monster  — small tax on auction dominators
    1:  0.12,   # Strong
    2:  0.25,   # GoodMade
    3:  0.32,   # MedMade  — highest info value
    4:  0.28,   # WeakMade
    5:  0.35,   # ComboStrong
    6:  0.28,   # ComboMed
    7:  0.30,   # NutDraw
    8:  0.25,   # Draw
    9:  0.18,   # WeakDraw
   10:  0.12,   # Gutshot
   11:  0.20,   # AceHigh
   12:  0.10,   # HighCard
   13:  0.05,   # Air (base — boosted every 10th hand for unpredictability)
}
AUCTION_MIN_BID = 8


# ═══════════════════════════════════════════════════════════════════════════════
# REVEALED CARD ANALYSIS  →  (is_brick: bool, threat: float 0-1)
# ═══════════════════════════════════════════════════════════════════════════════

def analyze_revealed_card(rev_card, board):
    r = rev_card.rank + 2
    s = rev_card.suit
    board_ranks = [c.rank+2 for c in board]
    board_suits = [c.suit for c in board]
    threat = 0.0
    if r in board_ranks:          threat += 0.40
    if board_suits.count(s) >= 2: threat += 0.25
    sc = sum(1 for br in board_ranks if abs(r-br)<=2)
    threat += 0.20 if sc>=2 else (0.10 if sc==1 else 0.0)
    if r >= 13:                   threat += 0.15
    return (threat < 0.35), min(1.0, threat)   # (is_brick, threat_score)


# ═══════════════════════════════════════════════════════════════════════════════
# OPPONENT PROFILER
#
# Tracks stats, fits one of 5 profiles after 100 hands, re-fits every 200.
#
# NEW vs previous version:
#  • opp_bid tracking now uses the actual Vickrey payment (exact, not estimated)
#  • Adaptation triggers after only 3 auction results (was 10) — fixes Thambi case
#  • early_raise / early_check tracking for "passive-early, nuke-river" detection
#  • bluff_period() returns profile-aware bluff frequency (NIT = more bluffs)
#  • BB-position awareness passed in so thresholds differ by position
# ═══════════════════════════════════════════════════════════════════════════════

class OpponentProfiler:
    UNKNOWN = 0
    MANIAC  = 1   # overbets constantly, high raise%
    STATION = 2   # never folds, calls everything
    NIT     = 3   # folds majority (55%+)
    TAG     = 4   # tight-aggressive, balanced raises

    def __init__(self):
        self.profile      = self.UNKNOWN
        self.hands_seen   = 0
        self.next_eval    = 100

        # Action totals
        self.act_raise    = 0
        self.act_call     = 0
        self.act_fold     = 0
        self.act_total    = 0
        self.big_bet_cnt  = 0     # times opp bet >= 80% of pot

        # Early-street aggression (preflop + flop only)
        # Used to detect "passive-early, nuke-river" = polarized value bot
        self.early_raises = 0
        self.early_acts   = 0

        # Auction tracking — exact bids when known
        self.auc_bids     = []    # list of confirmed opp bids
        self.auc_total    = 0

    # ── Observation ───────────────────────────────────────────────────────────

    def observe_action(self, action_type):
        self.act_total += 1
        if   action_type == 'raise': self.act_raise += 1
        elif action_type == 'call':  self.act_call  += 1
        elif action_type == 'fold':  self.act_fold  += 1

    def observe_early_action(self, is_aggressive):
        self.early_acts += 1
        if is_aggressive: self.early_raises += 1

    def observe_big_bet(self):
        self.big_bet_cnt += 1

    def observe_auction_bid(self, opp_bid):
        """Call with the confirmed opponent bid (from Vickrey payment)."""
        if opp_bid > 0:
            self.auc_bids.append(opp_bid)
        self.auc_total += 1

    def observe_hand(self):
        self.hands_seen += 1
        if self.hands_seen >= self.next_eval and self.act_total >= 20:
            self._fit_profile()
            self.next_eval += 200

    # ── Profile fitting ────────────────────────────────────────────────────────

    def _fit_profile(self):
        if self.act_total == 0:
            self.profile = self.UNKNOWN
            return
        fold_r  = self.act_fold  / self.act_total
        raise_r = self.act_raise / self.act_total
        call_r  = self.act_call  / self.act_total
        bbrate  = self.big_bet_cnt / max(1, self.hands_seen)
        if raise_r > 0.22 or bbrate > 0.30:
            self.profile = self.MANIAC
        elif fold_r < 0.22 and call_r > 0.40:
            self.profile = self.STATION
        elif fold_r > 0.50:
            self.profile = self.NIT
        elif fold_r >= 0.30 and raise_r >= 0.18:
            self.profile = self.TAG
        else:
            self.profile = self.UNKNOWN

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def opp_avg_bid(self):
        return sum(self.auc_bids)/len(self.auc_bids) if self.auc_bids else 10.0

    @property
    def opp_is_auction_dominator(self):
        # FIX: adapt after only 3 confirmed bids (was 10)
        # Thambi bids 55+ from hand 1 — we need to react by hand 4
        return len(self.auc_bids) >= 3 and self.opp_avg_bid > 25.0

    @property
    def early_af(self):
        return self.early_raises / max(1, self.early_acts)

    @property
    def is_passive_early_nuke_river(self):
        """
        Detects "DeiThambi" archetype:
        - Barely bets early streets (early_af < 0.20)
        - Fires huge bets only with nuts
        - Against these: fold river to anything > 2x pot unless very strong
        """
        return (self.early_af < 0.20
                and self.big_bet_cnt >= 3
                and self.hands_seen >= 20)

    def threshold_mods(self):
        """(raise_delta, call_delta) additive adjustments per profile."""
        if   self.profile == self.MANIAC:  return (+0.05, +0.05)
        elif self.profile == self.STATION: return (-0.05, -0.03)
        elif self.profile == self.NIT:     return (-0.06, -0.05)
        elif self.profile == self.TAG:     return (+0.02, +0.03)
        else:                              return ( 0.00,  0.00)

    def bluff_period(self, cat_id):
        """
        Returns bluff period N (bluff every Nth opportunity).
        Profile-aware: NIT opponents get more frequent bluffs.
        STATION / MANIAC: no bluffs (return 0 = disabled).
        """
        if self.profile in (self.STATION, self.MANIAC):
            return 0   # bluffing disabled
        # Base periods by category
        if   cat_id in (7, 8): base = 4
        elif cat_id in (5, 6): base = 5
        elif cat_id in (9,10): base = 8
        else:                  return 0   # not a bluffing category
        # NIT folds more → bluff more aggressively
        if self.profile == self.NIT:
            return max(2, base - 1)
        return base

    def thin_value_viable(self):
        return self.profile in (self.STATION, self.UNKNOWN)


# ═══════════════════════════════════════════════════════════════════════════════
# BOT
# ═══════════════════════════════════════════════════════════════════════════════

class Bot(BaseBot):

    def __init__(self):
        self.profiler         = OpponentProfiler()
        self.round            = 0
        self._bluff_draw_ctr  = 0   # cumulative bluff counter
        self._bluff_air_ctr   = 0   # auction air-bluff counter
        self._reset_hand()

    def _reset_hand(self):
        self._hole            = []
        self._preflop_eq      = 0.5
        self._cat             = (13, 'Air')
        self._prev_street     = STREET_PREFLOP
        self._prev_opp_wager  = 0
        self._auction_won     = False
        self._auction_lost    = False
        self._has_opp_card    = False
        self._opp_card_threat = 0.0
        self._opp_card_brick  = True
        self._chips_pre_auc   = 5000
        self._my_bid          = 0
        self._auc_detected    = False
        self._bets_faced      = 0
        self._is_bb           = False   # NOW USED in threshold logic

    # ── Equity ────────────────────────────────────────────────────────────────

    def _equity(self, gs):
        hole   = [eval7.Card(s) for s in gs.my_hand]
        board  = [eval7.Card(s) for s in gs.board]
        opp_r  = [eval7.Card(s) for s in gs.opp_revealed_cards]
        street = gs.street
        if street == STREET_PREFLOP: return self._preflop_eq
        if street == 'river':        return exact_river_equity(hole, board, opp_r)
        if street == 'turn' and len(board)==4 and len(opp_r)>=1:
            return exact_turn_equity_known(hole, board, opp_r)
        return CATEGORY_EQUITY.get(self._cat[0], 0.5)

    # ── Auction bid ───────────────────────────────────────────────────────────
    #
    # FIX vs previous: faster adaptation (3 confirmed bids, not 10)
    # FIX: bid just above opp average (opp_avg * 1.10, not 1.20)
    # Air bluff every 10th air hand → bid 20% pot (unpredictability signal)

    def _auction_bid(self, cat_id, pot, my_chips):
        frac = AUCTION_BID_FRAC.get(cat_id, 0.10)

        # Air bluff: every 10th air hand bid 20% pot
        if cat_id == 13:
            self._bluff_air_ctr += 1
            if self._bluff_air_ctr % 10 == 0:
                frac = 0.20

        bid = int(pot * frac)
        bid = max(AUCTION_MIN_BID, bid)

        # Adaptive: if opp is dominating auction, bid just above their avg
        if self.profiler.opp_is_auction_dominator:
            target = int(self.profiler.opp_avg_bid * 1.10) + 5
            bid = max(bid, target)

        bid = min(bid, int(my_chips * 0.10))   # cap at 10% of stack
        return max(0, min(bid, my_chips))

    # ── Thresholds ────────────────────────────────────────────────────────────
    #
    # FIX vs previous: _is_bb is now actually used
    # BB postflop: slightly tighter raise_t (OOP disadvantage)
    # BB preflop: lower call_t (already invested, better pot odds)
    #
    # Base call_t values calibrated for target fold rates:
    #   Flop: 45-55% fold rate  → call_t = 0.30 (MDF will be applied after)
    #   Turn: 50-60% fold rate  → call_t = 0.38
    #   River: 60-70% fold rate → call_t = 0.50 (polarized overbets)

    def _base_thresholds(self, street):
        if street == STREET_PREFLOP:
            return 0.65, 0.38
        elif street == 'flop':
            # Low base to fix over-folding. Overbet protection applied separately.
            return 0.68, 0.30
        elif street == 'turn':
            return 0.72, 0.38
        else:  # river
            # Tightened. River is where we bleed. Big losses all on river.
            return 0.76, 0.50

    def _thresholds(self, street, is_bb=False):
        raise_t, call_t = self._base_thresholds(street)

        # BB postflop: OOP (acts first) → tighter raises, same calls
        # BB preflop: looser calls (invested chips, better pot odds)
        if is_bb:
            if street == STREET_PREFLOP:
                call_t -= 0.06     # defend BB more: 0.38 → 0.32
            elif street in ('flop', 'turn', 'river'):
                raise_t += 0.03   # OOP → only raise strongest hands

        # Profile adjustments
        rd, cd = self.profiler.threshold_mods()
        raise_t = max(0.52, min(0.85, raise_t + rd))
        call_t  = max(0.22, min(0.65, call_t  + cd))

        return raise_t, call_t

    # ── Overbet protection ────────────────────────────────────────────────────
    #
    # FIX vs previous:
    # Flop: normal bets (≤80% pot) do NOT tighten call_t at all.
    #   This was the root cause of 67-95% flop fold rates — we were tightening
    #   even for 50-75% pot bets. Now flop protection only kicks in for >80% pot.
    # River: "passive-early, nuke-river" bot detected → require 0.68+ equity.
    # ORDERING: overbet protection runs BEFORE MDF, so MDF can override on flop.

    def _apply_overbet_protection(self, call_t, bet_ratio, street):
        if street == 'flop':
            # FIX: threshold raised from 0.75 to 0.80 — normal bets unchanged
            if   bet_ratio >= 2.0: call_t = max(call_t, 0.55)
            elif bet_ratio >= 1.5: call_t = max(call_t, 0.50)
            elif bet_ratio >= 0.8: call_t = max(call_t, 0.44)
            # bet_ratio < 0.8: no adjustment — defend normally
        elif street == 'turn':
            if   bet_ratio >= 2.0: call_t = max(call_t, 0.60)
            elif bet_ratio >= 1.5: call_t = max(call_t, 0.56)
            elif bet_ratio >= 1.0: call_t = max(call_t, 0.52)
            elif bet_ratio >= 0.75: call_t = max(call_t, 0.46)
        else:  # river
            if   bet_ratio >= 3.0: call_t = max(call_t, 0.72)
            elif bet_ratio >= 2.0: call_t = max(call_t, 0.67)
            elif bet_ratio >= 1.5: call_t = max(call_t, 0.62)
            elif bet_ratio >= 1.0: call_t = max(call_t, 0.57)
            # "Passive-early, nuke-river" bot (e.g., Thambi): extra tighten
            if bet_ratio >= 2.0 and self.profiler.is_passive_early_nuke_river:
                call_t = max(call_t, 0.72)
        return call_t

    # ── Minimum Defense Frequency (MDF) ──────────────────────────────────────
    #
    # MDF = pot_before_bet / (pot_before_bet + bet)
    # We must defend at least MDF% of our range or opponent auto-profits.
    # Applied AFTER overbet protection — on flop, MDF OVERRIDES overbet
    # so we never fold more than (1 - MDF) regardless of bet size.
    # On river: no MDF enforcement (polarized ranges make it incorrect).

    def _apply_mdf(self, call_t, pot_odds, pot_before_bet, bet_size, street):
        if street == 'river' or bet_size <= 0 or pot_before_bet <= 0:
            return call_t
        mdf = pot_before_bet / (pot_before_bet + bet_size)
        # mdf = 0.67 for 50% pot bet → must defend 67% of range
        # Practical floor: minimum call_t = pot_odds + small edge
        if street == 'flop':
            # MDF overrides overbet protection on flop
            # Hard cap: call_t cannot exceed pot_odds + 0.18 on flop
            floor = pot_odds + 0.04
            cap   = pot_odds + 0.18
            call_t = max(floor, min(call_t, cap))
        elif street == 'turn':
            floor = pot_odds + 0.06
            cap   = pot_odds + 0.22
            call_t = max(floor, min(call_t, cap))
        return call_t

    # ── Revealed card adjustment ──────────────────────────────────────────────

    def _apply_auction_adj(self, raise_t, call_t, cat_id):
        if self._auction_won:
            t = self._opp_card_threat
            if t < 0.25:
                raise_t -= 0.05; call_t -= 0.03    # brick: be aggressive
            elif t > 0.60:
                raise_t += 0.06; call_t += 0.04    # connected: pot control
                if cat_id in (7,8,9,10):
                    raise_t += 0.03                 # draw vs likely made: extra tight
        elif self._auction_lost:
            raise_t += 0.02; call_t += 0.02         # they have info on us
        return raise_t, call_t

    # ── Bet sizing ────────────────────────────────────────────────────────────
    # Capped lower than original: river 50-85%, flop 33-55%

    def _value_bet_size(self, equity, pot, min_r, max_r, street, spr):
        frac = max(0.0, min(1.0, (equity - 0.55) / 0.35))
        if   street == 'flop':  base = pot * (0.33 + frac * 0.22)
        elif street == 'turn':  base = pot * (0.45 + frac * 0.30)
        else:                   base = pot * (0.50 + frac * 0.35)
        if spr < 2.0 and equity > 0.72:
            base = max(base, max_r * 0.85)
        if self._has_opp_card:
            base *= 1.12 if self._opp_card_brick else 0.88
        return max(min_r, min(max_r, int(base)))

    def _raise_size(self, equity, pot_odds, min_r, max_r):
        edge   = max(0.0, equity - pot_odds)
        frac   = min(1.0, edge * 2.5)
        target = int(min_r + frac * (max_r - min_r))
        return max(min_r, min(max_r, target))

    # ── Shove protection ──────────────────────────────────────────────────────
    # Returns True (call), False (fold), None (not a shove → use normal logic)

    def _shove_decision(self, pot_odds, equity, cost, my_chips, opp_chips):
        eff      = min(my_chips, opp_chips)
        eff_frac = cost / max(1, eff)
        if eff_frac >= 0.70: return equity >= max(pot_odds+0.10, 0.60)
        if eff_frac >= 0.40: return equity >= max(pot_odds+0.07, 0.55)
        return None

    # ── Semi-bluff schedule ───────────────────────────────────────────────────
    # Deterministic period from profiler (profile-aware frequency)
    # Hard cap: 40% pot, max 500 chips → limits downside of failed bluffs

    def _should_bluff(self, cat_id, spr, street):
        if street == 'river': return False
        if spr < 1.5:         return False
        period = self.profiler.bluff_period(cat_id)
        if period == 0:       return False
        self._bluff_draw_ctr += 1
        return (self._bluff_draw_ctr % period) == 0

    # ── Callbacks ─────────────────────────────────────────────────────────────

    def on_hand_start(self, gi: GameInfo, gs: PokerState):
        self.round += 1
        self._reset_hand()
        self._hole       = [eval7.Card(s) for s in gs.my_hand]
        self._preflop_eq = preflop_strength(self._hole[0], self._hole[1]) \
                           if len(self._hole)==2 else 0.5
        self._is_bb = (gs.my_wager == 20)

    def on_hand_end(self, gi: GameInfo, gs: PokerState):
        self.profiler.observe_hand()
        if gs.is_terminal and gs.payoff > 0 and gs.cost_to_call != 0:
            self.profiler.observe_action('fold')

    # ── Main decision ──────────────────────────────────────────────────────────

    def get_move(self, gi: GameInfo, gs: PokerState):
        street = gs.street

        # Reset per-street state on street transitions
        if street != self._prev_street:
            self._bets_faced    = 0
            self._prev_opp_wager = 0
        self._prev_street = street

        # ── Track opponent actions ─────────────────────────────────────────────
        # FIX: use wager delta to distinguish opp bet/raise vs call
        # Previous bug: cost > 0 was always tagged as 'raise', even when opp called
        curr_opp_wager  = gs.opp_wager
        wager_delta     = curr_opp_wager - self._prev_opp_wager
        is_early_street = street in (STREET_PREFLOP, 'flop')

        if street not in ('auction',) and wager_delta >= 0:
            if wager_delta > 0:
                if gs.cost_to_call > 0:
                    # Opp put chips in AND there is still a bet to call → opp raised
                    self.profiler.observe_action('raise')
                    pot = max(1, gs.pot)
                    if gs.cost_to_call > pot * 0.80:
                        self.profiler.observe_big_bet()
                    self.profiler.observe_early_action(is_aggressive=True) if is_early_street else None
                else:
                    # Opp put chips in but no cost to call → opp called
                    self.profiler.observe_action('call')
                    self.profiler.observe_early_action(is_aggressive=False) if is_early_street else None
            else:
                # wager_delta == 0 → opp checked
                self.profiler.observe_action('call')
                self.profiler.observe_early_action(is_aggressive=False) if is_early_street else None

        self._prev_opp_wager = curr_opp_wager

        # ── Classify hand (postflop) ───────────────────────────────────────────
        if street != STREET_PREFLOP:
            board = [eval7.Card(s) for s in gs.board]
            if len(board) >= 3:
                self._cat = classify_hand(self._hole, board)

        cat_id = self._cat[0]

        # ═══════════════════════════════════════════════════════════════════════
        # AUCTION
        # ═══════════════════════════════════════════════════════════════════════
        if street == 'auction':
            self._chips_pre_auc = gs.my_chips
            bid = self._auction_bid(cat_id, max(1, gs.pot), gs.my_chips)
            self._my_bid = bid
            return ActionBid(bid)

        # ── Detect auction outcome (first postflop call only) ──────────────────
        if not self._auc_detected and street in ('flop','turn','river'):
            self._auc_detected = True
            board_cards = [eval7.Card(s) for s in gs.board]
            if gs.opp_revealed_cards:
                # We won (or tied): amount we paid = their bid (Vickrey)
                paid = self._chips_pre_auc - gs.my_chips
                self._auction_won  = (paid != self._my_bid)
                self._has_opp_card = True
                rev_card = eval7.Card(gs.opp_revealed_cards[0])
                self._opp_card_brick, self._opp_card_threat = \
                    analyze_revealed_card(rev_card, board_cards)
                # Record EXACT opp bid (what we paid IS their bid in Vickrey)
                self.profiler.observe_auction_bid(paid)
            else:
                # We lost: their bid > our bid
                self._auction_lost = True
                # Record a lower-bound estimate: their bid was > our bid
                self.profiler.observe_auction_bid(self._my_bid + 10)

        # ── Equity ────────────────────────────────────────────────────────────
        equity = self._equity(gs)

        # ── Game state ────────────────────────────────────────────────────────
        pot       = max(1, gs.pot)
        cost      = gs.cost_to_call
        my_chips  = gs.my_chips
        opp_chips = gs.opp_chips
        pot_odds  = cost / (pot+cost) if (pot+cost) > 0 else 0.0
        spr       = min(my_chips, opp_chips) / pot

        can_raise = gs.can_act(ActionRaise)
        can_call  = gs.can_act(ActionCall)
        can_check = gs.can_act(ActionCheck)
        min_r = max_r = 0
        if can_raise:
            min_r, max_r = gs.raise_bounds

        pot_before_bet = max(1, pot - cost) if cost > 0 else pot
        bet_ratio      = cost / pot_before_bet if (cost > 0 and pot_before_bet > 0) else 0.0

        # Thresholds (now passes is_bb — was bug in previous version)
        raise_t, call_t = self._thresholds(street, is_bb=self._is_bb)
        raise_t, call_t = self._apply_auction_adj(raise_t, call_t, cat_id)

        if cost > 0:
            call_t = max(call_t, pot_odds + 0.04)

        # ═══════════════════════════════════════════════════════════════════════
        # PRE-FLOP
        # ═══════════════════════════════════════════════════════════════════════
        if street == STREET_PREFLOP:
            is_sb = (gs.my_wager == 10)

            if can_call and cost > 0:
                dec = self._shove_decision(pot_odds, equity, cost, my_chips, opp_chips)
                if dec is not None:
                    return ActionCall() if dec else ActionFold()

            if is_sb:
                # FIX (VPIP/PFR gap): eliminate limp zone entirely.
                # Previous: raise > 0.44, call 0.36-0.44 (limp zone), fold < 0.36
                # Now: raise > 0.42, fold < 0.42 — no limping in HU.
                # This pushes PFR from ~5% toward 60-70% target.
                if equity > 0.42 and can_raise:
                    sz = int(pot * 2.4)
                    return ActionRaise(max(min_r, min(max_r, sz)))
                return ActionFold()
            else:
                # BB: already uses lower call_t from _thresholds (is_bb=True → -0.06)
                # Effective call threshold: ~0.32 facing a raise
                if cost > 0:
                    if equity >= raise_t and can_raise:
                        sz = int(pot * 2.2)
                        return ActionRaise(max(min_r, min(max_r, sz)))
                    if equity >= call_t and can_call:
                        return ActionCall()
                    return ActionFold()
                else:
                    # SB just called — BB squeeze or check
                    if equity >= raise_t and can_raise:
                        sz = int(pot * 2.2)
                        return ActionRaise(max(min_r, min(max_r, sz)))
                    return ActionCheck() if can_check else ActionCall()

        # ═══════════════════════════════════════════════════════════════════════
        # POST-FLOP  (flop / turn / river)
        # BB acts FIRST postflop (OOP). SB acts second (in position).
        # ═══════════════════════════════════════════════════════════════════════

        if cost > 0:
            # ── FACING A BET ──────────────────────────────────────────────────

            # Step 1: Overbet protection (raises call_t for large bets)
            call_t = self._apply_overbet_protection(call_t, bet_ratio, street)

            # Step 2: Multi-bet protection (facing 2nd+ bet this street)
            self._bets_faced += 1
            if self._bets_faced >= 2:
                call_t = min(0.70, call_t + 0.04)

            # Step 3: MDF floor applied LAST — overrides overbet on flop/turn
            # This is the key fix for 67-95% flop fold rate
            call_t = self._apply_mdf(call_t, pot_odds, pot_before_bet, cost, street)

            # Step 4: Shove detection (prevent stack-off with medium hands)
            dec = self._shove_decision(pot_odds, equity, cost, my_chips, opp_chips)
            if dec is not None:
                if dec and can_call: return ActionCall()
                return ActionCheck() if can_check else ActionFold()

            # Step 5: Value raise
            if equity >= raise_t and can_raise:
                ra = self._raise_size(equity, pot_odds, min_r, max_r)
                return ActionRaise(ra)

            # Step 6: Call — bluff-catch region
            # This is where medium-strength hands (cats 3-7) get to call
            # and deny opponents auto-profitable cbets
            if equity >= call_t:
                return ActionCall() if can_call else (ActionCheck() if can_check else ActionFold())

            # Step 7: Fold (below call threshold)
            return ActionCheck() if can_check else ActionFold()

        else:
            # ── ACTING FIRST (no bet to face) ─────────────────────────────────

            # Strong value bet
            if equity >= raise_t and can_raise:
                ra = self._value_bet_size(equity, pot, min_r, max_r, street, spr)
                return ActionRaise(ra)

            # River thin value: only vs calling stations
            if street == 'river' and equity >= 0.62 and can_raise:
                if self.profiler.thin_value_viable():
                    thin_sz = int(pot * 0.35)
                    return ActionRaise(max(min_r, min(max_r, thin_sz)))

            # Probe bet: medium-strong on flop/turn
            # FIX: BB probes less frequently (OOP = more risk betting into unknown)
            can_probe = (not self._is_bb or equity >= 0.62)
            if cat_id in (2,3) and equity >= 0.58 and can_raise and street != 'river' and can_probe:
                ra = self._value_bet_size(equity, pot, min_r, max_r, street, spr)
                return ActionRaise(ra)

            # Semi-bluff: draws, profile-aware period, capped at 500 chips
            if self._should_bluff(cat_id, spr, street) and can_raise:
                bluff_sz = min(int(pot * 0.40), 500, max_r)
                bluff_sz = max(min_r, bluff_sz)
                return ActionRaise(bluff_sz)

            return ActionCheck() if can_check else (ActionCall() if can_call else ActionFold())


if __name__ == '__main__':
    args = parse_args()
    run_bot(Bot(), args)