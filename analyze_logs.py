"""
Log analysis tool for IIT Pokerbots engine.
Parses .glog files and produces detailed statistics + plots.

Usage:
    python analyze_logs.py                     # Analyze latest log
    python analyze_logs.py logs/*.glog         # Analyze specific logs
    python analyze_logs.py --last 10           # Analyze last 10 logs
    python analyze_logs.py --player MyBot      # Focus on one player
"""

import re, sys, os, glob, statistics
from collections import defaultdict, Counter

# ─── Try to import matplotlib (optional) ────────────────────────────────────
try:
    import matplotlib
    matplotlib.use('Agg')  # non-interactive backend
    import matplotlib.pyplot as plt
    import matplotlib.ticker as ticker
    HAS_PLT = True
except ImportError:
    HAS_PLT = False
    print("[WARN] matplotlib not installed. Text-only output. Install with: pip install matplotlib")


# ═══════════════════════════════════════════════════════════════════════════════
# LOG PARSER
# ═══════════════════════════════════════════════════════════════════════════════

_RE_HEADER    = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (.+) vs (.+)$')
_RE_ROUND     = re.compile(r'^Round #(\d+), (.+) \((-?\d+)\), (.+) \((-?\d+)\)$')
_RE_BLIND     = re.compile(r'^(.+) posts blind: (\d+)$')
_RE_RECEIVED  = re.compile(r'^(.+) received \[(.+)\]$')
_RE_BOARD     = re.compile(r'^(Flop|Turn|River) \[(.+)\], (.+) \((\d+)\), (.+) \((\d+)\)$')
_RE_BID       = re.compile(r'^(.+) bids (\d+)$')
_RE_AUCTION   = re.compile(r'^(.+) won the auction and was revealed \[(.+)\]$')
_RE_ACTION    = re.compile(r'^(.+) (checks|calls|folds|bets \d+|raises to \d+)$')
_RE_AWARDED   = re.compile(r'^(.+) awarded (-?\d+)$')
_RE_SHOWS     = re.compile(r'^(.+) shows \[(.+)\]$')
_RE_FINAL     = re.compile(r'^Final, (.+) \((-?\d+)\), (.+) \((-?\d+)\)$')


class HandRecord:
    """Stores all info about one hand (round).
    
    IMPORTANT: Player names are stored by name, not position.
    The Round line alternates player order, so we use dicts keyed by name.
    """
    __slots__ = [
        'round_num', 'players',    # players = {name: {...}}
        'board_flop', 'board_turn', 'board_river',
        'auction_winner', 'revealed_card',
        'last_street', 'actions',    # (street, player_name, action_str)
        'ended_by_fold', 'fold_by', 'went_to_showdown',
        'sb_player',                # name of the SB
    ]
    def __init__(self):
        for s in self.__slots__:
            setattr(self, s, None)
        self.players = {}   # {name: {'hand':[], 'bid':0, 'payoff':0, 'score_before':0}}
        self.actions = []
        self.ended_by_fold = False
        self.went_to_showdown = False
        self.last_street = 'preflop'
        self.sb_player = None


def parse_glog(filepath):
    """Parse a .glog file into a list of HandRecord objects."""
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

        # Header
        m = _RE_HEADER.match(line)
        if m:
            p1_name, p2_name = m.group(2), m.group(3)
            continue

        # Round start  (player order alternates! use names as keys)
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

        # Blind
        m = _RE_BLIND.match(line)
        if m:
            player, amt = m.group(1), int(m.group(2))
            if amt == 10:
                cur.sb_player = player
            continue

        # Received
        m = _RE_RECEIVED.match(line)
        if m:
            player, cards = m.group(1), m.group(2).split()
            if player in cur.players:
                cur.players[player]['hand'] = cards
            continue

        # Board
        m = _RE_BOARD.match(line)
        if m:
            street_name = m.group(1).lower()
            cards = m.group(2).split()
            if street_name == 'flop':
                cur.board_flop = cards[:3]
                current_street = 'flop'
            elif street_name == 'turn':
                cur.board_turn = cards
                current_street = 'turn'
            elif street_name == 'river':
                cur.board_river = cards
                current_street = 'river'
            cur.last_street = current_street
            continue

        # Bid
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

        # Shows (showdown)
        m = _RE_SHOWS.match(line)
        if m:
            cur.went_to_showdown = True
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

        # Awarded
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
# ANALYSIS FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

def analyze_match(p1, p2, hands, focus_player=None):
    """Produce a comprehensive analysis dict for one match."""
    if focus_player is None:
        focus_player = p1  # default: analyze player 1

    opp = p2 if focus_player == p1 else p1

    stats = {
        'focus': focus_player,
        'opponent': opp,
        'total_hands': len(hands),
        'final_pnl': 0,

        # Payoff distribution
        'payoffs': [],
        'big_wins': [],       # payoff > 500
        'big_losses': [],     # payoff < -500
        'medium_wins': [],    # 100..500
        'medium_losses': [],  # -500..-100
        'small_wins': [],     # 0..100
        'small_losses': [],   # -100..0

        # Street analysis
        'last_street_counts': Counter(),  # where hands end
        'payoff_by_street': defaultdict(list),

        # Fold analysis
        'folds_won': 0,      # opponent folded
        'folds_lost': 0,     # we folded
        'fold_pnl_won': 0,
        'fold_pnl_lost': 0,

        # Showdown analysis
        'showdowns': 0,
        'showdown_wins': 0,
        'showdown_pnl': 0,

        # Auction analysis
        'auctions_won': 0,
        'auctions_lost': 0,
        'auctions_tied': 0,
        'our_bids': [],
        'opp_bids': [],
        'pnl_when_auc_won': [],
        'pnl_when_auc_lost': [],
        'auction_cost_total': 0,  # net chips spent on auctions

        # Position analysis
        'pnl_as_sb': [],
        'pnl_as_bb': [],

        # Action distribution
        'our_raises': 0,
        'our_calls': 0,
        'our_checks': 0,
        'our_folds': 0,

        # Cumulative PnL over time
        'cumulative_pnl': [],

        # Biggest hands
        'top10_losses': [],
        'top10_wins': [],
    }

    cum = 0
    for h in hands:
        if focus_player not in h.players:
            continue
        payoff = h.players[focus_player]['payoff']
        if payoff is None:
            continue
        cum += payoff
        stats['payoffs'].append(payoff)
        stats['cumulative_pnl'].append(cum)

        # Buckets
        if   payoff > 500:   stats['big_wins'].append((h.round_num, payoff))
        elif payoff > 100:   stats['medium_wins'].append((h.round_num, payoff))
        elif payoff > 0:     stats['small_wins'].append((h.round_num, payoff))
        elif payoff < -500:  stats['big_losses'].append((h.round_num, payoff))
        elif payoff < -100:  stats['medium_losses'].append((h.round_num, payoff))
        else:                stats['small_losses'].append((h.round_num, payoff))

        # Street
        ls = h.last_street or 'preflop'
        stats['last_street_counts'][ls] += 1
        stats['payoff_by_street'][ls].append(payoff)

        # Folds
        if h.ended_by_fold:
            if h.fold_by != focus_player:
                stats['folds_won'] += 1
                stats['fold_pnl_won'] += payoff
            else:
                stats['folds_lost'] += 1
                stats['fold_pnl_lost'] += payoff

        # Showdown
        if h.went_to_showdown:
            stats['showdowns'] += 1
            stats['showdown_pnl'] += payoff
            if payoff > 0:
                stats['showdown_wins'] += 1

        # Auction (use name-keyed data)
        our_bid = h.players[focus_player]['bid']
        opp_bid = h.players[opp]['bid'] if opp in h.players else 0
        stats['our_bids'].append(our_bid)
        stats['opp_bids'].append(opp_bid)

        if h.auction_winner == focus_player:
            stats['auctions_won'] += 1
            stats['pnl_when_auc_won'].append(payoff)
            stats['auction_cost_total'] += opp_bid  # Vickrey: winner pays loser's bid
        elif h.auction_winner == opp:
            stats['auctions_lost'] += 1
            stats['pnl_when_auc_lost'].append(payoff)
        else:
            stats['auctions_tied'] += 1

        # Position (name-based)
        is_sb = (h.sb_player == focus_player)
        if is_sb:
            stats['pnl_as_sb'].append(payoff)
        else:
            stats['pnl_as_bb'].append(payoff)

        # Actions
        for (street, player, act) in h.actions:
            if player == focus_player:
                if 'raise' in act or 'bets' in act:
                    stats['our_raises'] += 1
                elif act == 'calls':
                    stats['our_calls'] += 1
                elif act == 'checks':
                    stats['our_checks'] += 1
                elif act == 'folds':
                    stats['our_folds'] += 1

    stats['final_pnl'] = cum

    # Top losses/wins
    indexed = [(h.round_num, h.players[focus_player]['payoff'], h) for h in hands
               if focus_player in h.players and h.players[focus_player]['payoff'] is not None]
    indexed.sort(key=lambda x: x[1])
    stats['top10_losses'] = indexed[:10]
    stats['top10_wins'] = indexed[-10:][::-1]

    return stats


def format_hand_detail(h, focus_player):
    """Format a hand for detailed display."""
    opp = [n for n in h.players if n != focus_player]
    opp_name = opp[0] if opp else '?'
    
    payoff = h.players[focus_player]['payoff']
    our_hand = h.players[focus_player]['hand']
    opp_hand = h.players[opp_name]['hand'] if opp_name in h.players else []
    our_bid = h.players[focus_player]['bid']
    opp_bid = h.players[opp_name]['bid'] if opp_name in h.players else 0

    board = ''
    if h.board_river:
        board = ' '.join(h.board_river)
    elif h.board_turn:
        board = ' '.join(h.board_turn)
    elif h.board_flop:
        board = ' '.join(h.board_flop)

    winner_tag = ''
    if h.auction_winner == focus_player:
        winner_tag = f'  AUC:WON(bid {our_bid} paid {opp_bid}) revealed={h.revealed_card}'
    elif h.auction_winner:
        winner_tag = f'  AUC:LOST(bid {our_bid} vs {opp_bid})'

    end = 'showdown' if h.went_to_showdown else ('fold by ' + (h.fold_by or '?'))
    pos = 'SB' if h.sb_player == focus_player else 'BB'

    return (f"  R#{h.round_num:4d} [{pos}] payoff={payoff:+6d}  "
            f"hand=[{' '.join(our_hand or [])}] vs [{' '.join(opp_hand or [])}]  "
            f"board=[{board}]  end={end}{winner_tag}")


def print_report(stats):
    """Print a comprehensive text report."""
    s = stats
    N = s['total_hands']
    print("=" * 80)
    print(f"  MATCH ANALYSIS: {s['focus']} vs {s['opponent']}  ({N} hands)")
    print("=" * 80)

    # Summary
    payoffs = s['payoffs']
    if payoffs:
        avg = statistics.mean(payoffs)
        med = statistics.median(payoffs)
        sd  = statistics.stdev(payoffs) if len(payoffs) > 1 else 0
        wins = sum(1 for p in payoffs if p > 0)
        losses = sum(1 for p in payoffs if p < 0)
        ties = sum(1 for p in payoffs if p == 0)
    else:
        avg = med = sd = 0
        wins = losses = ties = 0

    print(f"\n{'SUMMARY':^80}")
    print(f"  Final PnL:        {s['final_pnl']:+,d} chips")
    print(f"  Avg payoff/hand:  {avg:+.1f}")
    print(f"  Median payoff:    {med:+.1f}")
    print(f"  Std dev:          {sd:.1f}")
    print(f"  Hands won/lost:   {wins}/{losses}/{ties}  ({100*wins/N:.1f}% win rate)" if N else "")

    # Payoff buckets
    print(f"\n{'PAYOFF DISTRIBUTION':^80}")
    print(f"  Big wins (>500):     {len(s['big_wins']):4d}   total: {sum(p for _,p in s['big_wins']):+,d}")
    print(f"  Medium wins (100+):  {len(s['medium_wins']):4d}   total: {sum(p for _,p in s['medium_wins']):+,d}")
    print(f"  Small wins (0-100):  {len(s['small_wins']):4d}   total: {sum(p for _,p in s['small_wins']):+,d}")
    print(f"  Small losses (>-100):{len(s['small_losses']):4d}   total: {sum(p for _,p in s['small_losses']):+,d}")
    print(f"  Medium loss (-100+): {len(s['medium_losses']):4d}   total: {sum(p for _,p in s['medium_losses']):+,d}")
    print(f"  Big losses (<-500):  {len(s['big_losses']):4d}   total: {sum(p for _,p in s['big_losses']):+,d}")

    # Street analysis
    print(f"\n{'WHERE HANDS END':^80}")
    for st in ['preflop', 'flop', 'turn', 'river']:
        cnt = s['last_street_counts'].get(st, 0)
        pnls = s['payoff_by_street'].get(st, [])
        avg_p = statistics.mean(pnls) if pnls else 0
        total_p = sum(pnls)
        print(f"  {st:10s}  count={cnt:4d}  avg_pnl={avg_p:+7.1f}  total_pnl={total_p:+,d}")

    # Fold analysis
    print(f"\n{'FOLD ANALYSIS':^80}")
    print(f"  Won by opponent fold:  {s['folds_won']:4d}   total: {s['fold_pnl_won']:+,d}")
    print(f"  We folded:             {s['folds_lost']:4d}   total: {s['fold_pnl_lost']:+,d}")

    # Showdown analysis
    print(f"\n{'SHOWDOWN ANALYSIS':^80}")
    sd_n = s['showdowns']
    print(f"  Showdowns:       {sd_n}")
    if sd_n > 0:
        print(f"  Showdown wins:   {s['showdown_wins']} ({100*s['showdown_wins']/sd_n:.1f}%)")
        print(f"  Showdown PnL:    {s['showdown_pnl']:+,d}  (avg: {s['showdown_pnl']/sd_n:+.1f})")

    # Auction analysis
    print(f"\n{'AUCTION ANALYSIS':^80}")
    auc_total = s['auctions_won'] + s['auctions_lost'] + s['auctions_tied']
    print(f"  Auctions: won={s['auctions_won']} lost={s['auctions_lost']} tied={s['auctions_tied']}")
    our_avg_bid = statistics.mean(s['our_bids']) if s['our_bids'] else 0
    opp_avg_bid = statistics.mean(s['opp_bids']) if s['opp_bids'] else 0
    print(f"  Our avg bid:   {our_avg_bid:.1f}")
    print(f"  Their avg bid: {opp_avg_bid:.1f}")
    print(f"  Total auction cost (Vickrey): {s['auction_cost_total']:+,d}")
    if s['pnl_when_auc_won']:
        print(f"  Avg PnL when we win auction: {statistics.mean(s['pnl_when_auc_won']):+.1f}")
    if s['pnl_when_auc_lost']:
        print(f"  Avg PnL when we lose auction: {statistics.mean(s['pnl_when_auc_lost']):+.1f}")

    # Position analysis
    print(f"\n{'POSITION ANALYSIS':^80}")
    sb_n = len(s['pnl_as_sb'])
    bb_n = len(s['pnl_as_bb'])
    if sb_n > 0:
        print(f"  SB hands: {sb_n}   avg PnL: {statistics.mean(s['pnl_as_sb']):+.1f}   total: {sum(s['pnl_as_sb']):+,d}")
    if bb_n > 0:
        print(f"  BB hands: {bb_n}   avg PnL: {statistics.mean(s['pnl_as_bb']):+.1f}   total: {sum(s['pnl_as_bb']):+,d}")

    # Action frequency
    print(f"\n{'ACTION FREQUENCY ({s[\"focus\"]})'[:78]:^80}")
    total_acts = s['our_raises'] + s['our_calls'] + s['our_checks'] + s['our_folds']
    if total_acts > 0:
        print(f"  Raises:  {s['our_raises']:5d} ({100*s['our_raises']/total_acts:.1f}%)")
        print(f"  Calls:   {s['our_calls']:5d} ({100*s['our_calls']/total_acts:.1f}%)")
        print(f"  Checks:  {s['our_checks']:5d} ({100*s['our_checks']/total_acts:.1f}%)")
        print(f"  Folds:   {s['our_folds']:5d} ({100*s['our_folds']/total_acts:.1f}%)")

    # Top 10 losses
    print(f"\n{'TOP 10 BIGGEST LOSSES':^80}")
    for rnum, payoff, h in s['top10_losses']:
        print(format_hand_detail(h, stats['focus']))

    # Top 10 wins
    print(f"\n{'TOP 10 BIGGEST WINS':^80}")
    for rnum, payoff, h in s['top10_wins']:
        print(format_hand_detail(h, stats['focus']))

    print("\n" + "=" * 80)


def plot_match(stats, output_path):
    """Generate plots for one match analysis."""
    if not HAS_PLT:
        return

    fig, axes = plt.subplots(2, 3, figsize=(18, 10))
    fig.suptitle(f"{stats['focus']} vs {stats['opponent']}  |  Final PnL: {stats['final_pnl']:+,d}",
                 fontsize=14, fontweight='bold')

    # 1. Cumulative PnL
    ax = axes[0, 0]
    ax.plot(stats['cumulative_pnl'], linewidth=0.8, color='#2196F3')
    ax.axhline(0, color='gray', linestyle='--', linewidth=0.5)
    ax.set_title('Cumulative PnL')
    ax.set_xlabel('Hand #')
    ax.set_ylabel('Chips')
    ax.yaxis.set_major_formatter(ticker.FuncFormatter(lambda x, _: f'{x:,.0f}'))

    # 2. Payoff histogram
    ax = axes[0, 1]
    payoffs = stats['payoffs']
    if payoffs:
        # Clip for visibility
        clipped = [max(-2000, min(2000, p)) for p in payoffs]
        ax.hist(clipped, bins=60, color='#4CAF50', edgecolor='black', linewidth=0.3, alpha=0.8)
    ax.set_title('Payoff Distribution (clipped ±2000)')
    ax.set_xlabel('Payoff')
    ax.set_ylabel('Count')

    # 3. PnL by street
    ax = axes[0, 2]
    streets = ['preflop', 'flop', 'turn', 'river']
    totals = [sum(stats['payoff_by_street'].get(st, [0])) for st in streets]
    colors = ['#FF9800' if t < 0 else '#4CAF50' for t in totals]
    ax.bar(streets, totals, color=colors, edgecolor='black', linewidth=0.5)
    ax.set_title('Total PnL by Street')
    ax.set_ylabel('Chips')
    ax.axhline(0, color='gray', linestyle='--', linewidth=0.5)

    # 4. Auction win/loss PnL comparison
    ax = axes[1, 0]
    auc_data = {
        'Won auction': stats['pnl_when_auc_won'],
        'Lost auction': stats['pnl_when_auc_lost'],
    }
    positions = []
    labels = []
    for i, (label, data) in enumerate(auc_data.items()):
        if data:
            bp = ax.boxplot([data], positions=[i], widths=0.5, patch_artist=True)
            bp['boxes'][0].set_facecolor('#81D4FA' if i == 0 else '#FFAB91')
            labels.append(f"{label}\n(n={len(data)}, avg={statistics.mean(data):+.0f})")
            positions.append(i)
    ax.set_xticks(positions)
    ax.set_xticklabels(labels, fontsize=8)
    ax.set_title('PnL by Auction Outcome')
    ax.axhline(0, color='gray', linestyle='--', linewidth=0.5)

    # 5. Position PnL
    ax = axes[1, 1]
    sb_total = sum(stats['pnl_as_sb'])
    bb_total = sum(stats['pnl_as_bb'])
    ax.bar(['SB', 'BB'], [sb_total, bb_total],
           color=['#FF9800' if sb_total < 0 else '#4CAF50',
                  '#FF9800' if bb_total < 0 else '#4CAF50'],
           edgecolor='black', linewidth=0.5)
    ax.set_title('Total PnL by Position')
    ax.set_ylabel('Chips')
    ax.axhline(0, color='gray', linestyle='--', linewidth=0.5)

    # 6. Per-hand payoff scatter
    ax = axes[1, 2]
    if payoffs:
        colors_scatter = ['#4CAF50' if p >= 0 else '#F44336' for p in payoffs]
        ax.scatter(range(len(payoffs)), payoffs, c=colors_scatter, s=2, alpha=0.5)
    ax.axhline(0, color='gray', linestyle='--', linewidth=0.5)
    ax.set_title('Per-Hand Payoff')
    ax.set_xlabel('Hand #')
    ax.set_ylabel('Chips')

    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    plt.close()
    print(f"  [PLOT] Saved to {output_path}")


# ═══════════════════════════════════════════════════════════════════════════════
# MULTI-MATCH AGGREGATE
# ═══════════════════════════════════════════════════════════════════════════════

def aggregate_matches(all_stats):
    """Aggregate stats across multiple matches."""
    agg = {
        'matches': len(all_stats),
        'wins': 0,
        'losses': 0,
        'ties': 0,
        'total_pnl': 0,
        'pnls': [],
        'all_payoffs': [],
        'match_results': [],
    }
    for s in all_stats:
        pnl = s['final_pnl']
        agg['total_pnl'] += pnl
        agg['pnls'].append(pnl)
        agg['all_payoffs'].extend(s['payoffs'])
        if pnl > 0:
            agg['wins'] += 1
            agg['match_results'].append('W')
        elif pnl < 0:
            agg['losses'] += 1
            agg['match_results'].append('L')
        else:
            agg['ties'] += 1
            agg['match_results'].append('T')

    return agg


def print_aggregate(agg, all_stats):
    """Print aggregate report across matches."""
    print("\n" + "=" * 80)
    print(f"  AGGREGATE ANALYSIS: {agg['matches']} matches")
    print("=" * 80)

    print(f"\n  Win/Loss/Tie: {agg['wins']}/{agg['losses']}/{agg['ties']}")
    print(f"  Win rate: {100*agg['wins']/agg['matches']:.1f}%")
    print(f"  Total PnL: {agg['total_pnl']:+,d}")
    if agg['pnls']:
        print(f"  Avg match PnL: {statistics.mean(agg['pnls']):+,.0f}")
        if len(agg['pnls']) > 1:
            print(f"  Match PnL std: {statistics.stdev(agg['pnls']):,.0f}")
    if agg['all_payoffs']:
        print(f"  Avg hand PnL:  {statistics.mean(agg['all_payoffs']):+.1f}")

    # Match-by-match results
    print(f"\n  Match results: {''.join(agg['match_results'])}")
    for i, s in enumerate(all_stats):
        tag = 'W' if s['final_pnl'] > 0 else ('L' if s['final_pnl'] < 0 else 'T')
        print(f"    [{tag}] Match {i+1}: {s['final_pnl']:+,d}  (opponent: {s['opponent']})")

    # Aggregate street analysis
    print(f"\n  AGGREGATE STREET PnL:")
    for st in ['preflop', 'flop', 'turn', 'river']:
        pnls = []
        for s in all_stats:
            pnls.extend(s['payoff_by_street'].get(st, []))
        if pnls:
            print(f"    {st:10s}  n={len(pnls):5d}  avg={statistics.mean(pnls):+7.1f}  total={sum(pnls):+,d}")

    # Aggregate showdown stats
    total_sd = sum(s['showdowns'] for s in all_stats)
    total_sd_wins = sum(s['showdown_wins'] for s in all_stats)
    total_sd_pnl = sum(s['showdown_pnl'] for s in all_stats)
    if total_sd > 0:
        print(f"\n  AGGREGATE SHOWDOWN:")
        print(f"    Showdowns: {total_sd}  wins: {total_sd_wins} ({100*total_sd_wins/total_sd:.1f}%)")
        print(f"    Showdown PnL: {total_sd_pnl:+,d}  (avg: {total_sd_pnl/total_sd:+.1f})")

    # Aggregate auction
    total_auc_won = sum(s['auctions_won'] for s in all_stats)
    total_auc_lost = sum(s['auctions_lost'] for s in all_stats)
    all_auc_won_pnl = []
    all_auc_lost_pnl = []
    for s in all_stats:
        all_auc_won_pnl.extend(s['pnl_when_auc_won'])
        all_auc_lost_pnl.extend(s['pnl_when_auc_lost'])
    if total_auc_won + total_auc_lost > 0:
        print(f"\n  AGGREGATE AUCTION:")
        print(f"    Won: {total_auc_won}  Lost: {total_auc_lost}")
        if all_auc_won_pnl:
            print(f"    Avg PnL when won: {statistics.mean(all_auc_won_pnl):+.1f}")
        if all_auc_lost_pnl:
            print(f"    Avg PnL when lost: {statistics.mean(all_auc_lost_pnl):+.1f}")

    print("=" * 80)


def plot_aggregate(agg, all_stats, output_path):
    """Plot aggregate data across matches."""
    if not HAS_PLT:
        return

    fig, axes = plt.subplots(1, 3, figsize=(16, 5))
    fig.suptitle(f"Aggregate: {agg['matches']} matches  |  "
                 f"W/L: {agg['wins']}/{agg['losses']}  |  "
                 f"Total PnL: {agg['total_pnl']:+,d}", fontsize=13, fontweight='bold')

    # 1. Match PnL bar chart
    ax = axes[0]
    pnls = agg['pnls']
    colors_bar = ['#4CAF50' if p >= 0 else '#F44336' for p in pnls]
    ax.bar(range(len(pnls)), pnls, color=colors_bar, edgecolor='black', linewidth=0.3)
    ax.axhline(0, color='gray', linestyle='--', linewidth=0.5)
    ax.set_title('Match PnL')
    ax.set_xlabel('Match #')
    ax.set_ylabel('Chips')

    # 2. Cumulative match PnL
    ax = axes[1]
    cum = []
    t = 0
    for p in pnls:
        t += p
        cum.append(t)
    ax.plot(cum, marker='o', markersize=3, linewidth=1, color='#2196F3')
    ax.axhline(0, color='gray', linestyle='--', linewidth=0.5)
    ax.set_title('Cumulative PnL')
    ax.set_xlabel('Match #')
    ax.set_ylabel('Total Chips')

    # 3. Win/Loss pie
    ax = axes[2]
    ax.pie([agg['wins'], agg['losses'], agg['ties']],
           labels=['Wins', 'Losses', 'Ties'],
           colors=['#4CAF50', '#F44336', '#9E9E9E'],
           autopct='%1.1f%%', startangle=90)
    ax.set_title('Win/Loss Distribution')

    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    plt.close()
    print(f"  [PLOT] Aggregate saved to {output_path}")


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    import argparse
    parser = argparse.ArgumentParser(description='Poker Bot Log Analyzer')
    parser.add_argument('files', nargs='*', help='Log files to analyze')
    parser.add_argument('--last', type=int, default=0, help='Analyze last N log files')
    parser.add_argument('--player', type=str, default=None, help='Focus player name')
    parser.add_argument('--no-plot', action='store_true', help='Skip plots')
    parser.add_argument('--output-dir', type=str, default='./analysis', help='Output directory for plots')
    args = parser.parse_args()

    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')

    if args.files:
        files = args.files
    elif args.last > 0:
        all_logs = sorted(glob.glob(os.path.join(log_dir, '*.glog')))
        files = all_logs[-args.last:]
    else:
        all_logs = sorted(glob.glob(os.path.join(log_dir, '*.glog')))
        if all_logs:
            files = [all_logs[-1]]
            print(f"[INFO] No files specified. Analyzing latest: {os.path.basename(files[0])}")
        else:
            print("[ERROR] No .glog files found in logs/")
            return

    if not files:
        print("[ERROR] No matching files found.")
        return

    os.makedirs(args.output_dir, exist_ok=True)

    all_stats = []
    for fpath in files:
        fname = os.path.basename(fpath)
        print(f"\n--- Parsing {fname} ---")
        p1, p2, hands = parse_glog(fpath)
        if not hands:
            print(f"  [SKIP] No hands found in {fname}")
            continue

        focus = args.player or p1
        stats = analyze_match(p1, p2, hands, focus_player=focus)
        stats['_p1'] = p1
        stats['_file'] = fname
        all_stats.append(stats)

        print_report(stats)

        if not args.no_plot and HAS_PLT:
            plot_path = os.path.join(args.output_dir, fname.replace('.glog', '.png'))
            plot_match(stats, plot_path)

    # Aggregate if multiple
    if len(all_stats) > 1:
        agg = aggregate_matches(all_stats)
        print_aggregate(agg, all_stats)
        if not args.no_plot and HAS_PLT:
            agg_path = os.path.join(args.output_dir, 'aggregate.png')
            plot_aggregate(agg, all_stats, agg_path)


if __name__ == '__main__':
    main()
