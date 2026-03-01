from pkbot.base import BaseBot
from pkbot.actions import ActionCall, ActionCheck, ActionRaise, ActionFold, ActionBid
from pkbot.states import PokerState, GameInfo
from pkbot.runner import parse_args, run_bot
import eval7

# ── Rules clarification (from problem statement) ──────────────────────────────
# Pre-flop:  SB acts FIRST (posts 10, then decides fold/call/raise)
#            BB acts SECOND (can check, raise, or fold if SB raised)
# Post-flop: BB acts FIRST on every street (flop/turn/river)
#            SB acts SECOND
# This is OPPOSITE to standard Hold'em position convention.
# Engine uses 'pre-flop' (with hyphen) for the preflop street string.
# ─────────────────────────────────────────────────────────────────────────────

STREET_PREFLOP = 'pre-flop'


# ═══════════════════════════════════════════════════════════════════════════════
# PREFLOP TABLE  —  O(1) equity lookup
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
                    if suited: s += 0.04
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
# HAND CLASSIFICATION  —  14 categories, eval7 C evaluator (no MC)
#
# eval7 hand_type encoding (score >> 24):
#   0=HighCard  1=Pair  2=TwoPair  3=Trips  4=Straight
#   5=Flush  6=FullHouse  7=Quads  8=StraightFlush
# ═══════════════════════════════════════════════════════════════════════════════

CATEGORY_EQUITY = {
    0: 0.93,  # Monster: flush/FH/quads/SF
    1: 0.87,  # Strong: set/nut straight/top-two
    2: 0.74,  # GoodMade: TPTK / overpair TT+
    3: 0.64,  # MedMade: top pair med kicker / mid pair
    4: 0.50,  # WeakMade: bottom pair / underpair / TP weak kicker
    5: 0.65,  # ComboStrong: pair + FD or OESD (14+ outs)
    6: 0.55,  # ComboMed: pair + gutshot / weaker combo
    7: 0.52,  # NutDraw: nut FD + overcard(s)
    8: 0.40,  # Draw: OESD or plain FD
    9: 0.38,  # WeakDraw: gutshot + overcard(s)
    10: 0.30, # Gutshot only
    11: 0.32, # AceHigh
    12: 0.25, # HighCard
    13: 0.18, # Air
}

def _flush_outs(hole, board):
    for s in range(4):
        total = sum(1 for c in hole+board if c.suit==s)
        mine  = sum(1 for c in hole if c.suit==s)
        if total==4 and mine>=1: return 9
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
            if has_fd and has_oesd:            return (5,'ComboStrong')
            if has_fd or has_oesd or has_gut:  return (6,'ComboMed')
            return (4, 'WeakMade')
        else:
            return (6,'ComboMed') if (has_fd or has_oesd) else (4,'WeakMade')

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
# EXACT EQUITY (river + turn enumeration)
# River: exact enumeration of ~990 combos (~1ms)
# Turn with known opp card: ~1,980 evals (~2ms) — EXACT
# Turn without known card: use category equity (avoid 45ms enumeration)
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
            opp_score = eval7.evaluate([rev,c]+board)
            total += 1
            if   my_score > opp_score: wins += 1.0
            elif my_score == opp_score: wins += 0.5
    else:
        n = len(remaining)
        for i in range(n):
            for j in range(i+1, n):
                opp_score = eval7.evaluate([remaining[i],remaining[j]]+board)
                total += 1
                if   my_score > opp_score: wins += 1.0
                elif my_score == opp_score: wins += 0.5

    return wins/total if total>0 else 0.5


def exact_turn_equity_known(hole, board, opp_revealed):
    """Turn equity when 1 opp card is known: ~1,980 evals ~2ms."""
    known     = set(hole+board+opp_revealed)
    remaining = [c for c in _DECK_52 if c not in known]
    rev       = opp_revealed[0]
    wins, total = 0.0, 0
    for ri, river_card in enumerate(remaining):
        full_board = board + [river_card]
        my_score   = eval7.evaluate(hole+full_board)
        for ui in range(len(remaining)):
            if ui == ri: continue
            unk = remaining[ui]
            opp_score = eval7.evaluate([rev,unk]+full_board)
            total += 1
            if   my_score > opp_score: wins += 1.0
            elif my_score == opp_score: wins += 0.5
    return wins/total if total>0 else 0.5


# ═══════════════════════════════════════════════════════════════════════════════
# AUCTION BID TABLE
#
# Based on match analysis:
# - Thambi bids 55 avg → we bid 10, won only 2.3% — need higher bids
# - AllInOrFold bids 22 avg → we lost 65% of auctions
# - NPC48 bids 1.2 → we win 99.9% easily
# - Ajayendra bids 5.7 → we win 76% (our 11 chip bids work here)
#
# Strategy: bid higher on medium/draw hands (most info value).
# Air bids: a small but non-trivial base + bluff counter (every 10th hand
# we bid 25% pot on air to throw off opponent — cheap bluff signal).
# Monster: 5% to tax high bidders (they pay our bid when they win).
# Floor: 8 chips always (tax on auction dominators).
# ═══════════════════════════════════════════════════════════════════════════════

AUCTION_BID_FRAC = {
    0:  0.05,   # Monster — small tax on auction dominators
    1:  0.12,   # Strong
    2:  0.25,   # GoodMade
    3:  0.32,   # MedMade — highest info value
    4:  0.28,   # WeakMade
    5:  0.35,   # ComboStrong — pair+draw, critical info
    6:  0.28,   # ComboMed
    7:  0.30,   # NutDraw
    8:  0.25,   # Draw
    9:  0.18,   # WeakDraw
    10: 0.12,   # Gutshot
    11: 0.20,   # AceHigh
    12: 0.10,   # HighCard
    13: 0.05,   # Air — base
}

AUCTION_MIN_BID = 8  # always bid at least this (taxes auction dominators)


# ═══════════════════════════════════════════════════════════════════════════════
# REVEALED CARD ANALYSIS  —  binary + weighted
#
# Returns (connects: bool, threat: float 0-1)
# "connects" = their card hits the board meaningfully
# "threat"   = how dangerous is it (0=brick, 1=extremely connected)
# ═══════════════════════════════════════════════════════════════════════════════

def analyze_revealed_card(rev_card, board):
    r = rev_card.rank + 2
    s = rev_card.suit
    board_ranks = [c.rank+2 for c in board]
    board_suits = [c.suit for c in board]

    threat = 0.0
    if r in board_ranks:
        threat += 0.40   # pairs the board
    suit_count = board_suits.count(s)
    if suit_count >= 2:
        threat += 0.25   # flush draw component
    straight_connects = sum(1 for br in board_ranks if abs(r-br) <= 2)
    if straight_connects >= 2:
        threat += 0.20
    elif straight_connects == 1:
        threat += 0.10
    if r >= 13:
        threat += 0.15   # high card

    connects = threat >= 0.35
    return connects, min(1.0, threat)


# ═══════════════════════════════════════════════════════════════════════════════
# OPPONENT PROFILING  —  primitive, 3-bucket system
#
# Observe for first 100 hands, fit profile, play accordingly for remaining 900.
# Buckets:
#   0 = Unknown (default, use conservative static thresholds)
#   1 = AllIn/Maniac: VPIP>60%, PFR>15%, or bets pot-size overbets constantly
#       → never bluff, only value-bet top hands, fold to overbets
#   2 = CallingStation: fold_rate<30%, call_rate>55%
#       → bet thin value every street, never bluff
#   3 = NitFolder: fold_rate>55%
#       → bluff more, bet smaller to keep them in
#   4 = TAG (Tight Aggressive): fold_rate 35-55%, raise_rate>20%
#       → respect raises, play straightforward, bluff draws
#
# Profiles are re-evaluated every 200 hands to catch style changes.
# ═══════════════════════════════════════════════════════════════════════════════

class OpponentProfiler:
    UNKNOWN    = 0
    MANIAC     = 1
    STATION    = 2
    NIT        = 3
    TAG        = 4

    def __init__(self):
        self.reset_counters()
        self.profile    = self.UNKNOWN
        self.hands_seen = 0
        self.next_eval  = 100   # first profile fit after 100 hands

        # Running fold/call/raise counts (never reset, for re-evaluation)
        self.total_raise  = 0
        self.total_call   = 0
        self.total_fold   = 0
        self.total_action = 0
        self.big_bets     = 0   # times opp bet >= pot

    def reset_counters(self):
        pass   # counters are cumulative, no reset

    def observe_action(self, action_type):
        self.total_action += 1
        if action_type == 'raise':
            self.total_raise += 1
        elif action_type == 'call':
            self.total_call += 1
        elif action_type == 'fold':
            self.total_fold += 1

    def observe_big_bet(self):
        self.big_bets += 1

    def observe_hand(self):
        self.hands_seen += 1
        if self.hands_seen >= self.next_eval and self.total_action >= 20:
            self._fit_profile()
            self.next_eval += 200   # re-evaluate every 200 hands

    def _fit_profile(self):
        if self.total_action == 0:
            self.profile = self.UNKNOWN
            return

        fold_rate  = self.total_fold  / self.total_action
        raise_rate = self.total_raise / self.total_action
        call_rate  = self.total_call  / self.total_action
        big_bet_rate = self.big_bets  / max(1, self.hands_seen)

        # Maniac: very aggressive or constant overbets
        if raise_rate > 0.22 or big_bet_rate > 0.35:
            self.profile = self.MANIAC

        # CallingStation: rarely folds, calls everything
        elif fold_rate < 0.22 and call_rate > 0.45:
            self.profile = self.STATION

        # NitFolder: folds a lot
        elif fold_rate > 0.52:
            self.profile = self.NIT

        # TAG: folds medium, raises often
        elif fold_rate >= 0.32 and raise_rate >= 0.18:
            self.profile = self.TAG

        # Else: unknown, use defaults
        else:
            self.profile = self.UNKNOWN

    def threshold_mods(self, street):
        """
        Returns (raise_delta, call_delta) adjustments based on profile.
        Negative = looser (lower threshold), Positive = tighter.
        """
        p = self.profile

        if p == self.MANIAC:
            # Tighten heavily — don't bluff, only raise with strong hands
            return (+0.06, +0.06)

        elif p == self.STATION:
            # Loosen raise (bet thin value), keep call similar
            return (-0.05, -0.02)

        elif p == self.NIT:
            # Loosen raise (they fold to pressure), loosen call
            return (-0.06, -0.04)

        elif p == self.TAG:
            # Respect their raises — tighten call slightly
            return (+0.02, +0.03)

        else:  # UNKNOWN
            return (0.0, 0.0)

    def bluff_ok(self, spr):
        """Whether bluffing is viable given profile."""
        if self.profile == self.STATION: return False
        if self.profile == self.MANIAC:  return False
        if spr < 1.5: return False
        return True

    def value_bet_thin(self):
        """Whether to bet thin (1/3 pot) on medium hands."""
        return self.profile in (self.STATION, self.UNKNOWN)


# ═══════════════════════════════════════════════════════════════════════════════
# BOT
# ═══════════════════════════════════════════════════════════════════════════════

class Bot(BaseBot):

    def __init__(self):
        self.profiler = OpponentProfiler()
        self.round    = 0

        # Bluff counters — deterministic schedule
        self._bluff_draw_counter = 0   # semi-bluff with draws
        self._bluff_air_counter  = 0   # bluff auction tax on air hands

        # Per-hand state
        self._reset_hand_state()

    def _reset_hand_state(self):
        self._hole            = []
        self._preflop_eq      = 0.5
        self._cat             = (13, 'Air')
        self._prev_street     = STREET_PREFLOP
        self._auction_won     = False
        self._auction_lost    = False
        self._has_opp_card    = False
        self._opp_card_threat = 0.0
        self._opp_card_brick  = True
        self._chips_pre_auc   = 5000
        self._chips_post_auc  = 0
        self._my_bid          = 0
        self._auc_detected    = False
        self._bets_faced      = 0
        self._street_bets     = {}   # street -> count of bets we faced

    # ── Equity by street ──────────────────────────────────────────────────────
    # Pre-flop: lookup table (instant)
    # Flop:     category equity (instant) — MC is too slow per match data
    # Turn:     exact if opp card known (~2ms), else category equity
    # River:    exact enumeration always (~1ms)

    def _equity(self, gs):
        hole  = [eval7.Card(s) for s in gs.my_hand]
        board = [eval7.Card(s) for s in gs.board]
        opp_r = [eval7.Card(s) for s in gs.opp_revealed_cards]
        street = gs.street

        if street == STREET_PREFLOP:
            return self._preflop_eq

        if street == 'river':
            return exact_river_equity(hole, board, opp_r)

        if street == 'turn' and len(board) == 4 and len(opp_r) >= 1:
            return exact_turn_equity_known(hole, board, opp_r)

        # Flop or turn without opp card known — use category
        return CATEGORY_EQUITY.get(self._cat[0], 0.5)

    # ── Auction bid ──────────────────────────────────────────────────────────
    # Air bluff every 10th air hand: bid 20% pot (cheap threat signal)
    # Opponent who raised to win sees we bid non-trivially on air hands →
    # they can't easily reverse-engineer our hand from bid size.
    # This is safe: even if they bid 30 and win, we fold and lose only 30.

    def _auction_bid(self, cat_id, pot, my_chips):
        frac = AUCTION_BID_FRAC.get(cat_id, 0.10)

        # Air bluff: every 10th air hand, bump bid to 20% pot
        if cat_id == 13:
            self._bluff_air_counter += 1
            if self._bluff_air_counter % 10 == 0:
                frac = 0.20

        bid = int(pot * frac)
        bid = max(AUCTION_MIN_BID, bid)
        bid = min(bid, int(my_chips * 0.08))
        return max(0, min(bid, my_chips))

    # ── Thresholds (static base + profile adjustment + street) ───────────────
    #
    # Key fix from match analysis:
    # - Our fold-to-bet on flop was 67-95% — WAY too high
    # - We were folding to any bet on flop. call_t was too high.
    # - Lower call_t so we don't over-fold (calling more on flop is correct)
    # - Tighten river call_t — we were calling off stacks on river (big losses)

    def _thresholds(self, street, facing_bet=False):
        # Base thresholds — tuned from match data
        if street == STREET_PREFLOP:
            raise_t, call_t = 0.68, 0.40
        elif street == 'flop':
            # Lower call_t from 0.42 → 0.36 to fix over-folding
            raise_t, call_t = 0.70, 0.36
        elif street == 'turn':
            raise_t, call_t = 0.72, 0.40
        else:  # river
            # Tighten river — we were losing -40k to -68k on river
            # Don't call large river bets without strong equity
            raise_t, call_t = 0.76, 0.48

        # Profile adjustments
        rd, cd = self.profiler.threshold_mods(street)
        raise_t += rd
        call_t  += cd

        # Facing a bet: slightly more respect (passive bots rarely bet light)
        if facing_bet:
            call_t += 0.02

        return max(0.52, min(0.85, raise_t)), max(0.28, min(0.62, call_t))

    # ── Revealed card threshold adjustment ───────────────────────────────────
    #
    # Won auction + brick revealed:
    #   We know they're weak → lower raise_t (be more aggressive)
    #   Draw hands specifically: can bluff since they're weak
    #
    # Won auction + connected card revealed:
    #   They may have made hand → raise raise_t (pot control)
    #   Don't build big pot with marginal hands
    #
    # Lost auction:
    #   They have info on us → tighten slightly

    def _apply_auction_adj(self, raise_t, call_t, cat_id):
        if self._auction_won:
            threat = self._opp_card_threat
            if threat < 0.25:          # brick
                raise_t -= 0.05
                call_t  -= 0.03
            elif threat > 0.60:        # connected — they likely have something
                raise_t += 0.06
                call_t  += 0.04
                if cat_id in (7,8,9,10):   # on draws vs made hands: tighten more
                    raise_t += 0.03
        elif self._auction_lost:
            raise_t += 0.02
            call_t  += 0.02
        return raise_t, call_t

    # ── Bet sizing ────────────────────────────────────────────────────────────
    #
    # Fix from match analysis: we were betting 78-113% pot on river → too big
    # Revised: flop 33-55%, turn 45-75%, river 50-85%
    # Low SPR: shove only with equity > 0.70 (was 0.62, too loose)
    # Revealed card brick → bet 12% bigger; connected → bet 12% smaller

    def _value_bet_size(self, equity, pot, min_r, max_r, street, spr):
        frac = max(0.0, min(1.0, (equity - 0.55) / 0.35))

        if street == 'flop':
            base = pot * (0.33 + frac * 0.22)   # 33%-55% of pot
        elif street == 'turn':
            base = pot * (0.45 + frac * 0.30)   # 45%-75% of pot
        else:  # river — reduced from previous version
            base = pot * (0.50 + frac * 0.35)   # 50%-85% of pot

        # Low SPR shove: only when VERY strong (was 0.62, raised to 0.70)
        if spr < 2.0 and equity > 0.70:
            base = max(base, max_r * 0.85)

        # Revealed card sizing
        if self._has_opp_card:
            if self._opp_card_brick:
                base *= 1.12   # they're weak, bet bigger
            else:
                base *= 0.88   # they have something, bet smaller

        target = int(base)
        return max(min_r, min(max_r, target))

    def _raise_size(self, equity, pot_odds, min_r, max_r):
        edge   = max(0.0, equity - pot_odds)
        frac   = min(1.0, edge * 2.5)
        target = int(min_r + frac * (max_r - min_r))
        return max(min_r, min(max_r, target))

    # ── Bluff schedule ────────────────────────────────────────────────────────
    #
    # Semi-bluff (draws): every Nth opportunity
    #   cat 7,8 (strong draws) → every 4th
    #   cat 5,6 (combo)        → every 5th
    #   cat 9,10 (weak draws)  → every 8th
    #
    # CRITICAL: bluff bet capped at 40% of pot to limit loss exposure.
    # No bluffs on river (exact equity — just fold if below threshold).
    # No bluffs facing bets (raises = value only).
    # Profile check: no bluffs vs calling stations or maniacs.

    def _should_bluff(self, cat_id, spr, street):
        if street == 'river':  return False
        if not self.profiler.bluff_ok(spr): return False

        if   cat_id in (7, 8): period = 4
        elif cat_id in (5, 6): period = 5
        elif cat_id in (9,10): period = 8
        else: return False

        self._bluff_draw_counter += 1
        return (self._bluff_draw_counter % period) == 0

    # ── Shove protection ──────────────────────────────────────────────────────
    #
    # Critical fix from match analysis: we were calling off full stacks
    # with top pair / second pair against opponent shoves.
    # Example: R#76 Ajayendra — we had KcKs (lost to 86s straight)
    #          R#601 Ajayendra — we had 96 and raised all-in vs a raise
    #
    # Rule: facing a shove (cost > 40% of effective stack), require
    # much higher equity than normal. cat 2 (GoodMade) is NOT enough.
    # Need cat 0/1 or equity > 0.65+ to call a shove.

    def _shove_call_equity(self, pot_odds, equity, cost, my_chips, opp_chips):
        """
        Returns True if we should call a large raise (near-shove).
        eff_frac = cost / effective_stack
        """
        eff        = min(my_chips, opp_chips)
        eff_frac   = cost / max(1, eff)

        if eff_frac >= 0.70:   # true shove — need very strong hand
            return equity >= max(pot_odds + 0.10, 0.60)
        if eff_frac >= 0.40:   # large raise — tighter than normal
            return equity >= max(pot_odds + 0.07, 0.55)
        return None   # not a shove situation, use normal logic

    # ── Callbacks ─────────────────────────────────────────────────────────────

    def on_hand_start(self, gi: GameInfo, gs: PokerState):
        self.round += 1
        self._reset_hand_state()
        self._hole       = [eval7.Card(s) for s in gs.my_hand]
        self._preflop_eq = preflop_strength(self._hole[0], self._hole[1]) \
                           if len(self._hole)==2 else 0.5

    def on_hand_end(self, gi: GameInfo, gs: PokerState):
        self.profiler.observe_hand()
        # Record fold if opponent folded
        if gs.is_terminal and gs.payoff > 0 and gs.cost_to_call != 0:
            self.profiler.observe_action('fold')

    # ── Main decision ─────────────────────────────────────────────────────────

    def get_move(self, gi: GameInfo, gs: PokerState):
        street = gs.street

        # Track street transitions
        if street != self._prev_street:
            self._bets_faced = 0
        self._prev_street = street

        # Track opponent actions for profiling
        opp_wgr = gs.opp_wager
        if street != 'auction':
            cost = gs.cost_to_call
            if cost > 0:
                self.profiler.observe_action('raise')
                # Track if opp is betting big
                pot = max(1, gs.pot)
                if cost > pot * 0.8:
                    self.profiler.observe_big_bet()
            else:
                self.profiler.observe_action('check')

        # Classify hand on postflop streets
        if street != STREET_PREFLOP:
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

        # Detect auction outcome once, on first post-auction call
        if not self._auc_detected and street in ('flop','turn','river'):
            self._auc_detected   = True
            self._chips_post_auc = gs.my_chips
            board_cards          = [eval7.Card(s) for s in gs.board]

            if gs.opp_revealed_cards:
                paid = self._chips_pre_auc - gs.my_chips
                self._auction_won   = (paid != self._my_bid)
                self._has_opp_card  = True
                rev_card = eval7.Card(gs.opp_revealed_cards[0])
                connects, threat = analyze_revealed_card(rev_card, board_cards)
                self._opp_card_threat = threat
                self._opp_card_brick  = not connects
            else:
                self._auction_lost = True

        # Equity
        equity = self._equity(gs)

        # State
        pot       = max(1, gs.pot)
        cost      = gs.cost_to_call
        my_chips  = gs.my_chips
        opp_chips = gs.opp_chips
        pot_odds  = cost/(pot+cost) if (pot+cost)>0 else 0.0
        spr       = min(my_chips, opp_chips) / pot

        can_raise = gs.can_act(ActionRaise)
        can_call  = gs.can_act(ActionCall)
        can_check = gs.can_act(ActionCheck)
        min_r = max_r = 0
        if can_raise:
            min_r, max_r = gs.raise_bounds

        raise_t, call_t = self._thresholds(street, facing_bet=(cost>0))
        raise_t, call_t = self._apply_auction_adj(raise_t, call_t, cat_id)

        # Pot-odds floor
        if cost > 0:
            call_t = max(call_t, pot_odds + 0.05)

        # ═════════════════════════════════════════════════════════════════════
        # PRE-FLOP
        # Per rules: SB acts FIRST preflop. BB acts first post-flop.
        # SB = my_wager == 10 (posted small blind)
        # ═════════════════════════════════════════════════════════════════════
        if street == STREET_PREFLOP:
            is_sb = (gs.my_wager == 10)

            # Check for shove/large raise first
            if can_call and cost > 0:
                shove_dec = self._shove_call_equity(pot_odds, equity, cost, my_chips, opp_chips)
                if shove_dec is not None:
                    return ActionCall() if shove_dec else ActionFold()

            if is_sb:
                # SB acts FIRST preflop — can fold, call, or raise
                # Raise wide: SB has positional DISadvantage postflop (acts last preflop
                # but FIRST on all postflop streets in this game). Compensate by raising
                # to build pot with initiative and deny free flops.
                if equity > 0.58 and can_raise:
                    sz = int(pot * 2.4)
                    return ActionRaise(max(min_r, min(max_r, sz)))
                if equity > 0.40 and can_call:
                    return ActionCall()
                if can_call and cost <= 10 and equity > 0.32:
                    return ActionCall()
                return ActionFold()
            else:
                # BB acts second preflop — can check/raise if no raise, or fold/call/raise
                if cost > 0:
                    # Facing SB raise
                    if equity >= raise_t and can_raise:
                        sz = int(pot * 2.2)
                        return ActionRaise(max(min_r, min(max_r, sz)))
                    if equity >= call_t:
                        return ActionCall() if can_call else ActionFold()
                    return ActionFold()
                else:
                    # SB just called — BB can check or raise
                    if equity >= raise_t and can_raise:
                        sz = int(pot * 2.2)
                        return ActionRaise(max(min_r, min(max_r, sz)))
                    return ActionCheck() if can_check else ActionCall()

        # ═════════════════════════════════════════════════════════════════════
        # POST-FLOP (flop / turn / river)
        # BB acts FIRST on all postflop streets in this game.
        # ═════════════════════════════════════════════════════════════════════

        # Overbet protection — tighten call threshold vs large bets
        # Fix: we were calling off stack vs pot-sized overbets
        if cost > 0 and pot > 0:
            pot_before_bet = max(1, pot - cost)
            bet_ratio = cost / pot_before_bet
            if   bet_ratio >= 2.0:  call_t = max(call_t, 0.62)
            elif bet_ratio >= 1.5:  call_t = max(call_t, 0.58)
            elif bet_ratio >= 1.0:  call_t = max(call_t, 0.54)
            elif bet_ratio >= 0.75: call_t = max(call_t, 0.50)

        # Multi-bet protection: tighten after facing 2+ bets on same street
        if cost > 0:
            self._bets_faced += 1
            if self._bets_faced >= 2:
                call_t += 0.04

        if cost > 0:
            # ── FACING A BET / RAISE ──────────────────────────────────────────

            # Shove protection first
            shove_dec = self._shove_call_equity(pot_odds, equity, cost, my_chips, opp_chips)
            if shove_dec is not None:
                if shove_dec and can_call:
                    return ActionCall()
                return ActionCheck() if can_check else ActionFold()

            # Value raise
            if equity >= raise_t and can_raise:
                ra = self._raise_size(equity, pot_odds, min_r, max_r)
                return ActionRaise(ra)

            # Call
            if equity >= call_t:
                return ActionCall() if can_call else (ActionCheck() if can_check else ActionFold())

            # No bluff-raises when facing bets
            return ActionCheck() if can_check else ActionFold()

        else:
            # ── ACTING FIRST (no bet to face) ─────────────────────────────────

            # Strong hands: value bet
            if equity >= raise_t and can_raise:
                ra = self._value_bet_size(equity, pot, min_r, max_r, street, spr)
                return ActionRaise(ra)

            # River thin value: exact equity, bet 35% pot with 0.62-raise_t
            if street == 'river' and equity >= 0.62 and can_raise:
                # Only thin bet vs calling station profile or unknown
                if self.profiler.value_bet_thin():
                    thin_sz = int(pot * 0.35)
                    return ActionRaise(max(min_r, min(max_r, thin_sz)))

            # Medium-strong: probe bet on flop/turn (not river — too risky)
            if cat_id in (2,3) and equity >= 0.58 and can_raise and street != 'river':
                ra = self._value_bet_size(equity, pot, min_r, max_r, street, spr)
                return ActionRaise(ra)

            # Semi-bluff (draw hands, deterministic schedule)
            # Cap bluff size at 40% pot to limit loss exposure
            if self._should_bluff(cat_id, spr, street) and can_raise:
                bluff_sz = int(pot * 0.40)
                # Hard cap: bluff bet never exceeds 500 chips (avoids catastrophic loss)
                bluff_sz = min(bluff_sz, 500, max_r)
                bluff_sz = max(min_r, bluff_sz)
                return ActionRaise(bluff_sz)

            return ActionCheck() if can_check else (ActionCall() if can_call else ActionFold())


if __name__ == '__main__':
    args = parse_args()
    run_bot(Bot(), args)