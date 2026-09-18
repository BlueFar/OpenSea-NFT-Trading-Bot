import os
import warnings

# Suppress LibreSSL warnings on macOS
os.environ.setdefault("PYTHONWARNINGS", "ignore")
warnings.filterwarnings("ignore")

from src.bot import main

if __name__ == "__main__":
    main()
