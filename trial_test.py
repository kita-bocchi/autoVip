import sys, time
sys.path.insert(0, ".")
from wallet_daily import WalletBot, init_dirs, load_config

init_dirs()
cfg = load_config()
step = next(s for s in cfg["steps"] if s.get("do") == "trial_tasks")
bot = WalletBot(cfg)
bot.connect()
try:
    ok = bot.action_trial_tasks(step)
    print("RESULT:", ok)
finally:
    bot.close()
