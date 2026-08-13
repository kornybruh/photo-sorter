"""
Event Photo Sorter -- entry point.

The actual app lives in the sorter/ package (config, storage, photos,
logic, network, routes, plus templates/ and static/ for the frontend).
This file just starts it.

Usage:
    python web_sorter.py
"""
from sorter import main

if __name__ == "__main__":
    main()
