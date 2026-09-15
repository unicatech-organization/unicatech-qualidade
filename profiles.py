"""Named calibration profiles -- each one is a full, independent calibration
(config.json coordinates + templates/*.png appearance images), so the app can
switch between e.g. "Ratio 1.04" and "Ratio 1.07" without needing to recalibrate
every time you move to a PC/monitor with a different DPI scale. Coordinates AND
template images both depend on the real on-screen scale, so a profile has to
carry both together -- reusing just the coordinates from a different scale (or
just the images) doesn't work.

The 'default' profile is special: it IS the project's root config.json and
templates/ folder, so existing setups keep working with zero migration --
nothing needs to move the first time this feature is used.
"""
from pathlib import Path

BASE_DIR = Path(__file__).parent
PROFILES_DIR = BASE_DIR / "profiles"
STATE_DIR = BASE_DIR / "state"
STATE_DIR.mkdir(exist_ok=True)
ACTIVE_PROFILE_FILE = STATE_DIR / "active_profile.txt"

DEFAULT_PROFILE = "default"


def list_profiles():
    """Every available profile name, 'default' first."""
    names = [DEFAULT_PROFILE]
    if PROFILES_DIR.exists():
        names += sorted(p.name for p in PROFILES_DIR.iterdir() if p.is_dir())
    return names


def get_active_profile() -> str:
    if ACTIVE_PROFILE_FILE.exists():
        name = ACTIVE_PROFILE_FILE.read_text(encoding="utf-8").strip()
        if name:
            return name
    return DEFAULT_PROFILE


def set_active_profile(name: str):
    ACTIVE_PROFILE_FILE.write_text(name, encoding="utf-8")


def config_path(profile: str = None) -> Path:
    profile = profile or get_active_profile()
    if profile == DEFAULT_PROFILE:
        return BASE_DIR / "config.json"
    return PROFILES_DIR / profile / "config.json"


def templates_dir(profile: str = None) -> Path:
    profile = profile or get_active_profile()
    if profile == DEFAULT_PROFILE:
        return BASE_DIR / "templates"
    return PROFILES_DIR / profile / "templates"


def create_profile(name: str) -> bool:
    """Create a new empty profile (ready to calibrate into). Returns False if
    the name is invalid or already exists."""
    name = name.strip()
    if not name or name == DEFAULT_PROFILE or name in list_profiles():
        return False
    templates_dir(name).mkdir(parents=True, exist_ok=True)
    return True
