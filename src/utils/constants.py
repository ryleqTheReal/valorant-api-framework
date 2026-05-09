"""Here we will store all constants that are always reused across the entire app"""

VALORANT_PROCESS_NAMES: set[str] = {
    "VALORANT-Win64-Shipping.exe",  # Windows
    "VALORANT-Win64-Shipping",      # Linux (Wine/Proton)
    "VALORANT",                     # Fallback
}

RIOT_CLIENT_PROCESS_NAMES: set[str] = {
    "RiotClientServices.exe",       # Windows
    "RiotClientServices",           # Linux (Wine/Proton)
}