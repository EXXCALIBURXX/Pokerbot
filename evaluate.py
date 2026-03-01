import subprocess
import re
import os

# Settings
NUM_TOURNAMENTS = 50
COMMAND = ["python", "engine.py", "--small_log"]

# Metrics Storage
bot_a_results = {"wins": 0, "profit": 0.0}
bot_b_results = {"wins": 0, "profit": 0.0}

print(f"Starting Evaluation: {NUM_TOURNAMENTS} Tournaments ({NUM_TOURNAMENTS * 1000} total rounds)")
print("-" * 50)

for i in range(1, NUM_TOURNAMENTS + 1):
    # Run the engine
    result = subprocess.run(COMMAND, capture_output=True, text=True)
    output = result.stdout
    
    # Extract Bankrolls using Regex
    # Looking for: Total Bankroll: 1234
    bankrolls = re.findall(r"Total Bankroll: (-?\d+)", output)
    
    if len(bankrolls) >= 2:
        a_bankroll = int(bankrolls[0])
        b_bankroll = int(bankrolls[1])
        
        bot_a_results["profit"] += a_bankroll
        bot_b_results["profit"] += b_bankroll
        
        if a_bankroll > b_bankroll:
            bot_a_results["wins"] += 1
        else:
            bot_b_results["wins"] += 1
            
    print(f"Tournament {i}/{NUM_TOURNAMENTS} Complete. Run P&L: BotA={a_bankroll}, BotB={b_bankroll}")

# Final Summary
print("\n" + "="*50)
print("OVERALL EVALUATION METRICS (50,000 ROUNDS)")
print("="*50)
# Use :+.0f to show the profit as a signed number with no decimals
print(f"BotA (Your Heuristic) Total P&L: {bot_a_results['profit']:+.0f}")
print(f"BotB (Example Bot)    Total P&L: {bot_b_results['profit']:+.0f}")
print("-" * 50)
print(f"Tournament Win/Loss Record: BotA {bot_a_results['wins']} - {bot_b_results['wins']} BotB")
print(f"Average Profit per Tournament: {bot_a_results['profit']/NUM_TOURNAMENTS:+.2f}")