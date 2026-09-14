"""Calibration wizard.

Walks the user through pointing the mouse at each required screen element inside
the remote (Citrix) window and pressing F8 to capture it. All captured points are
stored RELATIVE to the window's top-left corner, so the automation keeps working
even if the window later opens in a different screen position (e.g. after a
re-login), as long as its internal layout/zoom doesn't change.

As a side effect, this also saves small screenshot "templates" of each element to
templates/*.png. Those are used by auto_detect.py to try to relocate the elements
automatically next time (by appearance), so you don't have to manually recalibrate
after every re-login -- manual calibration here is both the primary setup step and
the way to (re)generate the backup templates for auto-detection.
"""
import json
import time
from pathlib import Path

import keyboard
import pyautogui
from PIL import ImageGrab

import auto_detect
import citrix_utils

try:
    import win32clipboard
    _HAS_CLIPBOARD = True
except ImportError:
    _HAS_CLIPBOARD = False

CONFIG_PATH = Path(__file__).parent / "config.json"
CAPTURE_KEY = "f8"
CANCEL_KEY = "esc"

POINT_PAD_X = 90
POINT_PAD_Y = 16

# How long to wait, after moving the mouse away from a just-captured point, before
# taking its template screenshot -- see _settle_before_screenshot. Long enough for
# a CSS :hover transition to finish reverting on the site.
HOVER_SETTLE_SECONDS = 0.35

# Auto-added around the exact "ponta a ponta" corner clicks for the search-result
# calibration (see run_calibration part 2/2), so the user can click the precise
# edges without having to eyeball a manual margin -- the code always adds a
# consistent buffer on top of whatever exact box was clicked.
TABLE_REGION_PAD = 15

MOUSE_MOVE_SECONDS = 0.25

# Deliberately longer than the runtime default (wait_after_search_seconds) --
# this only runs once, to get the empty result on screen before the user
# manually captures its corners in part 2/2, and there's no retry loop here to
# fall back on if the page hasn't finished loading yet like there is at runtime.
CALIBRATION_SEARCH_WAIT_SECONDS = 3.5

# A CEP/Número combo known to return NO data, used to auto-trigger the empty
# search needed for part 2/2 -- saves the user from having to go find/type one
# themselves right in the middle of the wizard.
KNOWN_EMPTY_CEP = "13420778"
KNOWN_EMPTY_NUMERO = "9999"


def _wait_for_capture(log):
    """Block until the user presses F8 (capture) or ESC (cancel). Returns mouse pos or None."""
    log(f"   Posicione o mouse e pressione [{CAPTURE_KEY.upper()}] para capturar "
        f"(ou [{CANCEL_KEY.upper()}] para cancelar a calibração).")
    while True:
        if keyboard.is_pressed(CAPTURE_KEY):
            pos = pyautogui.position()
            while keyboard.is_pressed(CAPTURE_KEY):
                time.sleep(0.02)
            return pos
        if keyboard.is_pressed(CANCEL_KEY):
            while keyboard.is_pressed(CANCEL_KEY):
                time.sleep(0.02)
            return None
        time.sleep(0.02)


# Minimum width/height (px) for the table-result two-corner capture -- rejects an
# accidental double-tap at the same spot (mouse not moved between the two F8
# presses), which would otherwise silently save a zero-area region: seen in
# practice corrupting a region to a single point and making the template
# screenshot itself fail ("cannot write empty image").
MIN_REGION_SIZE_PX = 10


def _capture_two_corners(log, tl_instruction: str, br_instruction: str):
    """Prompt for two opposite corners (top-left, then bottom-right), retrying
    the pair if they land too close together instead of accepting a degenerate
    region. Returns (x1, y1, x2, y2) in absolute screen coords, or None if
    cancelled."""
    while True:
        log(f"\n> {tl_instruction}")
        tl = _wait_for_capture(log)
        if tl is None:
            return None
        log(f"> {br_instruction}")
        br = _wait_for_capture(log)
        if br is None:
            return None

        x1, y1 = min(tl.x, br.x), min(tl.y, br.y)
        x2, y2 = max(tl.x, br.x), max(tl.y, br.y)
        if (x2 - x1) < MIN_REGION_SIZE_PX or (y2 - y1) < MIN_REGION_SIZE_PX:
            log(f"   Os dois cantos capturados ficaram praticamente no mesmo lugar "
                f"({tl.x},{tl.y}) e ({br.x},{br.y}) -- parece que o mouse não foi movido entre "
                f"um F8 e outro. Vamos tentar de novo: capture os dois cantos OPOSTOS de verdade.")
            continue
        return x1, y1, x2, y2


def _set_clipboard_text(text: str):
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardText(text, win32clipboard.CF_TEXT)
    finally:
        win32clipboard.CloseClipboard()


def _fill_field_at(x, y, value: str):
    """Click the field at this exact absolute position and fill it via clipboard
    paste (same approach as automation._fill_field, which found typed keystrokes
    can get duplicated by the remote app on some machines)."""
    pyautogui.click(x, y, duration=MOUSE_MOVE_SECONDS)
    time.sleep(0.15)
    pyautogui.hotkey("ctrl", "a")
    pyautogui.press("delete")
    time.sleep(0.1)
    if _HAS_CLIPBOARD:
        _set_clipboard_text(value)
        time.sleep(0.05)
        pyautogui.hotkey("ctrl", "v")
    else:
        pyautogui.typewrite(value, interval=0.05)
    time.sleep(0.15)


def _settle_before_screenshot(log):
    """Move the mouse off to a neutral spot (screen center -- never a corner, to
    avoid pyautogui's FAILSAFE) and pause briefly before taking template
    screenshots. Capturing a point (see _wait_for_capture) requires the mouse to
    be RIGHT ON TOP of it, which for a button means it's sitting in its :hover
    state at that exact instant -- but when auto_detect looks for that button
    later, the mouse is normally elsewhere (it searches BEFORE moving/clicking),
    so it's comparing against the button's NORMAL appearance. Saving the template
    while still hovering would bake in a mismatch that lowers match confidence
    for no real reason -- stepping away first keeps the saved appearance
    consistent with what's actually on screen at match time."""
    screen_w, screen_h = pyautogui.size()
    pyautogui.moveTo(screen_w // 2, screen_h // 2, duration=0.1)
    time.sleep(HOVER_SETTLE_SECONDS)


def _save_template(name: str, box, log, click_ratio=None):
    """Save this capture as a NEW appearance variant for `name` -- never overwrites
    an existing one, so calibrating for a different layout (e.g. a narrower window)
    doesn't throw away the variant that already works for the normal layout."""
    path = auto_detect.next_variant_path(name)
    img = ImageGrab.grab(bbox=box, all_screens=True)
    img.save(path)
    if click_ratio is not None:
        sidecar = path.with_suffix(".click.json")
        sidecar.write_text(
            json.dumps({"x_ratio": click_ratio[0], "y_ratio": click_ratio[1]}), encoding="utf-8"
        )
    log(f"   Template salvo: {name}/{path.name} (variante nova, as anteriores continuam valendo)")


def run_calibration(log=print):
    """Run the interactive calibration wizard. `log` receives status strings.

    Returns True on success (config.json written), False if cancelled.
    """
    log("=== CALIBRAÇÃO ===")
    log("Deixe a janela do Citrix com o site Vivo Qualidade aberta e visível.")
    log("No primeiro passo, a janela será identificada pela posição do MOUSE no "
        "momento em que você apertar F8 (não importa qual janela está em foco agora).")

    config = {
        "wait_after_search_seconds": 1.0,
        "delay_between_cnpjs_seconds": 0.7,
        "tesseract_cmd": "C:\\Program Files\\Tesseract-OCR\\tesseract.exe",
    }

    window_left = window_top = None
    window_title = None
    captured = {}
    captured_abs = {}

    # ---------- Parte 1/2: campos de busca ----------
    log("\n--- PARTE 1/2: campos de busca ---")
    log("Importante: apenas POSICIONE o mouse em cada elemento, sem clicar -- um clique "
        "sem querer no campo CEP/Número deixa o cursor piscando ali, e isso fica registrado "
        "no print da calibração.")
    field_steps = [
        ("cep_field", "Posicione o mouse sobre o CAMPO CEP (sem clicar)."),
        ("numero_field", "Posicione o mouse sobre o CAMPO NÚMERO (sem clicar)."),
        ("pesquisar_button", "Posicione o mouse sobre o BOTÃO PESQUISAR."),
    ]
    for i, (key, instruction) in enumerate(field_steps):
        log(f"\n> {instruction}")
        pos = _wait_for_capture(log)
        if pos is None:
            log("Calibração cancelada pelo usuário.")
            return False

        if i == 0:
            win = citrix_utils.find_window_at_point(pos.x, pos.y)
            if win is None:
                log("Não consegui identificar a janela sob o mouse. Tente novamente, "
                    "certifique-se de que o mouse está sobre a janela do Citrix.")
                return False
            window_left, window_top = win.left, win.top
            window_title = citrix_utils.stable_title_anchor(win.title)
            log(f"   Janela identificada: '{win.title}' (canto superior-esquerdo em "
                f"{window_left},{window_top}, tamanho {win.width}x{win.height})")
            log(f"   Usando '{window_title}' como referência estável do título.")
            config["window_title_contains"] = window_title

        captured[key] = (pos.x - window_left, pos.y - window_top)
        captured_abs[key] = (pos.x, pos.y)
        log(f"   Capturado: offset relativo à janela = {captured[key]}")

    config["cep_field"] = {"x": captured["cep_field"][0], "y": captured["cep_field"][1]}
    config["numero_field"] = {"x": captured["numero_field"][0], "y": captured["numero_field"][1]}
    config["pesquisar_button"] = {"x": captured["pesquisar_button"][0], "y": captured["pesquisar_button"][1]}

    log("\nSalvando templates de imagem dos campos (backup para tentativa de calibração automática)...")
    _settle_before_screenshot(log)
    try:
        cx, cy = captured_abs["cep_field"]
        _save_template("cep_field", (cx - POINT_PAD_X, cy - POINT_PAD_Y, cx + POINT_PAD_X, cy + POINT_PAD_Y),
                        log, click_ratio=(0.5, 0.5))

        nx, ny = captured_abs["numero_field"]
        _save_template("numero_field", (nx - POINT_PAD_X, ny - POINT_PAD_Y, nx + POINT_PAD_X, ny + POINT_PAD_Y),
                        log, click_ratio=(0.5, 0.5))

        bx, by = captured_abs["pesquisar_button"]
        _save_template("pesquisar_button", (bx - POINT_PAD_X, by - POINT_PAD_Y, bx + POINT_PAD_X, by + POINT_PAD_Y),
                        log, click_ratio=(0.5, 0.5))
    except Exception as exc:
        log(f"   Aviso: não consegui salvar todos os templates ({exc}). "
            f"A calibração manual continua válida, só a automática pode não funcionar depois.")

    log(f"\nPreenchendo CEP {KNOWN_EMPTY_CEP} / Número {KNOWN_EMPTY_NUMERO} (combinação conhecida "
        f"sem resultado) e clicando em Pesquisar, para já deixar a tela pronta para a Parte 2/2...")
    try:
        cx, cy = captured_abs["cep_field"]
        nx, ny = captured_abs["numero_field"]
        bx, by = captured_abs["pesquisar_button"]
        _fill_field_at(cx, cy, KNOWN_EMPTY_CEP)
        _fill_field_at(nx, ny, KNOWN_EMPTY_NUMERO)
        pyautogui.click(bx, by, duration=MOUSE_MOVE_SECONDS)
        log(f"   Busca disparada. Aguardando {CALIBRATION_SEARCH_WAIT_SECONDS}s o resultado carregar...")
        time.sleep(CALIBRATION_SEARCH_WAIT_SECONDS)
    except Exception as exc:
        log(f"   Aviso: não consegui preencher/buscar automaticamente ({exc}). "
            f"Faça essa busca manualmente antes de continuar.")

    # ---------- Parte 2/2: resultado da pesquisa (ponta a ponta) ----------
    log("\n--- PARTE 2/2: resultado da pesquisa ---")
    log(f"A busca com CEP {KNOWN_EMPTY_CEP} / Número {KNOWN_EMPTY_NUMERO} já foi feita e a tela "
        f"deve estar mostrando 'No data available in table' agora. Se por algum motivo não estiver "
        f"vazia (ex: esse CEP passou a retornar dado), refaça manualmente uma busca que você sabe "
        f"que dá vazio antes de continuar -- é essencial que o resultado vazio esteja na tela agora.")
    corners = _capture_two_corners(
        log,
        "Posicione o mouse EXATAMENTE no CANTO SUPERIOR ESQUERDO do resultado da pesquisa, "
        "ponta a ponta (não precisa deixar margem -- ela é calculada automaticamente).",
        "Agora o CANTO INFERIOR DIREITO, também exato (um canto de verdade diferente do anterior).",
    )
    if corners is None:
        log("Calibração cancelada pelo usuário.")
        return False
    ex1, ey1, ex2, ey2 = corners
    px1, py1 = ex1 - TABLE_REGION_PAD, ey1 - TABLE_REGION_PAD
    px2, py2 = ex2 + TABLE_REGION_PAD, ey2 + TABLE_REGION_PAD
    log(f"   Capturado: {ex1},{ey1} -> {ex2},{ey2} (+{TABLE_REGION_PAD}px de folga automática em cada lado).")

    config["table_row_region"] = {
        "x1": px1 - window_left, "y1": py1 - window_top,
        "x2": px2 - window_left, "y2": py2 - window_top,
    }

    CONFIG_PATH.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"\nCalibração salva em {CONFIG_PATH}")

    log("\nSalvando templates de imagem do resultado (vazio)...")
    _settle_before_screenshot(log)
    try:
        # Same padded box saved as TWO template variants: 'no_data_banner' for the
        # empty/has-data appearance classification (see automation._classify_table_by_image),
        # 'table_row_region' for auto_detect to relocate this whole area later.
        _save_template("no_data_banner", (px1, py1, px2, py2), log)
        _save_template("table_row_region", (px1, py1, px2, py2), log)
    except Exception as exc:
        log(f"   Aviso: não consegui salvar todos os templates ({exc}). "
            f"A calibração manual continua válida, só a automática pode não funcionar depois.")

    return True


def load_config():
    if not CONFIG_PATH.exists():
        return None
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


if __name__ == "__main__":
    run_calibration()
