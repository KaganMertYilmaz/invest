"""Yapılandırma: yollar, API, mod -> çekme sıklığı kuralları."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

FD_API_KEY = os.getenv("FD_API_KEY", "")
BASE_URL = "https://api.financialdatasets.ai"

UNIVERSE_FILE = ROOT / "universe.json"
POSITIONS_FILE = ROOT / "positions.json"
DATA_DIR = ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"
LATEST_FILE = DATA_DIR / "latest.json"

# Her API isteği ~$0.02
COST_PER_REQUEST_USD = 0.02
# Temel veri = income ttm + income quarterly + balance + cash flow = 4 istek
FUNDAMENTALS_REQUESTS = 4

# Haftanın günleri: 0=Pzt, 2=Çar, 4=Cum (cron Pzt-Çar-Cum çalışır)
MODES = {
    "normal": {
        "tier_a_days": [0, 2, 4],
        "tier_b_days": [0],
        "spec_days": [0, 2, 4],
        "tier_b_biweekly": False,
        "fundamentals_max_age_days": 90,
    },
    "light": {
        "tier_a_days": [0],
        "tier_b_days": [0],
        "spec_days": [0],
        "tier_b_biweekly": True,  # sadece çift ISO haftalarında
        "fundamentals_max_age_days": 120,
    },
    "paused": None,  # hiç API çağrısı yok
}
