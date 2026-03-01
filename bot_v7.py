from pkbot.base import BaseBot
from pkbot.actions import ActionCall, ActionCheck, ActionRaise, ActionFold, ActionBid
from pkbot.states import PokerState, GameInfo
from pkbot.runner import parse_args, run_bot
import eval7

# Street name constant — engine uses 'pre-flop' (with hyphen!)
STREET_PREFLOP = 'pre-flop'

# ═══════════════════════════════════════════════════════════════════════════════
# PREFLOP TABLE  —  O(1) equity lookup, zero compute
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
# HAND CLASSIFICATION  —  14 categories via eval7 (deterministic)
# eval7 encoding: 0=HighCard 1=Pair 2=TwoPair 3=Trips 4=Straight 5=Flush
#                  6=FullHouse 7=Quads 8=StraightFlush
# ═══════════════════════════════════════════════════════════════════════════════

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

    score = eval7.evaluate(hole + board)
    hand_type = score >> 24

    fd = _flush_outs(hole, board)
    sd = _straight_outs(hole, board)
    has_fd   = fd >= 9
    has_oesd = sd >= 8
    has_gut  = sd >= 4
    has_bd   = fd == 3

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
            return (1, 'Strong') if max(hr) == max(br) else (2, 'GoodMade')
        return (3, 'MedMade')

    if hand_type == 1:
        br_s   = sorted([c.rank for c in board], reverse=True)
        hr_s   = sorted([c.rank for c in hole],  reverse=True)
        br_set = set(br_s)

        if hr_s[0] == hr_s[1]:
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
            if has_fd and has_oesd: return (5, 'ComboStrong')
            if has_fd or has_oesd or has_gut: return (6, 'ComboMed')
            return (4, 'WeakMade')
        else:
            return (6, 'ComboMed') if (has_fd or has_oesd) else (4, 'WeakMade')

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
# CATEGORY EQUITY — deterministic, used for flop/turn decisions
# ═══════════════════════════════════════════════════════════════════════════════

CATEGORY_EQUITY = {
    0: 0.93, 1: 0.87, 2: 0.75, 3: 0.62, 4: 0.50,
    5: 0.65, 6: 0.54, 7: 0.49, 8: 0.41, 9: 0.36,
    10: 0.29, 11: 0.32, 12: 0.25, 13: 0.18,
}


# ═══════════════════════════════════════════════════════════════════════════════
# EXACT RIVER EQUITY  —  enumerate all possible opponent hands
#   On river: board=5, hole=2, known opp cards=0 or 1
#   Unknown: C(remaining,2) = ~990 combos → ~1ms (EXACT, no randomness)
#   Known 1: remaining_cards → ~44 combos → ~0.05ms
# ═══════════════════════════════════════════════════════════════════════════════

_DECK_52 = eval7.Deck().cards

def exact_river_equity(hole, board, opp_revealed):
    """Calculate exact river equity by enumerating all opponent hands."""
    known = set(hole + board + opp_revealed)
    remaining = [c for c in _DECK_52 if c not in known]

    my_score = eval7.evaluate(hole + board)

    wins = 0.0
    total = 0

    if len(opp_revealed) == 1:
        # Known 1 card: enumerate the unknown second card
        rev = opp_revealed[0]
        for c in remaining:
            opp_score = eval7.evaluate([rev, c] + board)
            total += 1
            if   my_score > opp_score: wins += 1.0
            elif my_score == opp_score: wins += 0.5
    else:
        # Unknown: enumerate all C(remaining, 2) = ~990 combos
        n = len(remaining)
        for i in range(n):
            for j in range(i + 1, n):
                opp_score = eval7.evaluate([remaining[i], remaining[j]] + board)
                total += 1
                if   my_score > opp_score: wins += 1.0
                elif my_score == opp_score: wins += 0.5

    return wins / total if total > 0 else 0.5


def exact_turn_equity(hole, board, opp_revealed):
    """Calculate exact turn equity by enumerating river card + opponent hands.
    Board must have exactly 4 cards (turn).
    With 1 known opp card:  45 river × 44 unknown = 1,980 evals (~2ms)
    With 0 known opp cards: 46 river × C(45,2)    = 45,540 evals (~45ms)
    """
    known = set(hole + board + opp_revealed)
    remaining = [c for c in _DECK_52 if c not in known]

    wins = 0.0
    total = 0

    if len(opp_revealed) == 1:
        rev = opp_revealed[0]
        for ri, river_card in enumerate(remaining):
            full_board = board + [river_card]
            my_score = eval7.evaluate(hole + full_board)
            for ui in range(len(remaining)):
                if ui == ri:
                    continue
                unk = remaining[ui]
                opp_score = eval7.evaluate([rev, unk] + full_board)
                total += 1
                if   my_score > opp_score: wins += 1.0
                elif my_score == opp_score: wins += 0.5
    else:
        # No known opp card: enumerate river × all opp C(remaining-1, 2)
        for ri, river_card in enumerate(remaining):
            full_board = board + [river_card]
            my_score = eval7.evaluate(hole + full_board)
            rest = [remaining[j] for j in range(len(remaining)) if j != ri]
            n2 = len(rest)
            for i in range(n2):
                for j in range(i + 1, n2):
                    opp_score = eval7.evaluate([rest[i], rest[j]] + full_board)
                    total += 1
                    if   my_score > opp_score: wins += 1.0
                    elif my_score == opp_score: wins += 0.5

    return wins / total if total > 0 else 0.5


# ═══════════════════════════════════════════════════════════════════════════════
# AUCTION BID TABLE  —  fraction of pot by category
#
# Key insight from bits match: opponents who bid high (218 avg) always
# win the auction and see our card. But Vickrey means they pay OUR bid.
# By bidding HIGHER, we tax their strategy: they still win but pay more.
# Minimum floor: 8 chips for all categories → any auction-dominator pays 8+
# ═══════════════════════════════════════════════════════════════════════════════

AUCTION_BID_FRAC = {
    0:  0.05,   # Monster — small but forces opponent to pay something
    1:  0.12,   # Strong
    2:  0.25,   # GoodMade
    3:  0.32,   # MedMade — highest uncertainty
    4:  0.28,   # WeakMade
    5:  0.35,   # ComboStrong — draw + pair
    6:  0.28,   # ComboMed
    7:  0.30,   # NutDraw
    8:  0.25,   # Draw
    9:  0.18,   # WeakDraw
    10: 0.12,   # Gutshot
    11: 0.20,   # AceHigh
    12: 0.10,   # HighCard
    13: 0.05,   # Air
}

# Minimum bid floor: force auction-dominators to pay at least this
AUCTION_MIN_BID = 8


# ═══════════════════════════════════════════════════════════════════════════════
# REVEALED CARD ANALYSIS
# ═══════════════════════════════════════════════════════════════════════════════

def revealed_card_connects(rev_card, board):
    """Returns True if the revealed card connects to the board."""
    r = rev_card.rank + 2
    s = rev_card.suit
    board_ranks = [c.rank + 2 for c in board]
    board_suits = [c.suit for c in board]
    if r in board_ranks:             return True
    if board_suits.count(s) >= 2:    return True
    connects = sum(1 for br in board_ranks if abs(r - br) <= 2)
    if connects >= 2:                return True
    return False


# ═══════════════════════════════════════════════════════════════════════════════
# BOT  —  Deterministic strategy
#
# Design principles:
# 1. ZERO Monte Carlo — preflop table + category equity + exact river
# 2. ZERO randomness in decisions — no random bluffs, no random sizing
# 3. Deterministic bet sizing based on equity/category
# 4. Tight defaults that only loosen when evidence supports it
# 5. River decisions are EXACT (full enumeration)
# ═══════════════════════════════════════════════════════════════════════════════

class Bot(BaseBot):

    def __init__(self):
        self.round = 0

        # Per-hand state (reset each hand)
        self._hole            = []
        self._preflop_eq      = 0.5
        self._cat             = (13, 'Air')
        self._prev_opp_wgr    = 0
        self._prev_street     = STREET_PREFLOP
        self._auction_won     = False
        self._auction_lost    = False
        self._has_opp_card    = False
        self._chips_pre_auc   = 5000
        self._chips_post_auc  = 0
        self._my_bid          = 0
        self._auc_detected    = False
        self._opp_card_brick  = False

        # Cumulative bluff counter: deterministic alternating pattern
        self._bluff_counter   = 0
        # Street bet counter: track how many bets we've faced this hand
        self._bets_faced      = 0

    # ── Equity by street ──────────────────────────────────────────────────────

    def _equity(self, gs):
        """
        Preflop:  PREFLOP_TABLE (O(1))
        Flop:     CATEGORY_EQUITY (O(1))
        Turn:     exact_turn_equity when opp card known (~2ms), else CATEGORY_EQUITY
        River:    exact_river_equity (enumeration, ~1ms, EXACT)
        """
        hole  = [eval7.Card(s) for s in gs.my_hand]
        board = [eval7.Card(s) for s in gs.board]
        opp_r = [eval7.Card(s) for s in gs.opp_revealed_cards]

        if gs.street == STREET_PREFLOP:
            return self._preflop_eq

        if gs.street == 'river':
            return exact_river_equity(hole, board, opp_r)

        if gs.street == 'turn' and len(board) == 4:
            if len(opp_r) >= 1:
                # Exact turn equity with known opp card: ~1,980 evals, ~2ms
                return exact_turn_equity(hole, board, opp_r)
            else:
                # No opp card known (~5% of hands): exact but slower ~45ms
                # Safe: only ~32 hands/game × 45ms = 1.4s
                return exact_turn_equity(hole, board, opp_r)

        # Flop: use deterministic category equity
        return CATEGORY_EQUITY.get(self._cat[0], 0.5)

    # ── Auction bid ──────────────────────────────────────────────────────────

    def _auction_bid(self, cat_id, pot, my_chips):
        frac = AUCTION_BID_FRAC.get(cat_id, 0.10)
        bid = int(pot * frac)

        # Floor: force auction-dominators to pay
        bid = max(AUCTION_MIN_BID, bid)

        # Cap: never risk more than 8% of stack
        bid = min(bid, int(my_chips * 0.08))

        return max(0, bid)

    # ── Deterministic bluff schedule ─────────────────────────────────────────

    def _should_bluff(self, cat_id, spr, street):
        """Deterministic bluff: every Nth opportunity based on category."""
        if spr < 1.5: return False
        if street == 'river': return False   # no bluffs on river (decisions exact)

        # Only semi-bluff with hands that have outs
        if cat_id in (7, 8):      # Strong draws: bluff every 4th opportunity
            period = 4
        elif cat_id in (5, 6):    # Combo hands: bluff every 5th
            period = 5
        elif cat_id in (9, 10):   # Weak draws: bluff every 8th
            period = 8
        else:
            return False

        self._bluff_counter += 1
        return (self._bluff_counter % period) == 0

    # ── Bet sizing (deterministic) ───────────────────────────────────────────

    def _value_bet_size(self, equity, pot, min_r, max_r, street, spr):
        # Equity-scaled fraction of pot
        frac = max(0.0, min(1.0, (equity - 0.55) / 0.35))

        if street == 'flop':
            base = pot * (0.33 + frac * 0.34)
        elif street == 'turn':
            base = pot * (0.40 + frac * 0.45)
        else:  # river
            base = pot * (0.50 + frac * 0.60)

        # Low SPR: shove for max pressure
        if spr < 2.0 and equity > 0.62:
            base = max(base, max_r * 0.9)

        # Auction info: size adjustment
        if self._has_opp_card:
            if self._opp_card_brick:
                base *= 1.12
            else:
                base *= 0.88

        target = int(base)
        return max(min_r, min(max_r, target))

    def _raise_size(self, equity, pot_odds, min_r, max_r):
        edge = max(0.0, equity - pot_odds)
        frac = min(1.0, edge * 2.5)
        target = int(min_r + frac * (max_r - min_r))
        return max(min_r, min(max_r, target))

    # ── Thresholds (static + street-specific) ────────────────────────────────

    def _thresholds(self, street, facing_bet=False):
        """
        Fixed thresholds. No opponent adaptation = no variability from
        opponent model noise. Tight defaults that protect against losses.
        """
        raise_t = 0.72
        call_t  = 0.42

        # Street-specific adjustments
        if street == 'river':
            raise_t += 0.04   # only raise river with very strong hands
            call_t  += 0.06   # river calls need significantly more equity
        elif street == 'turn':
            raise_t += 0.01
            call_t  += 0.02

        # When facing a bet: respect it more (passive opponents rarely bet light)
        if facing_bet:
            call_t += 0.03

        return max(0.55, min(0.85, raise_t)), max(0.30, min(0.62, call_t))

    # ── Engine callbacks ─────────────────────────────────────────────────────

    def on_hand_start(self, gi: GameInfo, gs: PokerState):
        self.round += 1
        self._hole           = [eval7.Card(s) for s in gs.my_hand]
        self._cat            = (13, 'Air')
        self._prev_opp_wgr   = gs.opp_wager
        self._prev_street    = STREET_PREFLOP
        self._auction_won    = False
        self._auction_lost   = False
        self._has_opp_card   = False
        self._auc_detected   = False
        self._opp_card_brick = False
        self._chips_pre_auc  = gs.my_chips
        self._chips_post_auc = 0
        self._my_bid         = 0
        self._bets_faced     = 0
        self._preflop_eq     = preflop_strength(self._hole[0], self._hole[1]) \
                               if len(self._hole) == 2 else 0.5

    def on_hand_end(self, gi: GameInfo, gs: PokerState):
        pass  # No opponent model to update

    # ── Main decision ────────────────────────────────────────────────────────

    def get_move(self, gi: GameInfo, gs: PokerState):
        street = gs.street

        # ── Track street transitions ─────────────────────────────────────────
        if street != self._prev_street:
            self._prev_opp_wgr = 0
        self._prev_opp_wgr = gs.opp_wager
        self._prev_street  = street

        # ── Classify hand (postflop) ─────────────────────────────────────────
        if street not in (STREET_PREFLOP,):
            board = [eval7.Card(s) for s in gs.board]
            if len(board) >= 3:
                self._cat = classify_hand(self._hole, board)

        cat_id = self._cat[0]

        # ═════════════════════════════════════════════════════════════════════
        # AUCTION
        # ═════════════════════════════════════════════════════════════════════
        if street == 'auction':
            self._chips_pre_auc = gs.my_chips
            bid = self._auction_bid(cat_id, max(1, gs.pot), gs.my_chips)
            self._my_bid = bid
            return ActionBid(bid)

        # ── Detect auction outcome ───────────────────────────────────────────
        if not self._auc_detected and street in ('flop', 'turn', 'river'):
            self._auc_detected = True
            self._chips_post_auc = gs.my_chips
            if gs.opp_revealed_cards:
                paid = self._chips_pre_auc - gs.my_chips
                if paid != self._my_bid:
                    self._auction_won = True
                else:
                    pass  # tie
                self._has_opp_card = True

                board_cards = [eval7.Card(s) for s in gs.board]
                rev_card = eval7.Card(gs.opp_revealed_cards[0])
                self._opp_card_brick = not revealed_card_connects(rev_card, board_cards)
            else:
                self._auction_lost = True

        # ── Equity ───────────────────────────────────────────────────────────
        equity = self._equity(gs)

        # ── State ────────────────────────────────────────────────────────────
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

        raise_t, call_t = self._thresholds(street, facing_bet=(cost > 0))

        # Pot-odds floor: NEVER call unless equity exceeds pot odds + margin
        if cost > 0:
            call_t = max(call_t, pot_odds + 0.06)

        # ═════════════════════════════════════════════════════════════════════
        # PREFLOP  (street == 'pre-flop')
        # ═════════════════════════════════════════════════════════════════════
        if street == STREET_PREFLOP:
            is_sb = (gs.my_wager == 10)

            # Facing a shove (>= 50% of effective stack)
            if can_call and cost > 0:
                eff = min(my_chips, opp_chips)
                shove_frac = cost / max(1, eff)
                if shove_frac >= 0.70:
                    thresh = max(pot_odds + 0.08, 0.48)
                    return ActionCall() if equity >= thresh else ActionFold()
                if shove_frac >= 0.40:
                    thresh = max(pot_odds + 0.06, 0.45)
                    return ActionCall() if equity >= thresh else ActionFold()

            if is_sb:
                # SB: raise strong, limp medium, fold weak
                if equity > 0.60 and can_raise:
                    sz = int(pot * 2.4)
                    return ActionRaise(max(min_r, min(max_r, sz)))
                if equity > 0.44 and can_call:
                    return ActionCall()
                if can_call and cost <= 10 and equity > 0.36:
                    return ActionCall()
                return ActionFold()
            else:
                # BB: raise very strong, check all else (never fold in BB for free)
                if cost > 0:
                    # Facing raise
                    if equity >= 0.72 and can_raise:
                        sz = int(pot * 2.2)
                        return ActionRaise(max(min_r, min(max_r, sz)))
                    if equity >= max(call_t, pot_odds + 0.06):
                        return ActionCall() if can_call else ActionFold()
                    return ActionFold()
                else:
                    # No raise to face (limped or we just post BB)
                    if equity >= 0.68 and can_raise:
                        sz = int(pot * 2.2)
                        return ActionRaise(max(min_r, min(max_r, sz)))
                    if can_check:
                        return ActionCheck()
                    return ActionCall() if can_call else ActionFold()

        # ═════════════════════════════════════════════════════════════════════
        # POST-FLOP (flop / turn / river)
        # ═════════════════════════════════════════════════════════════════════

        # Overbet protection: tighter call threshold vs large bets
        if cost > 0 and pot > 0:
            pot_before_bet = max(1, pot - cost)
            bet_ratio = cost / pot_before_bet
            if   bet_ratio >= 2.0:   call_t = max(call_t, 0.60)
            elif bet_ratio >= 1.5:   call_t = max(call_t, 0.56)
            elif bet_ratio >= 1.0:   call_t = max(call_t, 0.52)
            elif bet_ratio >= 0.75:  call_t = max(call_t, 0.48)

        # Multi-bet protection: facing 2nd+ bet this street = stronger range
        if cost > 0:
            self._bets_faced += 1
            if self._bets_faced >= 2:
                call_t += 0.04  # they're repping strong when betting again

        if cost > 0:
            # ── FACING A BET ─────────────────────────────────────────────────

            # Raise with strong hands
            if equity >= raise_t and can_raise:
                ra = self._raise_size(equity, pot_odds, min_r, max_r)
                return ActionRaise(ra)

            # Call with adequate equity
            if equity >= call_t:
                return ActionCall() if can_call else (ActionCheck() if can_check else ActionFold())

            # No bluff-raises when facing bets (deterministic = no gambles)
            if can_check:
                return ActionCheck()
            return ActionFold()

        else:
            # ── ACTING FIRST ─────────────────────────────────────────────────

            # Value bet strong hands
            if equity >= raise_t and can_raise:
                ra = self._value_bet_size(equity, pot, min_r, max_r, street, spr)
                return ActionRaise(ra)

            # River thin value: exact equity allows precise thin bets
            # Bet 1/3 pot with equity 0.62-raise_t to extract value from weaker calls
            if street == 'river' and equity >= 0.62 and can_raise:
                thin_sz = int(pot * 0.35)
                ra = max(min_r, min(max_r, thin_sz))
                return ActionRaise(ra)

            # Bet medium-strong hands on flop/turn (deterministic)
            if cat_id in (2, 3) and equity >= 0.58 and can_raise:
                if street != 'river':
                    ra = self._value_bet_size(equity, pot, min_r, max_r, street, spr)
                    return ActionRaise(ra)

            # Semi-bluff (deterministic schedule)
            if self._should_bluff(cat_id, spr, street) and can_raise:
                ra = max(min_r, min(max_r, int(pot * 0.40)))
                return ActionRaise(ra)

            if can_check:
                return ActionCheck()
            if can_call:
                return ActionCall()
            return ActionFold()


if __name__ == '__main__':
    args = parse_args()
    run_bot(Bot(), args)
