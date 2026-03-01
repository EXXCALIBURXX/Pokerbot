from pkbot.base import BaseBot
from pkbot.actions import ActionCall, ActionCheck, ActionRaise, ActionFold, ActionBid
from pkbot.states import PokerState, GameInfo
from pkbot.runner import parse_args, run_bot
import eval7
import random

class Bot(BaseBot):
    def __init__(self):
        self.hole_cards = []
        self.board_cards = []

    def on_hand_start(self, game_info: GameInfo, current_state: PokerState):
        self.hole_cards = []
        self.board_cards = []

    def on_hand_end(self, game_info: GameInfo, current_state: PokerState):
        pass

    def calculate_equity(self, hole_cards, board_cards, opp_revealed_cards):
        try:
            deck = eval7.Deck()
            known_cards = hole_cards + board_cards
            
            opp_known = []
            if opp_revealed_cards:
                for c_str in opp_revealed_cards:
                    c = eval7.Card(c_str)
                    opp_known.append(c)
                    known_cards.append(c)
            
            for card in known_cards:
                if card in deck.cards:
                    deck.cards.remove(card)
            
            iters = 75
            wins = 0
            
            len_board = len(board_cards)
            len_opp_known = len(opp_known)
            needed_opp = max(0, 2 - len_opp_known)
            needed_board = max(0, 5 - len_board)
            
            for _ in range(iters):
                deck.shuffle()
                sample = deck.peek(needed_opp + needed_board)
                
                opp_hole = opp_known + sample[:needed_opp]
                board_rest = sample[needed_opp:]
                final_board = board_cards + board_rest
                
                my_score = eval7.evaluate(hole_cards + final_board)
                opp_score = eval7.evaluate(opp_hole + final_board)
                
                if my_score > opp_score:
                    wins += 1
                elif my_score == opp_score:
                    wins += 0.5
            
            return wins / iters
        
        except:
            return 0.5

    def get_move(self, game_info: GameInfo, current_state: PokerState):
        try:
            self.hole_cards = [eval7.Card(s) for s in current_state.my_hand]
            self.board_cards = [eval7.Card(s) for s in current_state.board]
            
            equity = self.calculate_equity(self.hole_cards, self.board_cards, current_state.opp_revealed_cards)
            
            if current_state.street == 'auction':
                pot_size = current_state.pot
                stack = current_state.my_chips
                
                uncertainty = 1.0 - 2.0 * abs(equity - 0.5)
                uncertainty = max(0, min(1.0, uncertainty))
                
                bid_amount = int(pot_size * 0.15 * uncertainty)
                
                if bid_amount < 2:
                    bid_amount = 0
                
                max_bid = int(stack * 0.05)
                bid_amount = min(bid_amount, max_bid)
                bid_amount = max(0, bid_amount)
                
                return ActionBid(bid_amount)
            
            if current_state.can_act(ActionRaise):
                min_raise, max_raise = current_state.raise_bounds
            else:
                min_raise = 0
                max_raise = 0
            
            pot = current_state.pot
            cost_to_call = current_state.cost_to_call
            
            pot_total = pot + cost_to_call
            pot_odds = cost_to_call / pot_total if pot_total > 0 else 0
            
            raise_threshold = 0.75
            call_threshold = pot_odds + 0.05
            
            if equity > raise_threshold:
                if current_state.can_act(ActionRaise):
                    raise_amount = min_raise + int(pot * 0.5)
                    raise_amount = max(min_raise, min(max_raise, raise_amount))
                    return ActionRaise(raise_amount)
                elif current_state.can_act(ActionCall):
                    return ActionCall()
                else:
                    return ActionCheck()
            
            elif equity > call_threshold:
                if current_state.can_act(ActionCall):
                    return ActionCall()
                
                if current_state.can_act(ActionCheck):
                    return ActionCheck()
            
            else:
                if current_state.can_act(ActionCheck):
                    return ActionCheck()
                
                if random.random() < 0.02 and current_state.can_act(ActionRaise):
                    return ActionRaise(min_raise)
                
                return ActionFold()
        
        except Exception:
            if current_state.can_act(ActionCheck):
                return ActionCheck()
            if current_state.can_act(ActionCall):
                return ActionCall()
            return ActionFold()


if __name__ == '__main__':
    args = parse_args()
    run_bot(Bot(), args)