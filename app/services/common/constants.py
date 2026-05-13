GENERIC_CHARGER_ID = "bdd8919c-1714-4776-bbbb-bed44ffc5886"
MIN_SESSIONS_FOR_CHARGER_SPECIFIC = 5

# CSV session import
# Primary format includes UTC offset so local times are stored correctly as UTC.
# Naive format (no offset) is accepted as a fallback and treated as UTC.
CSV_SESSION_DT_FMT = "%d.%m.%Y %H:%M"
CSV_SESSION_DT_FMT_TZ = "%d.%m.%Y %H:%M %z"
NEW_CHARGER_NOMINAL_POWER_KW = 11.0