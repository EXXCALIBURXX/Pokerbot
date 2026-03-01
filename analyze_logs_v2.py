"""
Log analysis tool for IIT Pokerbots engine — Sneak Peek Hold'em.
Parses .glog / .log files and produces detailed statistics + plots.

Usage:
    python analyze_logs.py                     # Analyze latest log
    python analyze_logs.py logs/*.glog         # Analyze specific logs
    python analyze_logs.py --last 10           # Analyze last 10 logs
    python analyze_logs.py --player MyBot      # Focus on one player
    python analyze_logs.py --no-plot           # Text report only
"""
from io import StringIO
import re, sys, os, glob, statistics
from collections import defaultdict, Counter

# ─── Try to import matplotlib (optional) ────────────────────────────────────
try:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.ticker as ticker
    import numpy as np
    HAS_PLT = True
except ImportError:
    HAS_PLT = False
    print("[WARN] matplotlib/numpy not installed — text-only output.")
    print("       Install with: pip install matplotlib numpy")


# ═══════════════════════════════════════════════════════════════════════════════
# LOG PARSER
# ═══════════════════════════════════════════════════════════════════════════════

_RE_HEADER   = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (.+) vs (.+)$')
_RE_ROUND    = re.compile(r'^Round #(\d+), (.+) \((-?\d+)\), (.+) \((-?\d+)\)$')
_RE_BLIND    = re.compile(r'^(.+) posts blind: (\d+)$')
_RE_RECEIVED = re.compile(r'^(.+) received \[(.+)\]$')
_RE_BOARD    = re.compile(r'^(Flop|Turn|River) \[(.+)\], (.+) \((\d+)\), (.+) \((\d+)\)$')
_RE_BID      = re.compile(r'^(.+) bids (\d+)$')
_RE_AUCTION  = re.compile(r'^(.+) won the auction and was revealed \[(.+)\]$')
_RE_ACTION   = re.compile(r'^(.+) (checks|calls|folds|bets \d+|raises to \d+)$')
_RE_AWARDED  = re.compile(r'^(.+) awarded (-?\d+)$')
_RE_SHOWS    = re.compile(r'^(.+) shows \[(.+)\]$')
_RE_FINAL    = re.compile(r'^Final, (.+) \((-?\d+)\), (.+) \((-?\d+)\)$')


class HandRecord:
    """
    Stores all data for one hand.
    Player data is keyed by name (not position) to handle alternating blind order.
    """
    __slots__ = [
        'round_num', 'players',
        'board_flop', 'board_turn', 'board_river',
        'auction_winner', 'revealed_card',
        'last_street', 'actions',      # list of (street, player_name, action_str)
        'ended_by_fold', 'fold_by', 'went_to_showdown',
        'sb_player',
        'pot_at_street',               # dict: street -> pot size at start of that street
        '_seen_board_streets',         # BUG FIX #2: deduplicate duplicate Flop lines
    ]

    def __init__(self):
        for s in self.__slots__:
            setattr(self, s, None)
        self.players = {}
        self.actions = []
        self.ended_by_fold = False
        self.went_to_showdown = False
        self.last_street = 'preflop'
        self.sb_player = None
        self.pot_at_street = {}
        self._seen_board_streets = set()


def parse_glog(filepath):
    """Parse a .glog/.log file into a list of HandRecord objects."""
    with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
        lines = f.readlines()

    hands = []
    cur = None
    p1_name = p2_name = None
    current_street = 'preflop'

    for raw in lines:
        line = raw.strip()
        if not line:
            continue

        # Game header
        m = _RE_HEADER.match(line)
        if m:
            p1_name, p2_name = m.group(2), m.group(3)
            continue

        # Round start
        m = _RE_ROUND.match(line)
        if m:
            if cur is not None:
                hands.append(cur)
            cur = HandRecord()
            cur.round_num = int(m.group(1))
            name_a, score_a = m.group(2), int(m.group(3))
            name_b, score_b = m.group(4), int(m.group(5))
            if p1_name is None:
                p1_name, p2_name = name_a, name_b
            cur.players[name_a] = {'hand': [], 'bid': 0, 'payoff': 0, 'score_before': score_a}
            cur.players[name_b] = {'hand': [], 'bid': 0, 'payoff': 0, 'score_before': score_b}
            current_street = 'preflop'
            continue

        if cur is None:
            continue

        # Blinds
        m = _RE_BLIND.match(line)
        if m:
            if int(m.group(2)) == 10:
                cur.sb_player = m.group(1)
            continue

        # Hole cards
        m = _RE_RECEIVED.match(line)
        if m:
            player, cards = m.group(1), m.group(2).split()
            if player in cur.players:
                cur.players[player]['hand'] = cards
            continue

        # Board — BUG FIX #2: the engine re-emits the Flop header after the
        # auction result (with updated wager counts).  We must still update
        # current_street on the duplicate so that post-auction flop actions are
        # tagged correctly; we just don't overwrite the board cards or pot.
        m = _RE_BOARD.match(line)
        if m:
            street_name = m.group(1).lower()
            current_street = street_name          # always sync street
            if street_name in cur._seen_board_streets:
                continue                          # skip card/pot overwrite
            cur._seen_board_streets.add(street_name)
            cards = m.group(2).split()
            # Pot at this street = sum of both wagers shown in the header line
            wager_a, wager_b = int(m.group(4)), int(m.group(6))
            pot = wager_a + wager_b
            if street_name == 'flop':
                cur.board_flop = cards[:3]
                cur.pot_at_street['flop'] = pot
                current_street = 'flop'
            elif street_name == 'turn':
                cur.board_turn = cards
                cur.pot_at_street['turn'] = pot
                current_street = 'turn'
            elif street_name == 'river':
                cur.board_river = cards
                cur.pot_at_street['river'] = pot
                current_street = 'river'
            cur.last_street = current_street
            continue

        # Bids (BUG FIX #1: only recorded per-hand in players dict,
        # not appended to a global list — avg calculated correctly later)
        m = _RE_BID.match(line)
        if m:
            player, amt = m.group(1), int(m.group(2))
            if player in cur.players:
                cur.players[player]['bid'] = amt
            current_street = 'auction'
            continue

        # Auction result
        m = _RE_AUCTION.match(line)
        if m:
            cur.auction_winner = m.group(1)
            cur.revealed_card = m.group(2)
            continue

        # Showdown reveal
        m = _RE_SHOWS.match(line)
        if m:
            player, cards = m.group(1), m.group(2).split()
            cur.went_to_showdown = True
            if player in cur.players:
                cur.players[player]['hand'] = cards   # update with confirmed hand
            continue

        # Action
        m = _RE_ACTION.match(line)
        if m:
            player, act = m.group(1), m.group(2)
            cur.actions.append((current_street, player, act))
            if act == 'folds':
                cur.ended_by_fold = True
                cur.fold_by = player
            continue

        # Payoff
        m = _RE_AWARDED.match(line)
        if m:
            player, amt = m.group(1), int(m.group(2))
            if player in cur.players:
                cur.players[player]['payoff'] = amt
            continue

    if cur is not None:
        hands.append(cur)

    return p1_name, p2_name, hands


# ═══════════════════════════════════════════════════════════════════════════════
# HELPER UTILITIES
# ═══════════════════════════════════════════════════════════════════════════════

def _bet_amount(act_str):
    """Extract numeric amount from 'bets 200' or 'raises to 400', else 0."""
    m = re.search(r'(\d+)', act_str)
    return int(m.group(1)) if m else 0


def _safe_mean(lst):
    return statistics.mean(lst) if lst else 0.0


def _safe_pct(num, den):
    return 100.0 * num / den if den > 0 else 0.0


def _rolling(values, window=100):
    """Rolling mean over a list of values."""
    result = []
    for i in range(len(values)):
        lo = max(0, i - window + 1)
        result.append(_safe_mean(values[lo:i+1]))
    return result


def _player_actions(h, player, street=None):
    """Return actions for a player, optionally filtered by street."""
    return [(s, p, a) for (s, p, a) in h.actions
            if p == player and (street is None or s == street)]


def _street_actions(h, street):
    """Return all actions on a given street."""
    return [(s, p, a) for (s, p, a) in h.actions if s == street]


# ═══════════════════════════════════════════════════════════════════════════════
# PROFILE BUILDER — compute a full stat profile for one player
# Used for both the focus player AND the opponent (#12)
# ═══════════════════════════════════════════════════════════════════════════════

def _build_profile(hands, player, opp):
    """
    Build a comprehensive statistics profile dict for `player`.
    All metrics computed in a single pass over the hand list.
    """
    p = {
        # ── basics ──────────────────────────────────────────────────────────
        'name': player,
        'total_hands': len(hands),

        # payoffs
        'payoffs': [],
        'cumulative_pnl': [],

        # payoff buckets: (round_num, payoff, HandRecord)
        'big_wins':      [],   # > 500
        'medium_wins':   [],   # 100–500
        'small_wins':    [],   # 0–100
        'small_losses':  [],   # -100–0
        'medium_losses': [],   # -500–-100
        'big_losses':    [],   # < -500

        # street end distribution
        'last_street_counts': Counter(),
        'payoff_by_street': defaultdict(list),

        # fold analysis
        'folds_won': 0, 'folds_lost': 0,
        'fold_pnl_won': 0, 'fold_pnl_lost': 0,

        # showdown
        'showdowns': 0, 'showdown_wins': 0, 'showdown_pnl': 0,

        # ── #4  VPIP / PFR ───────────────────────────────────────────────────
        'vpip': 0,    # voluntarily put chips in (called or raised PF, not counting BB option)
        'pfr': 0,     # preflop raised

        # ── #5  Fold-to-bet per street ──────────────────────────────────────
        'ftb': {s: {'faced': 0, 'folded': 0} for s in ('flop', 'turn', 'river')},

        # ── #6  Aggression Factor per street ────────────────────────────────
        'af': {s: {'raises': 0, 'calls': 0, 'checks': 0, 'folds': 0}
               for s in ('preflop', 'flop', 'turn', 'river')},

        # ── #7  C-bet ────────────────────────────────────────────────────────
        'cbet_opps': 0, 'cbet_made': 0, 'cbet_success': 0,

        # ── #8  Auction bid metrics ──────────────────────────────────────────
        # Only populated for hands that actually reached the flop
        'our_bids': [], 'opp_bids': [],
        'bid_pot_ratios': [],      # our_bid / pot_at_flop
        'bid_ratio_vs_opp': [],    # our_bid / opp_bid  (win>1, lose<1)
        'auctions_won': 0, 'auctions_lost': 0, 'auctions_tied': 0,
        'pnl_when_auc_won': [], 'pnl_when_auc_lost': [],
        'auction_cost_total': 0,

        # ── #9  Street-conditional win rates ────────────────────────────────
        'street_reach': {s: {'n': 0, 'wins': 0} for s in ('flop', 'turn', 'river')},

        # ── #10  Post-auction exploitation ──────────────────────────────────
        'post_auc': {
            'we_won_opp_bet_flop':  0, 'we_won_opp_bet_flop_n':  0,
            'we_lost_opp_bet_flop': 0, 'we_lost_opp_bet_flop_n': 0,
            'we_won_pnl':  [], 'we_lost_pnl': [],
        },

        # ── #11  Bet-size distribution (as % of pot) ────────────────────────
        'bet_sizes_pct': defaultdict(list),   # street -> [bet/pot %]
        'opp_bet_sizes_pct': defaultdict(list),

        # ── #12  position ───────────────────────────────────────────────────
        'pnl_as_sb': [], 'pnl_as_bb': [],

        # action totals (for action freq)
        'our_raises': 0, 'our_calls': 0, 'our_checks': 0, 'our_folds': 0,

        # top losses / wins stored as (round_num, payoff, HandRecord)
        'top10_losses': [],
        'top10_wins':   [],

        'final_pnl': 0,
    }

    cum = 0
    for h in hands:
        if player not in h.players:
            continue
        payoff = h.players[player]['payoff']
        if payoff is None:
            continue

        cum += payoff
        p['payoffs'].append(payoff)
        p['cumulative_pnl'].append(cum)

        # Payoff buckets
        rn = h.round_num
        if   payoff >  500:  p['big_wins'].append((rn, payoff, h))
        elif payoff >  100:  p['medium_wins'].append((rn, payoff, h))
        elif payoff >  0:    p['small_wins'].append((rn, payoff, h))
        elif payoff < -500:  p['big_losses'].append((rn, payoff, h))
        elif payoff < -100:  p['medium_losses'].append((rn, payoff, h))
        else:                p['small_losses'].append((rn, payoff, h))

        # Street end
        ls = h.last_street or 'preflop'
        p['last_street_counts'][ls] += 1
        p['payoff_by_street'][ls].append(payoff)

        # Fold analysis
        if h.ended_by_fold:
            if h.fold_by != player:
                p['folds_won'] += 1
                p['fold_pnl_won'] += payoff
            else:
                p['folds_lost'] += 1
                p['fold_pnl_lost'] += payoff

        # Showdown
        if h.went_to_showdown:
            p['showdowns'] += 1
            p['showdown_pnl'] += payoff
            if payoff > 0:
                p['showdown_wins'] += 1

        # Position
        is_sb = (h.sb_player == player)
        if is_sb:
            p['pnl_as_sb'].append(payoff)
        else:
            p['pnl_as_bb'].append(payoff)

        # ── #4  VPIP / PFR ───────────────────────────────────────────────────
        pf_acts = _player_actions(h, player, 'preflop')
        pf_action_strs = [a for (_, _, a) in pf_acts]
        raised_pf = any('raise' in a or 'bets' in a for a in pf_action_strs)
        called_pf = 'calls' in pf_action_strs
        # BB gets a "free check" option which is not a voluntary action
        is_bb = not is_sb
        bb_only_checked = is_bb and not raised_pf and not called_pf
        if raised_pf or (called_pf and not bb_only_checked):
            p['vpip'] += 1
        if raised_pf:
            p['pfr'] += 1

        # ── #6  Aggression factor (all streets) ─────────────────────────────
        for st in ('preflop', 'flop', 'turn', 'river'):
            for (_, pl, a) in h.actions:
                if pl != player:
                    continue
                if _street_for_af(a, st, h.actions):
                    continue   # wrong street check done inline below

            for (s, pl, a) in h.actions:
                if pl != player or s != st:
                    continue
                if 'raise' in a or 'bets' in a:
                    p['af'][st]['raises'] += 1
                    p['our_raises'] += 1
                elif a == 'calls':
                    p['af'][st]['calls'] += 1
                    p['our_calls'] += 1
                elif a == 'checks':
                    p['af'][st]['checks'] += 1
                    p['our_checks'] += 1
                elif a == 'folds':
                    p['af'][st]['folds'] += 1
                    p['our_folds'] += 1

        # ── #5  Fold-to-bet ──────────────────────────────────────────────────
        for st in ('flop', 'turn', 'river'):
            st_acts = _street_actions(h, st)
            for i, (_, pl, a) in enumerate(st_acts):
                if pl == opp and ('bets' in a or 'raise' in a):
                    # Next action from this player
                    our_response = next(
                        (a2 for (_, pl2, a2) in st_acts[i+1:] if pl2 == player), None
                    )
                    if our_response is not None:
                        p['ftb'][st]['faced'] += 1
                        if our_response == 'folds':
                            p['ftb'][st]['folded'] += 1

        # ── #7  C-bet ────────────────────────────────────────────────────────
        if h.board_flop and raised_pf:
            flop_acts = _street_actions(h, 'flop')
            if flop_acts and flop_acts[0][1] == player:   # we act first on flop
                p['cbet_opps'] += 1
                if 'bets' in flop_acts[0][2] or 'raise' in flop_acts[0][2]:
                    p['cbet_made'] += 1
                    # Did opp fold?
                    opp_folded_to_cbet = any(
                        a == 'folds' for (_, pl, a) in flop_acts if pl == opp
                    )
                    if opp_folded_to_cbet:
                        p['cbet_success'] += 1

        # ── #8  Auction ──────────────────────────────────────────────────────
        if h.board_flop:  # BUG FIX #1: only count bids for hands that reached the flop
            our_bid = h.players[player].get('bid', 0)
            opp_bid = h.players[opp]['bid'] if opp in h.players else 0
            p['our_bids'].append(our_bid)
            p['opp_bids'].append(opp_bid)

            pot_flop = h.pot_at_street.get('flop', 1) or 1
            p['bid_pot_ratios'].append(our_bid / pot_flop)
            if opp_bid > 0:
                p['bid_ratio_vs_opp'].append(our_bid / opp_bid)

            if h.auction_winner == player:
                p['auctions_won'] += 1
                p['pnl_when_auc_won'].append(payoff)
                p['auction_cost_total'] += opp_bid
            elif h.auction_winner == opp:
                p['auctions_lost'] += 1
                p['pnl_when_auc_lost'].append(payoff)
            else:
                p['auctions_tied'] += 1

        # ── #9  Street-conditional win rates ────────────────────────────────
        for st, attr in (('flop', 'board_flop'), ('turn', 'board_turn'), ('river', 'board_river')):
            if getattr(h, attr):
                p['street_reach'][st]['n'] += 1
                if payoff > 0:
                    p['street_reach'][st]['wins'] += 1

        # ── #10  Post-auction exploitation ──────────────────────────────────
        if h.board_flop:
            flop_acts = _street_actions(h, 'flop')
            opp_bet_flop = any('bets' in a or 'raise' in a
                                for (_, pl, a) in flop_acts if pl == opp)
            if h.auction_winner == player:
                p['post_auc']['we_won_opp_bet_flop_n'] += 1
                p['post_auc']['we_won_pnl'].append(payoff)
                if opp_bet_flop:
                    p['post_auc']['we_won_opp_bet_flop'] += 1
            elif h.auction_winner == opp:
                p['post_auc']['we_lost_opp_bet_flop_n'] += 1
                p['post_auc']['we_lost_pnl'].append(payoff)
                if opp_bet_flop:
                    p['post_auc']['we_lost_opp_bet_flop'] += 1

        # ── #11  Bet-size distribution as % of pot ──────────────────────────
        for st in ('flop', 'turn', 'river'):
            pot = h.pot_at_street.get(st, 0) or 1
            for (s, pl, a) in h.actions:
                if s != st:
                    continue
                amt = _bet_amount(a) if ('bets' in a or 'raise' in a) else 0
                if amt > 0:
                    pct = 100.0 * amt / pot
                    if pct > 500:          # cap all-in overbets for readability
                        pct = 500.0
                    if pl == player:
                        p['bet_sizes_pct'][st].append(pct)
                    elif pl == opp:
                        p['opp_bet_sizes_pct'][st].append(pct)

    p['final_pnl'] = cum

    # Top 10 losses/wins
    indexed = [(h.round_num, h.players[player]['payoff'], h)
               for h in hands
               if player in h.players and h.players[player]['payoff'] is not None]
    indexed.sort(key=lambda x: x[1])
    p['top10_losses'] = indexed[:10]
    p['top10_wins']   = indexed[-10:][::-1]

    return p


def _street_for_af(a, st, actions):
    """Helper stub — actual filtering done inline; not used."""
    return False


# ═══════════════════════════════════════════════════════════════════════════════
# ANALYSIS ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

def analyze_match(p1, p2, hands, focus_player=None):
    """Return (focus_profile, opp_profile) dicts for one match."""
    if focus_player is None:
        focus_player = p1
    opp = p2 if focus_player == p1 else p1

    focus_stats = _build_profile(hands, focus_player, opp)
    opp_stats   = _build_profile(hands, opp, focus_player)
    focus_stats['opponent']     = opp
    focus_stats['opp_profile']  = opp_stats   # #12: embed for comparison
    return focus_stats


# ═══════════════════════════════════════════════════════════════════════════════
# HAND DETAIL FORMATTER
# ═══════════════════════════════════════════════════════════════════════════════

def format_hand_detail(h, focus_player):
    opp_name = next((n for n in h.players if n != focus_player), '?')
    payoff   = h.players[focus_player]['payoff']
    our_hand = h.players[focus_player]['hand']
    opp_hand = h.players[opp_name]['hand'] if opp_name in h.players else []
    our_bid  = h.players[focus_player]['bid']
    opp_bid  = h.players[opp_name]['bid'] if opp_name in h.players else 0

    board = (' '.join(h.board_river) if h.board_river
             else ' '.join(h.board_turn) if h.board_turn
             else ' '.join(h.board_flop) if h.board_flop
             else '—')

    auc_tag = ''
    if h.auction_winner == focus_player:
        auc_tag = f'  AUC:WON(our={our_bid} paid={opp_bid}) rev={h.revealed_card}'
    elif h.auction_winner:
        auc_tag = f'  AUC:LOST(our={our_bid} vs opp={opp_bid})'

    end = 'showdown' if h.went_to_showdown else f'fold:{h.fold_by or "?"}'
    pos = 'SB' if h.sb_player == focus_player else 'BB'

    action_summary = ' → '.join(
        f"{pl[:3].upper()}:{a}" for (_, pl, a) in h.actions
        if pl in (focus_player, opp_name)
    )

    return (
        f"  R#{h.round_num:4d} [{pos}] payoff={payoff:+6d}  "
        f"hole=[{' '.join(our_hand or [])}] vs [{' '.join(opp_hand or [])}]  "
        f"board=[{board}]  end={end}{auc_tag}\n"
        f"         actions: {action_summary}"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# TEXT REPORT
# ═══════════════════════════════════════════════════════════════════════════════

def _bar(value, max_val, width=30, char='█'):
    filled = int(round(width * abs(value) / max(1, abs(max_val))))
    return char * filled + '·' * (width - filled)


def print_report(stats):
    s = stats
    op = stats['opp_profile']
    N  = s['total_hands']
    sep = '═' * 80

    def section(title):
        print(f"\n  {'─'*3} {title} {'─'*(72 - len(title))}")

    print('\n' + sep)
    print(f"  MATCH ANALYSIS : {s['name']}  vs  {s['opponent']}   ({N} hands)")
    print(sep)

    # ── Summary ─────────────────────────────────────────────────────────────
    pays = s['payoffs']
    if pays:
        wins   = sum(1 for p in pays if p > 0)
        losses = sum(1 for p in pays if p < 0)
        ties   = sum(1 for p in pays if p == 0)
        avg    = _safe_mean(pays)
        med    = statistics.median(pays)
        sd     = statistics.stdev(pays) if len(pays) > 1 else 0
    else:
        wins = losses = ties = avg = med = sd = 0

    section('SUMMARY')
    print(f"  Final PnL        : {s['final_pnl']:+,d} chips")
    print(f"  W / L / T        : {wins} / {losses} / {ties}   ({_safe_pct(wins, N):.1f}% hands won)")
    print(f"  Avg / Median / σ : {avg:+.1f} / {med:+.1f} / {sd:.1f} chips/hand")

    # ── Payoff distribution ──────────────────────────────────────────────────
    section('PAYOFF DISTRIBUTION')
    buckets = [
        ('Big wins   (>500)',    s['big_wins'],      '+'),
        ('Med wins   (100-500)', s['medium_wins'],   '+'),
        ('Small wins (0-100)',   s['small_wins'],    '+'),
        ('Small loss (0 to -100)', s['small_losses'], '-'),
        ('Med loss (-100 to -500)', s['medium_losses'],'-'),
        ('Big loss  (<-500)',    s['big_losses'],    '-'),
    ]
    max_count = max((len(b) for _, b, _ in buckets), default=1)
    for label, bucket, sign in buckets:
        total = sum(p for _, p, _ in bucket)
        bar   = _bar(len(bucket), max_count)
        print(f"  {label:28s}  {len(bucket):4d}  {bar}  total={total:+,d}")

    # ── Where hands end ──────────────────────────────────────────────────────
    section('WHERE HANDS END  &  P&L BY STREET')
    for st in ('preflop', 'flop', 'turn', 'river'):
        cnt  = s['last_street_counts'].get(st, 0)
        pnls = s['payoff_by_street'].get(st, [])
        avg_p = _safe_mean(pnls)
        tot_p = sum(pnls)
        pct   = _safe_pct(cnt, N)
        print(f"  {st:10s}  ends={cnt:4d} ({pct:5.1f}%)  avg={avg_p:+7.1f}  total={tot_p:+,d}")

    # ── #9  Street-conditional win rates ────────────────────────────────────
    section('#9  STREET-CONDITIONAL WIN RATES')
    print(f"  {'Street':10s}  {'Reached':>8s}  {'Won':>6s}  {'Win%':>6s}  {'Opp Win%':>9s}")
    for st in ('flop', 'turn', 'river'):
        our = s['street_reach'][st]
        them = op['street_reach'][st]
        our_pct  = _safe_pct(our['wins'],  our['n'])
        them_pct = _safe_pct(them['wins'], them['n'])
        print(f"  {st:10s}  {our['n']:8d}  {our['wins']:6d}  {our_pct:5.1f}%  {them_pct:8.1f}%")

    # ── #4  VPIP / PFR ───────────────────────────────────────────────────────
    section('#4  VPIP / PFR  (preflop voluntariness)')
    vpip_pct  = _safe_pct(s['vpip'],      N)
    pfr_pct   = _safe_pct(s['pfr'],       N)
    o_vpip    = _safe_pct(op['vpip'],     N)
    o_pfr     = _safe_pct(op['pfr'],      N)
    limp_pct  = vpip_pct - pfr_pct
    o_limp    = o_vpip - o_pfr
    print(f"  {'Metric':22s}  {'Ours':>8s}  {'Opp':>8s}  {'Note'}")
    print(f"  {'VPIP':22s}  {vpip_pct:7.1f}%  {o_vpip:7.1f}%  (voluntarily entered pot)")
    print(f"  {'PFR':22s}  {pfr_pct:7.1f}%  {o_pfr:7.1f}%  (raised preflop)")
    print(f"  {'Limp rate (VPIP-PFR)':22s}  {limp_pct:7.1f}%  {o_limp:7.1f}%  (called without raising)")

    # ── #6  Aggression Factor ────────────────────────────────────────────────
    section('#6  AGGRESSION FACTOR PER STREET  (AF = bets+raises / calls)')
    print(f"  {'Street':10s}  {'Raises':>7s}  {'Calls':>7s}  {'Checks':>7s}  {'Folds':>7s}  {'AF':>6s}  {'Opp AF':>7s}")
    for st in ('preflop', 'flop', 'turn', 'river'):
        d   = s['af'][st]
        od  = op['af'][st]
        af  = d['raises'] / max(1, d['calls'])
        oaf = od['raises'] / max(1, od['calls'])
        print(f"  {st:10s}  {d['raises']:7d}  {d['calls']:7d}  {d['checks']:7d}  "
              f"{d['folds']:7d}  {af:6.2f}  {oaf:7.2f}")

    # ── #5  Fold-to-bet rates ────────────────────────────────────────────────
    section('#5  FOLD-TO-BET RATES PER STREET  (critical exploit metric)')
    print(f"  {'Street':10s}  {'Faced':>6s}  {'Folded':>7s}  {'Fold%':>7s}  {'Opp Fold%':>10s}  {'Verdict'}")
    verdicts = {(0, 40): 'ok', (40, 60): 'marginal', (60, 75): '⚠ high', (75, 101): '🚨 exploitable'}
    for st in ('flop', 'turn', 'river'):
        d  = s['ftb'][st]
        od = op['ftb'][st]
        our_pct  = _safe_pct(d['folded'],  d['faced'])
        opp_pct  = _safe_pct(od['folded'], od['faced'])
        verdict  = next(v for (lo, hi), v in verdicts.items() if lo <= our_pct < hi)
        print(f"  {st:10s}  {d['faced']:6d}  {d['folded']:7d}  {our_pct:6.1f}%  {opp_pct:9.1f}%  {verdict}")

    # ── #7  C-bet ────────────────────────────────────────────────────────────
    section('#7  CONTINUATION BET (C-BET)')
    print(f"  {'Metric':35s}  {'Ours':>8s}  {'Opp':>8s}")
    for label, our_val, opp_val in [
        ('C-bet opportunities',   s['cbet_opps'], op['cbet_opps']),
        ('C-bets made',           s['cbet_made'], op['cbet_made']),
        ('C-bet % (of opps)',     _safe_pct(s['cbet_made'], s['cbet_opps']),
                                  _safe_pct(op['cbet_made'], op['cbet_opps'])),
        ('C-bet success (fold %)',_safe_pct(s['cbet_success'], s['cbet_made']),
                                  _safe_pct(op['cbet_success'], op['cbet_made'])),
    ]:
        if isinstance(our_val, float):
            print(f"  {label:35s}  {our_val:7.1f}%  {opp_val:7.1f}%")
        else:
            print(f"  {label:35s}  {our_val:8d}  {opp_val:8d}")

    # ── #8  Auction ──────────────────────────────────────────────────────────
    section('#8  AUCTION ANALYSIS')
    auc_total = s['auctions_won'] + s['auctions_lost'] + s['auctions_tied']
    our_avg_bid = _safe_mean(s['our_bids'])
    opp_avg_bid = _safe_mean(s['opp_bids'])
    our_bid_pot = _safe_mean(s['bid_pot_ratios'])
    our_comp    = _safe_mean(s['bid_ratio_vs_opp'])   # <1 = we underbid
    win_pct     = _safe_pct(s['auctions_won'], auc_total)

    print(f"  Auctions (flop hands)  : {auc_total}  won={s['auctions_won']} ({win_pct:.1f}%)  "
          f"lost={s['auctions_lost']}  tied={s['auctions_tied']}")
    print(f"  Our avg bid            : {our_avg_bid:.1f} chips  ({100*our_bid_pot:.1f}% of pot)")
    print(f"  Opp avg bid            : {opp_avg_bid:.1f} chips")
    print(f"  Our bid / opp bid avg  : {our_comp:.3f}  (1.0 = matched; <0.5 = severe underbid)")
    print(f"  Total auction cost paid: {s['auction_cost_total']:+,d} chips (Vickrey: winner pays loser's bid)")
    if s['pnl_when_auc_won']:
        print(f"  Avg PnL when WON  auction: {_safe_mean(s['pnl_when_auc_won']):+.1f}")
    if s['pnl_when_auc_lost']:
        print(f"  Avg PnL when LOST auction: {_safe_mean(s['pnl_when_auc_lost']):+.1f}")

    # ── #10  Post-auction exploitation ──────────────────────────────────────
    section('#10  POST-AUCTION BEHAVIOUR (opp betting frequency on flop)')
    pa = s['post_auc']
    we_won_n  = pa['we_won_opp_bet_flop_n']
    we_lost_n = pa['we_lost_opp_bet_flop_n']
    bet_when_won  = _safe_pct(pa['we_won_opp_bet_flop'],  we_won_n)
    bet_when_lost = _safe_pct(pa['we_lost_opp_bet_flop'], we_lost_n)
    print(f"  Opp bet flop after OPP won auction  : {bet_when_lost:5.1f}%  (n={we_lost_n})")
    print(f"  Opp bet flop after WE  won auction  : {bet_when_won:5.1f}%  (n={we_won_n})")
    print(f"  Avg PnL when we won auction         : {_safe_mean(pa['we_won_pnl']):+.1f}")
    print(f"  Avg PnL when we lost auction        : {_safe_mean(pa['we_lost_pnl']):+.1f}")

    # ── #11  Bet sizing distribution ────────────────────────────────────────
    section('#11  BET SIZING DISTRIBUTION (% of pot)')
    print(f"  {'Street':8s}  {'Ours: mean':>11s}  {'median':>8s}  {'Opp: mean':>11s}  {'median':>8s}  {'count(us/opp)'}")
    for st in ('flop', 'turn', 'river'):
        ours = s['bet_sizes_pct'][st]
        opps = s['opp_bet_sizes_pct'][st]
        if ours or opps:
            print(f"  {st:8s}  {_safe_mean(ours):10.1f}%  "
                  f"{(statistics.median(ours) if ours else 0):7.1f}%  "
                  f"{_safe_mean(opps):10.1f}%  "
                  f"{(statistics.median(opps) if opps else 0):7.1f}%  "
                  f"{len(ours)}/{len(opps)}")

    # ── Fold analysis ────────────────────────────────────────────────────────
    section('FOLD ANALYSIS')
    print(f"  We won by opp fold  : {s['folds_won']:4d}   total PnL: {s['fold_pnl_won']:+,d}")
    print(f"  We folded           : {s['folds_lost']:4d}   total PnL: {s['fold_pnl_lost']:+,d}")

    # ── Showdown ─────────────────────────────────────────────────────────────
    section('SHOWDOWN ANALYSIS')
    sd_n = s['showdowns']
    if sd_n:
        sd_wr = _safe_pct(s['showdown_wins'], sd_n)
        osd_wr = _safe_pct(op['showdown_wins'], op['showdowns'])
        print(f"  Showdowns          : {sd_n}")
        print(f"  Showdown win rate  : {sd_wr:.1f}%  (opp: {osd_wr:.1f}%)")
        print(f"  Showdown PnL       : {s['showdown_pnl']:+,d}  (avg {s['showdown_pnl']/sd_n:+.1f})")

    # ── Position ─────────────────────────────────────────────────────────────
    section('POSITION ANALYSIS')
    sb_n, bb_n = len(s['pnl_as_sb']), len(s['pnl_as_bb'])
    if sb_n:
        print(f"  SB hands : {sb_n:4d}   avg {_safe_mean(s['pnl_as_sb']):+.1f}   total {sum(s['pnl_as_sb']):+,d}")
    if bb_n:
        print(f"  BB hands : {bb_n:4d}   avg {_safe_mean(s['pnl_as_bb']):+.1f}   total {sum(s['pnl_as_bb']):+,d}")

    # ── Action frequency ─────────────────────────────────────────────────────
    section('ACTION FREQUENCY')
    total_acts = s['our_raises'] + s['our_calls'] + s['our_checks'] + s['our_folds']
    o_total    = op['our_raises'] + op['our_calls'] + op['our_checks'] + op['our_folds']
    if total_acts:
        print(f"  {'Action':8s}  {'Ours n':>8s}  {'Ours %':>8s}  {'Opp n':>8s}  {'Opp %':>8s}")
        for label, our_n, opp_n in [
            ('Raises',  s['our_raises'],  op['our_raises']),
            ('Calls',   s['our_calls'],   op['our_calls']),
            ('Checks',  s['our_checks'],  op['our_checks']),
            ('Folds',   s['our_folds'],   op['our_folds']),
        ]:
            print(f"  {label:8s}  {our_n:8d}  {_safe_pct(our_n,total_acts):7.1f}%  "
                  f"{opp_n:8d}  {_safe_pct(opp_n,o_total):7.1f}%")

    # ── #12  Opponent profile summary ────────────────────────────────────────
    section('#12  OPPONENT PROFILE SUMMARY')
    print(f"  Opp final PnL    : {op['final_pnl']:+,d}")
    print(f"  Opp VPIP / PFR   : {_safe_pct(op['vpip'],N):.1f}% / {_safe_pct(op['pfr'],N):.1f}%")
    for st in ('flop', 'turn', 'river'):
        d = op['ftb'][st]
        print(f"  Opp fold-to-bet {st:6s}: {_safe_pct(d['folded'],d['faced']):.1f}%  (n={d['faced']})")
    print(f"  Opp avg bid / pot: {_safe_mean(op['bid_pot_ratios'])*100:.1f}%")

    # ── Top losses/wins ──────────────────────────────────────────────────────
    section('TOP 10 BIGGEST LOSSES')
    for rnum, payoff, h in s['top10_losses']:
        print(format_hand_detail(h, stats['name']))

    section('TOP 10 BIGGEST WINS')
    for rnum, payoff, h in s['top10_wins']:
        print(format_hand_detail(h, stats['name']))

    print('\n' + sep + '\n')


# ═══════════════════════════════════════════════════════════════════════════════
# PLOTS — match-level (2 figures, 10 subplots total)
# ═══════════════════════════════════════════════════════════════════════════════

def plot_match(stats, output_dir, base_name):
    if not HAS_PLT:
        return
    s  = stats
    op = stats['opp_profile']
    focus = s['name']
    payoffs = s['payoffs']
    N = len(payoffs)

    # ── Figure 1: core six panels (original + enhanced) ─────────────────────
    fig1, axes1 = plt.subplots(2, 3, figsize=(19, 11))
    fig1.suptitle(
        f"{focus} vs {s['opponent']}  |  Final PnL: {s['final_pnl']:+,d}  |  "
        f"VPIP {_safe_pct(s['vpip'],s['total_hands']):.0f}%  "
        f"PFR {_safe_pct(s['pfr'],s['total_hands']):.0f}%",
        fontsize=13, fontweight='bold'
    )

    # 1a. Cumulative PnL
    ax = axes1[0, 0]
    ax.plot(s['cumulative_pnl'], linewidth=0.9, color='#2196F3', label=focus)
    ax.fill_between(range(N), s['cumulative_pnl'], 0,
                    where=[p < 0 for p in s['cumulative_pnl']],
                    alpha=0.15, color='#F44336')
    ax.fill_between(range(N), s['cumulative_pnl'], 0,
                    where=[p >= 0 for p in s['cumulative_pnl']],
                    alpha=0.15, color='#4CAF50')
    ax.axhline(0, color='gray', linestyle='--', linewidth=0.5)
    ax.set_title('Cumulative PnL')
    ax.set_xlabel('Hand #')
    ax.set_ylabel('Chips')
    ax.yaxis.set_major_formatter(ticker.FuncFormatter(lambda x, _: f'{x:,.0f}'))

    # 1b. Payoff histogram
    ax = axes1[0, 1]
    clipped = [max(-2000, min(2000, p)) for p in payoffs]
    ax.hist(clipped, bins=60, color='#2196F3', edgecolor='black', linewidth=0.2, alpha=0.8)
    ax.axvline(0, color='red', linewidth=0.8)
    ax.axvline(_safe_mean(payoffs), color='orange', linewidth=1.2, linestyle='--',
               label=f'mean={_safe_mean(payoffs):+.1f}')
    ax.set_title('Payoff Distribution (clipped ±2000)')
    ax.set_xlabel('Payoff (chips)')
    ax.set_ylabel('Hands')
    ax.legend(fontsize=8)

    # 1c. PnL by street (grouped bars: us vs opp)
    ax = axes1[0, 2]
    streets = ['preflop', 'flop', 'turn', 'river']
    our_totals = [sum(s['payoff_by_street'].get(st, [0])) for st in streets]
    opp_totals = [sum(op['payoff_by_street'].get(st, [0])) for st in streets]
    x = np.arange(len(streets))
    w = 0.35
    bars1 = ax.bar(x - w/2, our_totals, w, label=focus,
                   color=['#4CAF50' if v >= 0 else '#F44336' for v in our_totals],
                   edgecolor='black', linewidth=0.4)
    bars2 = ax.bar(x + w/2, opp_totals, w, label=s['opponent'],
                   color=['#81D4FA' if v >= 0 else '#FFAB91' for v in opp_totals],
                   edgecolor='black', linewidth=0.4)
    ax.set_title('Total PnL by Street (us vs opp)')
    ax.set_xticks(x); ax.set_xticklabels(streets)
    ax.axhline(0, color='gray', linestyle='--', linewidth=0.5)
    ax.legend(fontsize=8)

    # 1d. Auction PnL boxplot
    ax = axes1[1, 0]
    data_boxes = [s['pnl_when_auc_won'], s['pnl_when_auc_lost']]
    labels_box  = [
        f"Won auction\n(n={len(s['pnl_when_auc_won'])}, "
        f"avg={_safe_mean(s['pnl_when_auc_won']):+.0f})",
        f"Lost auction\n(n={len(s['pnl_when_auc_lost'])}, "
        f"avg={_safe_mean(s['pnl_when_auc_lost']):+.0f})",
    ]
    for i, (data, label) in enumerate(zip(data_boxes, labels_box)):
        if data:
            bp = ax.boxplot([data], positions=[i], widths=0.5, patch_artist=True,
                            flierprops={'marker': '.', 'markersize': 2})
            bp['boxes'][0].set_facecolor('#81D4FA' if i == 0 else '#FFAB91')
    ax.set_xticks([0, 1])
    ax.set_xticklabels(labels_box, fontsize=8)
    ax.set_title('PnL by Auction Outcome')
    ax.axhline(0, color='gray', linestyle='--', linewidth=0.5)

    # 1e. Position PnL
    ax = axes1[1, 1]
    sb_total = sum(s['pnl_as_sb'])
    bb_total = sum(s['pnl_as_bb'])
    sb_avg   = _safe_mean(s['pnl_as_sb'])
    bb_avg   = _safe_mean(s['pnl_as_bb'])
    bars = ax.bar(['SB', 'BB'], [sb_total, bb_total],
                  color=['#FF9800' if sb_total < 0 else '#4CAF50',
                         '#FF9800' if bb_total < 0 else '#4CAF50'],
                  edgecolor='black', linewidth=0.5)
    for bar, avg in zip(bars, [sb_avg, bb_avg]):
        ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 50,
                f'avg {avg:+.0f}', ha='center', fontsize=8)
    ax.set_title('Total PnL by Position')
    ax.set_ylabel('Chips')
    ax.axhline(0, color='gray', linestyle='--', linewidth=0.5)

    # 1f. Per-hand payoff scatter
    ax = axes1[1, 2]
    colors_sc = ['#4CAF50' if p >= 0 else '#F44336' for p in payoffs]
    ax.scatter(range(N), payoffs, c=colors_sc, s=2, alpha=0.4)
    ax.axhline(0, color='gray', linestyle='--', linewidth=0.5)
    ax.set_title('Per-Hand Payoff')
    ax.set_xlabel('Hand #')
    ax.set_ylabel('Chips')

    plt.tight_layout()
    path1 = os.path.join(output_dir, f'{base_name}_core.png')
    fig1.savefig(path1, dpi=150, bbox_inches='tight')
    plt.close(fig1)
    print(f"  [PLOT] Core chart saved → {path1}")

    # ── Figure 2: new analytics panels ──────────────────────────────────────
    fig2, axes2 = plt.subplots(2, 2, figsize=(16, 12))
    fig2.suptitle(
        f"Advanced Analytics: {focus} vs {s['opponent']}",
        fontsize=13, fontweight='bold'
    )

    # 2a. #13 Rolling 100-hand PnL window
    ax = axes2[0, 0]
    rolling = _rolling(payoffs, window=100)
    opp_rolling = _rolling(op['payoffs'], window=100)
    ax.plot(rolling, linewidth=1.0, color='#2196F3', label=focus)
    ax.plot(opp_rolling, linewidth=1.0, color='#FF7043', label=s['opponent'], alpha=0.7)
    ax.axhline(0, color='gray', linestyle='--', linewidth=0.5)
    ax.fill_between(range(len(rolling)), rolling, 0,
                    where=[r < 0 for r in rolling], alpha=0.12, color='red')
    ax.fill_between(range(len(rolling)), rolling, 0,
                    where=[r >= 0 for r in rolling], alpha=0.12, color='green')
    ax.set_title('Rolling 100-Hand Avg PnL (adaptation tracker)')
    ax.set_xlabel('Hand #')
    ax.set_ylabel('Avg chips / hand')
    ax.legend(fontsize=9)

    # 2b. #14 Bid distribution histogram (only flop hands)
    ax = axes2[0, 1]
    if s['our_bids'] and op['our_bids']:
        max_bid = max(max(s['our_bids'], default=0), max(op['our_bids'], default=0))
        bins    = np.linspace(0, min(max_bid + 10, 500), 40)
        ax.hist(s['our_bids'],  bins=bins, alpha=0.65, color='#2196F3',
                edgecolor='black', linewidth=0.2, label=f'{focus} (avg {_safe_mean(s["our_bids"]):.0f})')
        ax.hist(op['our_bids'], bins=bins, alpha=0.55, color='#FF7043',
                edgecolor='black', linewidth=0.2, label=f'{s["opponent"]} (avg {_safe_mean(op["our_bids"]):.0f})')
        ax.axvline(_safe_mean(s['our_bids']),  color='#2196F3', linewidth=1.5, linestyle='--')
        ax.axvline(_safe_mean(op['our_bids']), color='#FF7043', linewidth=1.5, linestyle='--')
    ax.set_title('Bid Distribution (flop hands only)')
    ax.set_xlabel('Bid amount (chips)')
    ax.set_ylabel('Hands')
    ax.legend(fontsize=9)

    # 2c. #16 Action frequency heatmap by street
    ax = axes2[1, 0]
    action_keys = ['raises', 'calls', 'checks', 'folds']
    action_labels = ['Raise/Bet', 'Call', 'Check', 'Fold']
    street_labels = ['Preflop', 'Flop', 'Turn', 'River']
    matrix = np.zeros((4, 4))  # streets × actions
    for si, st in enumerate(('preflop', 'flop', 'turn', 'river')):
        d     = s['af'][st]
        total = sum(d[k] for k in action_keys)
        for ai, k in enumerate(action_keys):
            matrix[si, ai] = _safe_pct(d[k], total)
    im = ax.imshow(matrix, aspect='auto', cmap='Blues', vmin=0, vmax=100)
    ax.set_xticks(range(4)); ax.set_xticklabels(action_labels, fontsize=9)
    ax.set_yticks(range(4)); ax.set_yticklabels(street_labels, fontsize=9)
    ax.set_title(f'Action Frequency Heatmap — {focus} (% per street)')
    for i in range(4):
        for j in range(4):
            val = matrix[i, j]
            ax.text(j, i, f'{val:.0f}%', ha='center', va='center',
                    fontsize=9, color='white' if val > 55 else 'black', fontweight='bold')
    plt.colorbar(im, ax=ax, shrink=0.8, label='% of actions')

    # 2d. #5 Fold-to-bet rates (us vs opp, per street) — bar chart
    ax = axes2[1, 1]
    ftb_streets = ['flop', 'turn', 'river']
    our_ftb = [_safe_pct(s['ftb'][st]['folded'], s['ftb'][st]['faced']) for st in ftb_streets]
    opp_ftb = [_safe_pct(op['ftb'][st]['folded'], op['ftb'][st]['faced']) for st in ftb_streets]
    x  = np.arange(3)
    w  = 0.35
    ax.bar(x - w/2, our_ftb, w, label=focus, color='#2196F3', edgecolor='black', linewidth=0.4)
    ax.bar(x + w/2, opp_ftb, w, label=s['opponent'], color='#FF7043', edgecolor='black', linewidth=0.4)
    ax.axhline(60, color='orange', linestyle='--', linewidth=1, label='60% warning line')
    ax.axhline(75, color='red',    linestyle='--', linewidth=1, label='75% exploit line')
    for xi, (ov, oo) in enumerate(zip(our_ftb, opp_ftb)):
        ax.text(xi - w/2, ov + 1.5, f'{ov:.0f}%', ha='center', fontsize=8)
        ax.text(xi + w/2, oo + 1.5, f'{oo:.0f}%', ha='center', fontsize=8)
    ax.set_xticks(x); ax.set_xticklabels(['Flop', 'Turn', 'River'])
    ax.set_ylim(0, 100)
    ax.set_ylabel('Fold-to-bet rate (%)')
    ax.set_title('Fold-to-Bet Rate per Street (exploit radar)')
    ax.legend(fontsize=8)

    plt.tight_layout()
    path2 = os.path.join(output_dir, f'{base_name}_advanced.png')
    fig2.savefig(path2, dpi=150, bbox_inches='tight')
    plt.close(fig2)
    print(f"  [PLOT] Advanced chart saved → {path2}")


# ═══════════════════════════════════════════════════════════════════════════════
# MULTI-MATCH AGGREGATE
# ═══════════════════════════════════════════════════════════════════════════════

def aggregate_matches(all_stats):
    agg = {
        'matches': len(all_stats),
        'wins': 0, 'losses': 0, 'ties': 0,
        'total_pnl': 0, 'pnls': [],
        'all_payoffs': [],
        'match_results': [],
    }
    for s in all_stats:
        pnl = s['final_pnl']
        agg['total_pnl'] += pnl
        agg['pnls'].append(pnl)
        agg['all_payoffs'].extend(s['payoffs'])
        if   pnl > 0: agg['wins']   += 1; agg['match_results'].append('W')
        elif pnl < 0: agg['losses'] += 1; agg['match_results'].append('L')
        else:         agg['ties']   += 1; agg['match_results'].append('T')
    return agg


def print_aggregate(agg, all_stats):
    print('\n' + '═' * 80)
    print(f"  AGGREGATE: {agg['matches']} matches")
    print('═' * 80)
    m = agg['matches']
    print(f"  W/L/T: {agg['wins']}/{agg['losses']}/{agg['ties']}  "
          f"({_safe_pct(agg['wins'], m):.1f}% win rate)")
    print(f"  Total PnL: {agg['total_pnl']:+,d}")
    if agg['pnls']:
        print(f"  Avg match PnL : {_safe_mean(agg['pnls']):+,.0f}")
        if len(agg['pnls']) > 1:
            print(f"  Match PnL σ   : {statistics.stdev(agg['pnls']):,.0f}")
    if agg['all_payoffs']:
        print(f"  Avg hand PnL  : {_safe_mean(agg['all_payoffs']):+.2f}")

    print(f"\n  Results: {''.join(agg['match_results'])}")
    for i, s in enumerate(all_stats):
        tag = 'W' if s['final_pnl'] > 0 else ('L' if s['final_pnl'] < 0 else 'T')
        print(f"    [{tag}] Match {i+1}: {s['final_pnl']:+,d}  (opp: {s['opponent']})")

    # Aggregate street PnL
    print(f"\n  STREET PnL (aggregated):")
    for st in ('preflop', 'flop', 'turn', 'river'):
        pnls = []
        for s in all_stats:
            pnls.extend(s['payoff_by_street'].get(st, []))
        if pnls:
            print(f"    {st:10s}  n={len(pnls):5d}  avg={_safe_mean(pnls):+7.1f}  total={sum(pnls):+,d}")

    # Aggregate VPIP/PFR
    total_hands = sum(s['total_hands'] for s in all_stats)
    tot_vpip = sum(s['vpip'] for s in all_stats)
    tot_pfr  = sum(s['pfr']  for s in all_stats)
    print(f"\n  VPIP: {_safe_pct(tot_vpip, total_hands):.1f}%   PFR: {_safe_pct(tot_pfr, total_hands):.1f}%")

    # Aggregate fold-to-bet
    print(f"\n  FOLD-TO-BET (aggregated):")
    for st in ('flop', 'turn', 'river'):
        faced  = sum(s['ftb'][st]['faced']  for s in all_stats)
        folded = sum(s['ftb'][st]['folded'] for s in all_stats)
        print(f"    {st:8s}  {_safe_pct(folded, faced):.1f}%  (faced={faced} folded={folded})")

    # Aggregate auction
    tot_auc_won  = sum(s['auctions_won']  for s in all_stats)
    tot_auc_lost = sum(s['auctions_lost'] for s in all_stats)
    tot_auc      = tot_auc_won + tot_auc_lost
    all_bids = [b for s in all_stats for b in s['our_bids']]
    opp_bids = [b for s in all_stats for b in s['opp_bids']]
    print(f"\n  AUCTION (aggregated):")
    print(f"    Won: {tot_auc_won} ({_safe_pct(tot_auc_won, tot_auc):.1f}%)  Lost: {tot_auc_lost}")
    print(f"    Our avg bid: {_safe_mean(all_bids):.1f}   Opp avg bid: {_safe_mean(opp_bids):.1f}")

    # Aggregate showdown
    total_sd     = sum(s['showdowns']      for s in all_stats)
    total_sd_win = sum(s['showdown_wins']  for s in all_stats)
    total_sd_pnl = sum(s['showdown_pnl']  for s in all_stats)
    if total_sd:
        print(f"\n  SHOWDOWN: {total_sd} total  win rate {_safe_pct(total_sd_win, total_sd):.1f}%  "
              f"PnL {total_sd_pnl:+,d}")

    print('═' * 80)


def plot_aggregate(agg, all_stats, output_path):
    if not HAS_PLT:
        return
    fig, axes = plt.subplots(1, 3, figsize=(16, 5))
    fig.suptitle(
        f"Aggregate: {agg['matches']} matches  W/L: {agg['wins']}/{agg['losses']}  "
        f"Total PnL: {agg['total_pnl']:+,d}",
        fontsize=13, fontweight='bold'
    )

    ax = axes[0]
    pnls   = agg['pnls']
    colors = ['#4CAF50' if p >= 0 else '#F44336' for p in pnls]
    ax.bar(range(len(pnls)), pnls, color=colors, edgecolor='black', linewidth=0.3)
    ax.axhline(0, color='gray', linestyle='--', linewidth=0.5)
    ax.set_title('Match PnL'); ax.set_xlabel('Match #'); ax.set_ylabel('Chips')

    ax = axes[1]
    cum, t = [], 0
    for p in pnls:
        t += p; cum.append(t)
    ax.plot(cum, marker='o', markersize=3, linewidth=1, color='#2196F3')
    ax.axhline(0, color='gray', linestyle='--', linewidth=0.5)
    ax.set_title('Cumulative Match PnL'); ax.set_xlabel('Match #'); ax.set_ylabel('Chips')

    ax = axes[2]
    ax.pie([agg['wins'], agg['losses'], max(0, agg['ties'])],
           labels=['Wins', 'Losses', 'Ties'],
           colors=['#4CAF50', '#F44336', '#9E9E9E'],
           autopct='%1.1f%%', startangle=90)
    ax.set_title('Win/Loss Split')

    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    plt.close()
    print(f"  [PLOT] Aggregate → {output_path}")


def save_text_report(stats, output_dir):
    """
    Save printed report to ./analysis/<logname>_analysis.txt
    """
    os.makedirs(output_dir, exist_ok=True)

    base = re.sub(r'\.(glog|log)$', '', stats['_file'])
    output_path = os.path.join(output_dir, f"{base}_analysis.txt")

    buffer = StringIO()
    old_stdout = sys.stdout
    sys.stdout = buffer

    print_report(stats)

    sys.stdout = old_stdout
    report_text = buffer.getvalue()

    # Print to console
    print(report_text)

    # Save to file
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(report_text)

    print(f"[TEXT] Report saved → {output_path}")
# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════


# def main():
#     import argparse
#     parser = argparse.ArgumentParser(description='Pokerbots Log Analyzer — Sneak Peek Hold\'em')
#     parser.add_argument('files',       nargs='*', help='Log files to analyze (.glog or .log)')
#     parser.add_argument('--last',      type=int,  default=0,          help='Analyze last N log files')
#     parser.add_argument('--player',    type=str,  default=None,       help='Focus player name')
#     parser.add_argument('--no-plot',   action='store_true',           help='Skip all plots')
#     parser.add_argument('--output-dir',type=str,  default='./analysis', help='Output dir for plots')
#     args = parser.parse_args()

#     script_dir = os.path.dirname(os.path.abspath(__file__))
#     log_dir    = os.path.join(script_dir, 'logs')

#     if args.files:
#         files = args.files
#     elif args.last > 0:
#         all_logs = sorted(
#             glob.glob(os.path.join(log_dir, '*.glog')) +
#             glob.glob(os.path.join(log_dir, '*.log'))
#         )
#         files = all_logs[-args.last:]
#     else:
#         all_logs = sorted(
#             glob.glob(os.path.join(log_dir, '*.glog')) +
#             glob.glob(os.path.join(log_dir, '*.log'))
#         )
#         if all_logs:
#             files = [all_logs[-1]]
#             print(f"[INFO] No files specified — analyzing latest: {os.path.basename(files[0])}")
#         else:
#             print("[ERROR] No .glog/.log files found in logs/")
#             return

#     if not files:
#         print("[ERROR] No matching files found.")
#         return

#     os.makedirs(args.output_dir, exist_ok=True)

#     all_stats = []
#     for fpath in files:
#         fname = os.path.basename(fpath)
#         print(f"\n{'─'*60}")
#         print(f"  Parsing: {fname}")
#         print(f"{'─'*60}")
#         p1, p2, hands = parse_glog(fpath)
#         if not hands:
#             print(f"  [SKIP] No hands found in {fname}")
#             continue

#         focus = args.player or p1
#         if focus not in (p1, p2):
#             print(f"  [WARN] Player '{focus}' not found; defaulting to '{p1}'")
#             focus = p1

#         stats = analyze_match(p1, p2, hands, focus_player=focus)
#         stats['_file'] = fname
#         all_stats.append(stats)

#         print_report(stats)
#         save_text_report(stats, args.output_dir)
#         if not args.no_plot and HAS_PLT:
#             base = re.sub(r'\.(glog|log)$', '', fname)
#             plot_match(stats, args.output_dir, base)

#     if len(all_stats) > 1:
#         agg = aggregate_matches(all_stats)
#         print_aggregate(agg, all_stats)
#         if not args.no_plot and HAS_PLT:
#             plot_aggregate(agg, all_stats, os.path.join(args.output_dir, 'aggregate.png'))


# if __name__ == '__main__':
#     main()

# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    import argparse
    parser = argparse.ArgumentParser(description='Pokerbots Log Analyzer — Sneak Peek Hold\'em')
    parser.add_argument('files',       nargs='*', help='Log files to analyze (.glog or .log)')
    parser.add_argument('--last',      type=int,  default=0,          help='Analyze last N log files')
    parser.add_argument('--player',    type=str,  default=None,       help='Focus player name')
    parser.add_argument('--no-plot',   action='store_true',           help='Skip all plots')
    parser.add_argument('--output-dir',type=str,  default='./analysis', help='Output dir for plots')
    args = parser.parse_args()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    log_dir    = os.path.join(script_dir, 'logs')

    if args.files:
        files = args.files
    elif args.last > 0:
        all_logs = sorted(
            glob.glob(os.path.join(log_dir, '*.glog')) +
            glob.glob(os.path.join(log_dir, '*.log'))
        )
        files = all_logs[-args.last:]
    else:
        all_logs = sorted(
            glob.glob(os.path.join(log_dir, '*.glog')) +
            glob.glob(os.path.join(log_dir, '*.log'))
        )
        if all_logs:
            files = [all_logs[-1]]
            print(f"[INFO] No files specified — analyzing latest: {os.path.basename(files[0])}")
        else:
            print("[ERROR] No .glog/.log files found in logs/")
            return

    if not files:
        print("[ERROR] No matching files found.")
        return

    os.makedirs(args.output_dir, exist_ok=True)

    all_stats = []
    for fpath in files:
        fname = os.path.basename(fpath)
        print(f"\n{'─'*60}")
        print(f"  Parsing: {fname}")
        print(f"{'─'*60}")
        p1, p2, hands = parse_glog(fpath)
        if not hands:
            print(f"  [SKIP] No hands found in {fname}")
            continue

        # 🚀 HARDCODED FIX: Exploit the fact that you are always "6_7"
        focus = args.player
        if focus:
            # If you manually pass --player, try to respect it
            matched = next((p for p in (p1, p2) if p.lower() == focus.lower()), None)
            if not matched:
                print(f"  [WARN] Player '{focus}' not found.")
                focus = '6_7' if '6_7' in (p1, p2) else p2
            else:
                focus = matched
        else:
            # Default behavior: actively hunt for "6_7"
            if '6_7' in (p1, p2):
                focus = '6_7'
            else:
                focus = p2 # Ultimate fallback if 6_7 isn't playing this match
                
        print(f"  [INFO] Analyzing from perspective of: {focus}")

        stats = analyze_match(p1, p2, hands, focus_player=focus)
        stats['_file'] = fname
        all_stats.append(stats)

        print_report(stats)
        save_text_report(stats, args.output_dir)
        if not args.no_plot and HAS_PLT:
            base = re.sub(r'\.(glog|log)$', '', fname)
            plot_match(stats, args.output_dir, base)

    if len(all_stats) > 1:
        agg = aggregate_matches(all_stats)
        print_aggregate(agg, all_stats)
        if not args.no_plot and HAS_PLT:
            plot_aggregate(agg, all_stats, os.path.join(args.output_dir, 'aggregate.png'))

if __name__ == '__main__':
    main()