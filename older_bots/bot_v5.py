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
# HAND CLASSIFICATION  —  14 categories, uses eval7 (no MC)
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

    # eval7 encoding: 0=HighCard 1=Pair 2=TwoPair 3=Trips 4=Straight 5=Flush 6=FH 7=Quads 8=SF
    if hand_type >= 6: return (0, 'Monster')   # Full House, Quads, Straight Flush
    if hand_type == 5: return (0, 'Monster')   # Flush
    if hand_type == 4: return (1, 'Strong')    # Straight

    if hand_type == 3:  # Trips
        br = [c.rank for c in board]
        for hr in [c.rank for c in hole]:
            if br.count(hr) >= 2: return (1, 'Strong')
        return (3, 'MedMade')

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

    # High card — draw territory
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
# FALLBACK EQUITY BY CATEGORY  —  used when time < 3s
# ═══════════════════════════════════════════════════════════════════════════════

CATEGORY_EQUITY = {
    0: 0.93, 1: 0.87, 2: 0.75, 3: 0.62, 4: 0.50,
    5: 0.65, 6: 0.54, 7: 0.49, 8: 0.41, 9: 0.36,
    10: 0.29, 11: 0.32, 12: 0.25, 13: 0.18,
}


# ═══════════════════════════════════════════════════════════════════════════════
# AUCTION BID TABLE  —  fraction of pot by category (replaces MC info_value)
#
# Middle categories (2-8) get the highest bids because information has the
# most impact when our hand strength is uncertain.
# Monsters don't need info. Air can't use it.
# An intimidation premium (+0.05) is added for categories 2-8 because
# winning the auction discourages opponent from bluffing.
#
# In Vickrey auction we pay the LOSER's bid regardless.
# So if opponent bids ~4 chips, we win and pay ~4 even though we bid 20+.
# ═══════════════════════════════════════════════════════════════════════════════

AUCTION_BID_FRAC = {
    0:  0.00,   # Monster — already winning
    1:  0.08,   # Strong — slight confirmation value
    2:  0.22,   # GoodMade — want to know if dominated
    3:  0.30,   # MedMade — highest uncertainty, info most valuable
    4:  0.25,   # WeakMade — need info to decide fold/continue
    5:  0.32,   # ComboStrong — draw + pair, info crucial
    6:  0.25,   # ComboMed — similar uncertainty
    7:  0.28,   # NutDraw — want to know if drawing live
    8:  0.22,   # Draw — moderate info value
    9:  0.15,   # WeakDraw — modest
    10: 0.10,   # Gutshot — low draw
    11: 0.18,   # AceHigh — knowing opponent card helps
    12: 0.08,   # HighCard — might fold anyway
    13: 0.02,   # Air — noise bid for unpredictability
}

INTIMIDATION_BONUS = 0.05   # added for categories 2-8


# ═══════════════════════════════════════════════════════════════════════════════
# REVEALED CARD ANALYSIS  (for bet sizing only, NOT threshold shifts)
# MC equity already incorporates the revealed card info into the equity number.
# We use this ONLY for: (a) bet sizing adjustments (b) bluff frequency boost.
# ═══════════════════════════════════════════════════════════════════════════════

def revealed_card_connects(rev_card, board):
    """Returns True if the revealed card connects to the board."""
    r = rev_card.rank + 2
    s = rev_card.suit
    board_ranks = [c.rank + 2 for c in board]
    board_suits = [c.suit for c in board]
    if r in board_ranks:             return True   # pairs the board
    if board_suits.count(s) >= 2:    return True   # flush draw
    connects = sum(1 for br in board_ranks if abs(r - br) <= 2)
    if connects >= 2:                return True   # straight connectivity
    return False


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
# OPPONENT MODEL  —  minimal EMA, no profiles, no switching
# ═══════════════════════════════════════════════════════════════════════════════

class OpponentModel:
    ALPHA = 0.08

    def __init__(self):
        self.hands          = 0
        self.ema_aggression = 0.30   # fraction of actions that are raises
        self.ema_fold_rate  = 0.40   # fraction of actions that are folds
        self.ema_bid        = 8.0    # average auction bid
        self.ema_auc_win    = 0.50   # their auction win rate

    def _ema(self, old, obs):
        return (1.0 - self.ALPHA) * old + self.ALPHA * obs

    def observe_action(self, action_type):
        self.ema_aggression = self._ema(self.ema_aggression, 1.0 if action_type == 'raise' else 0.0)
        self.ema_fold_rate  = self._ema(self.ema_fold_rate,  1.0 if action_type == 'fold' else 0.0)

    def observe_bid(self, bid):
        if bid >= 0:
            self.ema_bid = self._ema(self.ema_bid, float(bid))

    def observe_auction(self, they_won):
        self.ema_auc_win = self._ema(self.ema_auc_win, 1.0 if they_won else 0.0)

    @property
    def is_foldy(self):
        return self.ema_fold_rate > 0.45

    @property
    def is_station(self):
        return self.ema_fold_rate < 0.22 and self.ema_aggression < 0.25

    @property
    def is_aggro(self):
        return self.ema_aggression > 0.45


# ═══════════════════════════════════════════════════════════════════════════════
# BOT
# ═══════════════════════════════════════════════════════════════════════════════

class Bot(BaseBot):

    def __init__(self):
        self.opp   = OpponentModel()
        self.round = 0

        # Per-hand state (reset each hand)
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
        self._opp_card_brick  = False   # True when revealed card doesn't connect

    # ── Time-adaptive MC iters ────────────────────────────────────────────────
    #  Preflop: 0 (use table).  Obvious categories: capped low.
    #  Total budget target: ~4-8s per 1000-hand game.

    def _iters(self, tb, cat_id):
        if tb < 3.0: return 0      # emergency: use category fallback
        if tb < 5.0: return 12

        if   tb > 18: base = 150
        elif tb > 14: base = 120
        elif tb > 10: base = 90
        elif tb > 7:  base = 60
        else:         base = 35

        # Don't waste iters on obvious hands
        cap = {0: 20, 1: 60, 12: 20, 13: 12, 11: 25}.get(cat_id, base)
        return min(base, cap)

    # ── Equity (MC + fallback) ────────────────────────────────────────────────

    def _equity(self, gs, tb, override_iters=None):
        hole  = [eval7.Card(s) for s in gs.my_hand]
        board = [eval7.Card(s) for s in gs.board]
        opp_r = [eval7.Card(s) for s in gs.opp_revealed_cards]
        cat_id = self._cat[0]

        if gs.street == 'preflop' and not opp_r:
            return self._preflop_eq

        n = override_iters if override_iters is not None else self._iters(tb, cat_id)
        if n == 0:
            return CATEGORY_EQUITY[cat_id]

        return calc_equity(hole, board, opp_r, n)

    # ── Auction bid (category-based, NO MC) ──────────────────────────────────
    #  Vickrey dominant strategy: bid true value.
    #  True value = info_value + intimidation + denial.
    #  Approximated by category-based fraction of pot.

    def _auction_bid(self, cat_id, pot, my_chips):
        frac = AUCTION_BID_FRAC.get(cat_id, 0.10)

        # Intimidation bonus: winning disrupts opponent's bluffing
        if 2 <= cat_id <= 8:
            frac += INTIMIDATION_BONUS

        bid = int(pot * frac)

        # Cap: never risk more than 8% of stack on info
        bid = min(bid, int(my_chips * 0.08))

        # Floor: even low categories bid something small to force opponent to pay
        if cat_id in (9, 10, 11) and bid < 3:
            bid = random.randint(1, 6)

        # Noise +/-8% for unpredictability
        if bid > 4:
            bid = int(bid * random.uniform(0.92, 1.08))

        return max(0, bid)

    # ── Bluff check ──────────────────────────────────────────────────────────

    def _should_bluff(self, cat_id, spr):
        if self.opp.is_station: return False
        if spr < 1.2: return False
        ftr = self.opp.ema_fold_rate
        if ftr < 0.38: return False

        # Brick boost: we KNOW they're weak, they don't know we know
        brick_boost = 0.08 if (self._auction_won and self._opp_card_brick) else 0.0

        # Semi-bluff: draws + overcards
        if cat_id in (5, 6, 7, 8):
            prob = 0.18 + 0.25 * (ftr - 0.38) + brick_boost
        elif cat_id in (9, 10, 11):
            prob = 0.08 + 0.15 * (ftr - 0.38) + brick_boost
        elif cat_id in (12, 13):
            prob = 0.04 + 0.10 * (ftr - 0.38) + brick_boost
        else:
            return False

        return random.random() < min(prob, 0.35)

    # ── Bet sizing ───────────────────────────────────────────────────────────

    def _value_bet_size(self, equity, pot, min_r, max_r):
        # Scale from 0.38x pot (thin) to 0.85x pot (strong)
        frac = max(0.0, min(1.0, (equity - 0.55) / 0.35))
        base = pot * (0.38 + frac * 0.47)

        # Auction-informed sizing (equity already accounts for the card;
        # this adjusts HOW MUCH we extract / risk)
        if self._auction_won:
            if self._opp_card_brick:
                base *= 1.15   # they're likely weak → size up for max value
            else:
                base *= 0.85   # they connect → pot control, smaller sizing

        target = int(base)
        return max(min_r, min(max_r, target))

    def _raise_size(self, equity, pot_odds, min_r, max_r):
        edge = max(0.0, equity - pot_odds)
        frac = min(1.0, edge * 2.5)
        target = int(min_r + frac * (max_r - min_r))
        return max(min_r, min(max_r, target))

    # ── Thresholds (smooth EMA adjustments, no modes) ────────────────────────

    def _thresholds(self, street):
        ftr = self.opp.ema_fold_rate
        agg = self.opp.ema_aggression

        raise_t = 0.70
        call_t  = 0.40

        # Exploit folders: widen raise range
        if ftr > 0.42:
            delta = min(0.10, 0.16 * (ftr - 0.42))
            raise_t -= delta
            call_t  -= delta * 0.5

        # Tighten vs aggro opponents
        if agg > 0.42:
            delta = min(0.08, 0.14 * (agg - 0.42))
            raise_t += delta
            call_t  += delta * 0.6

        # Calling station: bet thinner for value
        if self.opp.is_station:
            raise_t -= 0.05
            call_t  -= 0.04

        # River: slightly tighter (no equity improvement ahead)
        if street == 'river':
            raise_t += 0.02
            call_t  += 0.02

        return max(0.52, min(0.82, raise_t)), max(0.28, min(0.58, call_t))

    # ── Engine callbacks ─────────────────────────────────────────────────────

    def on_hand_start(self, gi: GameInfo, gs: PokerState):
        self.round += 1
        self._hole          = [eval7.Card(s) for s in gs.my_hand]
        self._cat           = (13, 'Air')
        self._prev_opp_wgr  = gs.opp_wager
        self._prev_street   = 'preflop'
        self._auction_won   = False
        self._auction_lost  = False
        self._auc_detected  = False
        self._opp_card_brick = False
        self._chips_pre_auc = gs.my_chips
        self._chips_post_auc = 0
        self._my_bid        = 0
        self._preflop_eq    = preflop_strength(self._hole[0], self._hole[1]) \
                              if len(self._hole) == 2 else 0.5

    def on_hand_end(self, gi: GameInfo, gs: PokerState):
        self.opp.hands += 1

        # Record fold if opponent folded
        if gs.is_terminal and gs.payoff > 0 and gs.cost_to_call != 0:
            self.opp.observe_action('fold')

        # Infer opponent auction bid from chip changes
        if self._chips_post_auc > 0:
            auction_cost = self._chips_pre_auc - self._chips_post_auc
            if self._auction_won:
                # We won: we paid their bid (Vickrey second-price)
                self.opp.observe_bid(auction_cost)
                self.opp.observe_auction(they_won=False)
            elif self._auction_lost:
                # We lost: they bid more than us, estimate conservatively
                self.opp.observe_bid(self._my_bid + random.randint(1, 20))
                self.opp.observe_auction(they_won=True)
            else:
                # Tie: both paid own bid
                self.opp.observe_bid(self._my_bid)
                self.opp.observe_auction(they_won=False)

    # ── Main decision ────────────────────────────────────────────────────────

    def get_move(self, gi: GameInfo, gs: PokerState):
        street = gs.street
        tb     = gi.time_bank

        # ── Track opponent actions ───────────────────────────────────────────
        if street != self._prev_street:
            self._prev_opp_wgr = 0
        opp_wgr = gs.opp_wager
        if street not in ('auction',):
            if opp_wgr > self._prev_opp_wgr:
                if gs.cost_to_call > 0:
                    self.opp.observe_action('raise')
                else:
                    self.opp.observe_action('call')
            elif (opp_wgr == self._prev_opp_wgr and gs.cost_to_call == 0
                  and street == self._prev_street and street != 'preflop'):
                self.opp.observe_action('check')
        self._prev_opp_wgr = opp_wgr
        self._prev_street  = street

        # ── Classify hand (postflop) ─────────────────────────────────────────
        if street not in ('preflop',):
            board = [eval7.Card(s) for s in gs.board]
            if len(board) >= 3:
                self._cat = classify_hand(self._hole, board)

        cat_id = self._cat[0]

        # ═════════════════════════════════════════════════════════════════════
        # AUCTION  —  category-based bid, zero MC overhead
        # ═════════════════════════════════════════════════════════════════════
        if street == 'auction':
            self._chips_pre_auc = gs.my_chips
            bid = self._auction_bid(cat_id, max(1, gs.pot), gs.my_chips)
            self._my_bid = bid
            return ActionBid(bid)

        # ── Detect auction outcome (first post-auction call) ─────────────────
        if not self._auc_detected and street in ('flop', 'turn', 'river'):
            self._auc_detected = True
            self._chips_post_auc = gs.my_chips
            if gs.opp_revealed_cards:
                paid = self._chips_pre_auc - gs.my_chips
                if paid != self._my_bid:
                    self._auction_won = True
                # else: tie (both paid own bid, both saw a card)

                # Analyze revealed card for sizing / bluff decisions
                board_cards = [eval7.Card(s) for s in gs.board]
                rev_card = eval7.Card(gs.opp_revealed_cards[0])
                self._opp_card_brick = not revealed_card_connects(rev_card, board_cards)
            else:
                self._auction_lost = True

        # ── Equity ───────────────────────────────────────────────────────────
        equity = self._equity(gs, tb)

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

        raise_t, call_t = self._thresholds(street)

        # ═════════════════════════════════════════════════════════════════════
        # PREFLOP
        # ═════════════════════════════════════════════════════════════════════
        if street == 'preflop':
            is_sb = (gs.my_wager == 10)

            # Facing a shove (>= 70% of effective stack)
            if can_call and cost > 0:
                eff = min(my_chips, opp_chips)
                shove_frac = cost / max(1, eff)
                if shove_frac >= 0.70:
                    thresh = max(pot_odds + 0.08, 0.46)
                    return ActionCall() if equity >= thresh else ActionFold()
                if shove_frac >= 0.40:
                    thresh = max(pot_odds + 0.05, 0.43)
                    return ActionCall() if equity >= thresh else ActionFold()

            if is_sb:
                # SB: raise wide (>55%), call medium, fold weak
                if equity > 0.55 and can_raise:
                    sz = int(pot * 2.4)
                    return ActionRaise(max(min_r, min(max_r, sz)))
                if equity > 0.40 and can_call:
                    return ActionCall()
                if can_call and cost <= 10 and equity > 0.32:
                    return ActionCall()
                return ActionFold()
            else:
                # BB
                if equity >= raise_t and can_raise:
                    sz = int(pot * 2.2)
                    return ActionRaise(max(min_r, min(max_r, sz)))
                if equity >= call_t:
                    return ActionCall() if can_call else (ActionCheck() if can_check else ActionFold())
                if can_check:
                    return ActionCheck()
                return ActionFold()

        # ═════════════════════════════════════════════════════════════════════
        # POST-FLOP (flop / turn / river)
        # ═════════════════════════════════════════════════════════════════════

        # Overbet protection: tighten call threshold vs large bets
        if cost > 0 and pot > 0:
            overbet = cost / pot
            if   overbet >= 1.5:  call_t = max(call_t, 0.58)
            elif overbet >= 0.9:  call_t = max(call_t, 0.50)
            elif overbet >= 0.6:  call_t = max(call_t, 0.44)

        if cost > 0:
            # ── FACING A BET ─────────────────────────────────────────────────
            if equity >= raise_t and can_raise:
                ra = self._raise_size(equity, pot_odds, min_r, max_r)
                return ActionRaise(ra)

            if equity >= call_t:
                return ActionCall() if can_call else (ActionCheck() if can_check else ActionFold())

            # Bluff raise
            if self._should_bluff(cat_id, spr) and can_raise:
                ra = max(min_r, min(max_r, int(min_r + 0.25 * (max_r - min_r))))
                return ActionRaise(ra)

            if can_check:
                return ActionCheck()
            return ActionFold()

        else:
            # ── ACTING FIRST (no bet to face) ────────────────────────────────
            if equity >= raise_t and can_raise:
                ra = self._value_bet_size(equity, pot, min_r, max_r)
                return ActionRaise(ra)

            # Bet medium-strong hands proactively (don't check monsters/GoodMade)
            if cat_id in (2, 3) and equity >= 0.56 and can_raise:
                freq = 0.65 if street == 'flop' else 0.80
                if random.random() < freq:
                    ra = self._value_bet_size(equity, pot, min_r, max_r)
                    return ActionRaise(ra)

            # Protection bet: medium equity on safe-ish spots
            if equity >= 0.54 and can_raise and random.random() < 0.20:
                ra = max(min_r, min(max_r, int(pot * 0.30)))
                return ActionRaise(ra)

            # Bluff
            if self._should_bluff(cat_id, spr) and can_raise:
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
